from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Optional, List
from anthropic import AsyncAnthropic
from supabase import create_client, Client
import fitz  # PyMuPDF
import base64
import json
import os
import gc

app = FastAPI(title="Vectia IA Backend Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if supabase_url and supabase_key:
    supabase: Client = create_client(supabase_url, supabase_key)

anthropic_client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

api_key_header = APIKeyHeader(name="X-API-KEY")

async def verify_api_key(api_key: str = Depends(api_key_header)):
    SECRET_KEY = os.environ.get("VECTIA_BACKEND_SECRET") 
    if api_key != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Acceso denegado: API KEY inválida")
    return api_key

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "vectia-ia-engine"}

@app.post("/api/procesar-factura")
async def procesar_factura(
    archivo: UploadFile = File(...),
    id_usuario: int = Form(...),
    team_id: Optional[int] = Form(None),
    api_key: str = Depends(verify_api_key)
):
    if not archivo.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    documento = None
    try:
        # Renderizado a 150 DPI con liberación inmediata de memoria por página
        contenido_pdf = await archivo.read()
        documento = fitz.open(stream=contenido_pdf, filetype="pdf")
        
        imagenes_base64 = []
        for pagina in documento:
            pixmap = pagina.get_pixmap(dpi=150)
            img_b64 = base64.b64encode(pixmap.tobytes("jpeg")).decode('utf-8')
            imagenes_base64.append(img_b64)
            del pixmap  # Libera el buffer de imagen inmediatamente
        documento.close()
        del contenido_pdf
        gc.collect()

        # MEGA-PROMPT DE AUDITORÍA ORIGINAL (MÓDULO B2B ENTERPRISE)
        prompt_sistema = """
        Eres Vectia IA, un auditor financiero, energético y normativo experto en servicios públicos de Colombia (CREG, CRA, SSPD).
        Analiza las imágenes de la factura provista y extrae la información cumpliendo ESTRICTAMENTE estas reglas. 

        REGLAS DE ESTANDARIZACIÓN (¡CRÍTICO!):
        1. "tipo_servicio": Únicamente puede ser "energia", "agua", o "gas".
        2. "proveedor": Debe ser el nombre comercial estandarizado. Opciones permitidas: 
           "Enel Colombia", "EPM", "Vanti", "Triple A", "Air-e", "Afinia", "Celsia", "Gases del Caribe", "Gases de Occidente", "EAAB".
           Si el logo dice "enel", asigna "Enel Colombia".
        3. "unidad_medida": Para energia SIEMPRE es "kWh". Para agua y gas SIEMPRE es "m3".

        REGLAS DE AISLAMIENTO FINANCIERO (LOS 3 COSTOS):
        No uses símbolos de moneda ni separadores de miles. Usa punto para decimales.
        1. "costo_servicio_core": El costo neto del consumo de energía/agua/gas (incluyendo subsidios o contribuciones de ley propios del servicio).
        2. "cargos_terceros": Costos ajenos al core como "Total aseo", "Área Limpia", recolección de basuras, alumbrado público, cuotas de créditos (Ej. Credigolazo) o seguros. Si no hay, es 0.
        3. "valor_total": El gran total a pagar en la factura. Matemáticamente, costo_servicio_core + cargos_terceros debe aproximarse al valor_total.

        EXTRACCIÓN OPERATIVA:
        - "dias_facturados": Busca "Días facturados", "Días de consumo", o calcula la diferencia entre lectura actual y anterior si aplica. Número entero.
        - "numero_factura": El consecutivo fiscal (Ej: DOCUMENTO EQUIVALENTE No. 399...).
        - "numero_cliente": El NIC, Cuenta, o Número de Cliente.
        - "periodo_inicio" y "periodo_fin": Formato "YYYY-MM-DD".

        MÓDULO INSPECTOR Y ALERTAS REGULATORIAS (detalles_adicionales):
        Busca cobros específicos y asigna su VALOR EXACTO (número). Si no existe el cobro, asigna 0.0.
        - "costo_energia_reactiva_cop": Busca "Energía Reactiva", "Reactiva Inductiva/Capacitiva" (Resolución CREG 015).
        - "costo_contribucion_solidaridad_cop": Recargo estipulado por Ley 142 para estratos comerciales/industriales.
        - "costo_aseo_cop": El valor facturado exclusivamente por aseo/recolección de basura.
        - "costo_alumbrado_publico_cop": El valor por alumbrado público.

        DEVUELVE ÚNICAMENTE UN JSON CON LA SIGUIENTE ESTRUCTURA ESTRICTA:
        {
          "factura": {
            "tipo_servicio": "energia",
            "proveedor": "Enel Colombia",
            "numero_factura": "string",
            "numero_cliente": "string",
            "direccion_sede": "string",
            "ciudad": "string",
            "dias_facturados": 30,
            "costo_servicio_core": 0.0,
            "cargos_terceros": 0.0,
            "valor_total": 0.0,
            "periodo_inicio": "YYYY-MM-DD",
            "periodo_fin": "YYYY-MM-DD",
            "detalles_adicionales": {
                "costo_energia_reactiva_cop": 0.0,
                "costo_contribucion_solidaridad_cop": 0.0,
                "costo_aseo_cop": 0.0,
                "costo_alumbrado_publico_cop": 0.0
            }
          },
          "medidores": [
            {
              "numero_medidor": "string o 'MED-UNICO'",
              "consumo": 0.0,
              "unidad_medida": "kWh"
            }
          ]
        }
        """

        contenido_mensaje = []
        for img in imagenes_base64:
            contenido_mensaje.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img
                }
            })
        
        contenido_mensaje.append({
            "type": "text",
            "text": "Procesa las imágenes del recibo y registra los datos invocando la herramienta obligatoria."
        })

        # ESQUEMA DE HERRAMIENTA NATIVA PARA FORZAR JSON PERFECTO
        esquema_herramienta = {
            "name": "registrar_datos_factura",
            "description": "Guarda de manera estructurada los campos auditados del recibo de servicios públicos.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "factura": {
                        "type": "object",
                        "properties": {
                            "tipo_servicio": {"type": "string", "enum": ["energia", "agua", "gas"]},
                            "proveedor": {"type": "string"},
                            "numero_factura": {"type": "string"},
                            "numero_cliente": {"type": "string"},
                            "direccion_sede": {"type": "string"},
                            "ciudad": {"type": "string"},
                            "dias_facturados": {"type": "integer"},
                            "costo_servicio_core": {"type": "number"},
                            "cargos_terceros": {"type": "number"},
                            "valor_total": {"type": "number"},
                            "periodo_inicio": {"type": "string"},
                            "periodo_fin": {"type": "string"},
                            "detalles_adicionales": {
                                "type": "object",
                                "properties": {
                                    "costo_energia_reactiva_cop": {"type": "number"},
                                    "costo_contribucion_solidaridad_cop": {"type": "number"},
                                    "costo_aseo_cop": {"type": "number"},
                                    "costo_alumbrado_publico_cop": {"type": "number"}
                                },
                                "required": ["costo_energia_reactiva_cop", "costo_contribucion_solidaridad_cop", "costo_aseo_cop", "costo_alumbrado_publico_cop"]
                            }
                        },
                        "required": ["tipo_servicio", "proveedor", "numero_factura", "numero_cliente", "direccion_sede", "ciudad", "dias_facturados", "costo_servicio_core", "cargos_terceros", "valor_total", "periodo_inicio", "periodo_fin", "detalles_adicionales"]
                    },
                    "medidores": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "numero_medidor": {"type": "string"},
                                "consumo": {"type": "number"},
                                "unidad_medida": {"type": "string"}
                            },
                            "required": ["numero_medidor", "consumo", "unidad_medida"]
                        }
                    }
                },
                "required": ["factura", "medidores"]
            }
        }

        # Ejecución Claude Sonnet 5.5 (Dejando que el modelo use la herramienta en modo "auto")
        respuesta = await anthropic_client.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=2500,
            system=prompt_sistema,
            tools=[esquema_herramienta],
            messages=[
                {"role": "user", "content": contenido_mensaje} 
            ]
        )

        del imagenes_base64
        gc.collect()

        datos_extraidos = None
        for block in respuesta.content:
            if block.type == "tool_use":
                datos_extraidos = block.input
                break

        if not datos_extraidos:
            raise HTTPException(status_code=500, detail="El modelo no invocó la estructura de datos requerida.")

        datos_factura = datos_extraidos["factura"]
        datos_factura["id_usuario"] = id_usuario
        if team_id is not None:
            datos_factura["team_id"] = team_id
        
        res_factura = supabase.table("facturas").insert(datos_factura).execute()
        id_factura = res_factura.data[0]["id"]

        lista_medidores = datos_extraidos.get("medidores", [])
        if lista_medidores:
            for m in lista_medidores: 
                m["id_factura"] = id_factura
            supabase.table("consumos_medidores").insert(lista_medidores).execute()

        # CONTRATO CON EL FRONTEND RESTAURADO
        return {
            "status": "success", 
            "id_factura": id_factura,
            "proveedor": datos_factura.get("proveedor"),
            "numero_factura": datos_factura.get("numero_factura"), 
            "detalles_adicionales": datos_factura.get("detalles_adicionales", {}), 
            "kpis_financieros": {
                "core": datos_factura.get("costo_servicio_core"),
                "terceros": datos_factura.get("cargos_terceros"),
                "total": datos_factura.get("valor_total")
            }
        }

    except Exception as e:
        if documento:
            try:
                documento.close()
            except Exception:
                pass
        gc.collect()
        raise HTTPException(status_code=500, detail=str(e))
