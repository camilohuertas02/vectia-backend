from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from anthropic import AsyncAnthropic
from supabase import create_client, Client
import fitz  # PyMuPDF
import base64
import json
import os
import re

app = FastAPI()

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

@app.post("/api/procesar-factura")
async def procesar_factura(
    archivo: UploadFile = File(...),
    id_usuario: int = Form(...),
    api_key: str = Depends(verify_api_key)
):
    if not archivo.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    try:
        # Renderizado a 150 DPI para precisión visual absoluta
        contenido_pdf = await archivo.read()
        documento = fitz.open(stream=contenido_pdf, filetype="pdf")
        
        imagenes_base64 = []
        for pagina in documento:
            pixmap = pagina.get_pixmap(dpi=150)
            img_b64 = base64.b64encode(pixmap.tobytes("jpeg")).decode('utf-8')
            imagenes_base64.append(img_b64)
        documento.close()

        # MEGA-PROMPT DE AUDITORÍA (MÓDULO B2B ENTERPRISE)
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
            "text": "Procesa la factura y devuelve EXCLUSIVAMENTE el objeto JSON estructurado."
        })

        # PREFILL AVANZADO: Inyectamos la primera estructura clave para destruir cualquier intento de Markdown externo.
        prefill_string = "{\n  \"factura\": {"


       # Ejecución Claude 3.5 Sonnet con técnica de Prefill (JSON forzado)
        respuesta = await anthropic_client.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=2500,
            system=prompt_sistema,
            messages=[
                {"role": "user", "content": contenido_mensaje},
                {"role": "assistant", "content": prefill_string}
            ]
        )

        # Reconstruimos el JSON añadiendo la llave inicial que inyectamos
        texto_crudo = prefill_string + respuesta.content[0].text
        
        texto_json = re.sub(r'```(?:json)?|```', '', texto_crudo).strip()
        
        datos_extraidos = json.loads(texto_json)

        # Inserción en Base de Datos
        datos_factura = datos_extraidos["factura"]
        datos_factura["id_usuario"] = id_usuario
        
        # Validar y limpiar campos si es necesario a nivel Python
        res_factura = supabase.table("facturas").insert(datos_factura).execute()
        id_factura = res_factura.data[0]["id"]

        lista_medidores = datos_extraidos.get("medidores", [])
        if lista_medidores:
            for m in lista_medidores: 
                m["id_factura"] = id_factura
            supabase.table("consumos_medidores").insert(lista_medidores).execute()

        return {
            "status": "success", 
            "id_factura": id_factura,
            "proveedor": datos_factura.get("proveedor"),
            "kpis_financieros": {
                "core": datos_factura.get("costo_servicio_core"),
                "terceros": datos_factura.get("cargos_terceros"),
                "total": datos_factura.get("valor_total")
            },
            "alertas_financieras": datos_factura.get("detalles_adicionales", {})
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics")
async def obtener_analiticas(
    id_usuario: int = Query(...), 
    api_key: str = Depends(verify_api_key)
):
    try:
        # Traemos toda la data de facturas y los medidores asociados
        res = supabase.table("facturas").select("*, consumos_medidores(*)").eq("id_usuario", id_usuario).execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
