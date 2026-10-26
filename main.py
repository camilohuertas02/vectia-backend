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
    team_id: Optional[int] = Form(None),
    api_key: str = Depends(verify_api_key)
):
    if not archivo.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    try:
        contenido_pdf = await archivo.read()
        documento = fitz.open(stream=contenido_pdf, filetype="pdf")
        
        imagenes_base64 = []
        for pagina in documento:
            pixmap = pagina.get_pixmap(dpi=150)
            img_b64 = base64.b64encode(pixmap.tobytes("jpeg")).decode('utf-8')
            imagenes_base64.append(img_b64)
        documento.close()

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

        respuesta = await anthropic_client.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=2500,
            system=prompt_sistema,
            tools=[esquema_herramienta],
            messages=[
                {"role": "user", "content": contenido_mensaje} 
            ]
        )

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
        raise HTTPException(status_code=500, detail=str(e))


def consultar_facturas_equipo_o_usuario(id_usuario: int, team_id: Optional[int] = None):
    query = supabase.table("facturas").select("*, consumos_medidores(*)")
    if team_id is not None:
        query = query.or_(f"team_id.eq.{team_id},id_usuario.eq.{id_usuario}")
    else:
        query = query.eq("id_usuario", id_usuario)
    return query.order("periodo_fin", desc=True).execute()


@app.get("/api/analytics")
async def obtener_analiticas(
    id_usuario: int = Query(...),
    team_id: Optional[int] = Query(None),
    api_key: str = Depends(verify_api_key)
):
    try:
        res = consultar_facturas_equipo_o_usuario(id_usuario=id_usuario, team_id=team_id)
        return {"status": "success", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ChatHistoryItem(BaseModel):
    role: str
    content: str

class ChatQueryRequest(BaseModel):
    id_usuario: int
    team_id: Optional[int] = None
    pregunta: str
    historial: Optional[List[ChatHistoryItem]] = []

@app.post("/api/chat")
async def asistente_analitico_chat(
    payload: ChatQueryRequest,
    api_key: str = Depends(verify_api_key)
):
    try:
        res = consultar_facturas_equipo_o_usuario(
            id_usuario=payload.id_usuario,
            team_id=payload.team_id
        )
        facturas_db = res.data or []

        resumen_global = {
            "total_facturas_analizadas": len(facturas_db),
            "gasto_total_cop": 0.0,
            "costo_neto_core_cop": 0.0,
            "cargos_terceros_cop": 0.0,
            "total_energia_reactiva_cop": 0.0,
            "total_contribucion_solidaridad_cop": 0.0,
            "total_aseo_cop": 0.0,
            "total_alumbrado_publico_cop": 0.0,
            "consumo_energia_kwh": 0.0,
            "consumo_agua_m3": 0.0,
            "consumo_gas_m3": 0.0,
            "huella_carbono_ton_co2e": 0.0
        }

        facturas_limpias = []
        for f in facturas_db:
            val_total = float(f.get("valor_total") or 0)
            val_core = float(f.get("costo_servicio_core") or 0)
            val_terceros = float(f.get("cargos_terceros") or 0)
            detalles = f.get("detalles_adicionales") or {}

            reactiva = float(detalles.get("costo_energia_reactiva_cop") or 0)
            solidaridad = float(detalles.get("costo_contribucion_solidaridad_cop") or 0)
            aseo = float(detalles.get("costo_aseo_cop") or 0)
            alumbrado = float(detalles.get("costo_alumbrado_publico_cop") or 0)

            resumen_global["gasto_total_cop"] += val_total
            resumen_global["costo_neto_core_cop"] += val_core
            resumen_global["cargos_terceros_cop"] += val_terceros
            resumen_global["total_energia_reactiva_cop"] += reactiva
            resumen_global["total_contribucion_solidaridad_cop"] += solidaridad
            resumen_global["total_aseo_cop"] += aseo
            resumen_global["total_alumbrado_publico_cop"] += alumbrado

            medidores_f = f.get("consumos_medidores") or []
            consumo_f = sum(float(m.get("consumo") or 0) for m in medidores_f)
            servicio = f.get("tipo_servicio") or "energia"

            if servicio == "energia":
                resumen_global["consumo_energia_kwh"] += consumo_f
                resumen_global["huella_carbono_ton_co2e"] += (consumo_f * 0.164) / 1000.0
            elif servicio == "agua":
                resumen_global["consumo_agua_m3"] += consumo_f
                resumen_global["huella_carbono_ton_co2e"] += (consumo_f * 0.3) / 1000.0
            elif servicio == "gas":
                resumen_global["consumo_gas_m3"] += consumo_f
                resumen_global["huella_carbono_ton_co2e"] += (consumo_f * 2.02) / 1000.0

            facturas_limpias.append({
                "id": f.get("id"),
                "servicio": servicio,
                "proveedor": f.get("proveedor"),
                "numero_factura": f.get("numero_factura"),
                "cuenta_nic": f.get("numero_cliente"),
                "sede": f.get("direccion_sede"),
                "ciudad": f.get("ciudad"),
                "periodo_inicio": f.get("periodo_inicio"),
                "periodo_fin": f.get("periodo_fin"),
                "dias_facturados": f.get("dias_facturados"),
                "costo_neto_core_cop": val_core,
                "cargos_terceros_cop": val_terceros,
                "valor_total_cop": val_total,
                "energia_reactiva_cop": reactiva,
                "contribucion_solidaridad_cop": solidaridad,
                "aseo_cop": aseo,
                "alumbrado_publico_cop": alumbrado,
                "medidores": [
                    {
                        "medidor": m.get("numero_medidor"),
                        "consumo": float(m.get("consumo") or 0),
                        "unidad": m.get("unidad_medida")
                    }
                    for m in medidores_f
                ]
            })

        prompt_asistente = f"""
        Eres el Asistente Analítico y Normativo de Vectia IA, especializado en análisis financiero de facturación y regulación colombiana de servicios públicos domiciliarios.

        BASE DE CONOCIMIENTO NORMATIVO Y REGULATORIO COLOMBIANO:
        1. Ley 142 de 1994 (Régimen de Servicios Públicos Domiciliarios):
           - Art. 89: Regula la Contribución de Solidaridad (sobreprecio hasta del 20% para usuarios industriales y comerciales destinado a subsidiar estratos 1, 2 y 3).
           - Art. 146: Derecho a la medición real del consumo; prohíbe cobrar por promedio salvo imposibilidad técnica temporal justificada.
           - Art. 150: Límite de 5 meses para cobros inoportunos o retroactivos por error u omisión de la empresa prestadora.
           - Arts. 152 a 158: Derecho de Petición en interés particular y Recursos de Reposición (ante el operador) y en Subsidio de Apelación (ante la Superintendencia de Servicios Públicos Domiciliarios - SSPD). El operador tiene 15 días hábiles para responder (Silencio Administrativo Positivo si no responde en término).
        2. Exención de Contribución de Solidaridad (20%) en Energía y Gas:
           - Normativa: Artículo 211 del Estatuto Tributario (modificado por Ley 1430 de 2010), Decreto 2860 de 2013 y Decreto 1625 de 2016.
           - Usuarios industriales cuya actividad económica principal en el RUT corresponda a los códigos CIIU de las Divisiones 01 a 36 (agroindustria, minería, manufactura) o 41 a 43 (construcción) tienen derecho a la EXENCIÓN del 20% de contribución de solidaridad en energía eléctrica y gas natural, radicando el RUT actualizado (con menos de 30 días de expedición) y solicitud formal ante el comercializador.
        3. Resolución CREG 015 de 2018 (Capítulo 12 - Energía Reactiva):
           - Se penaliza el transporte de energía reactiva inductiva cuando supera el 50% de la energía activa (kWh) consumida en el periodo, y toda la energía reactiva capacitiva inyectada a la red.
           - Factor Multiplicador M: Si el exceso persiste por más de 12 meses, el factor M aumenta progresivamente de M=1 hasta M=6 (mes a mes) y de M=6 a M=12 (anualmente), multiplicando la tarifa de distribución. Solución técnica: compensación mediante bancos de condensadores automáticos o filtros activos.
        4. Acueducto, Alcantarillado y Aseo (Regulación CRA - Resolución CRA 943 de 2021):
           - Desviaciones Significativas de Consumo (Art. 1.13.1.6 CRA 943): El operador está obligado a investigar previamente en sitio antes de facturar un incremento anormal del consumo (fugas ocultas o perceptibles).
           - Tasa de Aseo y Alumbrado Público: Son cargos recaudados conjuntamente en la factura pero regulados por tarifas municipales/distritales (Ley 1819 de 2016 para Impuesto de Alumbrado Público).

        REGLAS DE FORMATO Y RESPUESTA:
        1. Usa párrafos separados por saltos de línea claros y viñetas simples (-).
        2. Cuando uses negritas (**texto**), cierra siempre los dos asteriscos correctamente.
        3. Cita las cifras exactas en Pesos Colombianos ($ COP) y unidades (kWh, m³) de las facturas del usuario.
        4. ALCANCE LEGAL: Cuando des orientación regulatoria o de reclamación, incluye al final una breve nota aclarando que se trata de orientación técnica y normativa basada en la regulación colombiana vigente y no sustituye el concepto formal de un abogado o apoderado jurídico.

        RESUMEN CONSOLIDADO DE LA ORGANIZACIÓN:
        {json.dumps(resumen_global, ensure_ascii=False)}

        DETALLE DE FACTURAS DE LA ORGANIZACIÓN:
        {json.dumps(facturas_limpias, ensure_ascii=False)}
        """

        mensajes_claude = []
        if payload.historial:
            for item in payload.historial[-8:]:
                rol = "assistant" if item.role == "assistant" else "user"
                mensajes_claude.append({"role": rol, "content": item.content})

        mensajes_claude.append({"role": "user", "content": payload.pregunta})

        respuesta_ia = await anthropic_client.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=1500,
            system=prompt_asistente,
            messages=mensajes_claude
        )

        texto_respuesta = ""
        for block in respuesta_ia.content:
            if block.type == "text":
                texto_respuesta += block.text

        return {
            "status": "success",
            "respuesta": texto_respuesta,
            "total_facturas_contexto": len(facturas_db)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Asistente Analítico: {str(e)}")
