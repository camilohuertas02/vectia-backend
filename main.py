from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from openai import AsyncOpenAI
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

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ==========================================
# SEGURIDAD SERVIDOR A SERVIDOR (API KEY)
# ==========================================
api_key_header = APIKeyHeader(name="X-API-KEY")

async def verify_api_key(api_key: str = Depends(api_key_header)):
    # Define esta variable de entorno en tu servidor (Render/Railway)
    SECRET_KEY = os.environ.get("VECTIA_BACKEND_SECRET") 
    
    if api_key != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Acceso denegado: API KEY inválida")
    return api_key


# ==========================================
# ENDPOINT 1: SUBIDA Y EXTRACCIÓN CON IA
# ==========================================
@app.post("/api/procesar-factura")
async def procesar_factura(
    archivo: UploadFile = File(...),
    id_usuario: int = Form(...),
    api_key: str = Depends(verify_api_key)
):
    if not archivo.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    try:
        # 1. Leer y rasterizar PDF
        contenido_pdf = await archivo.read()
        documento = fitz.open(stream=contenido_pdf, filetype="pdf")
        
        imagenes_base64 = []
        for pagina in documento:
            pixmap = pagina.get_pixmap(dpi=90)
            img_b64 = base64.b64encode(pixmap.tobytes("jpeg")).decode('utf-8')
            imagenes_base64.append(img_b64)
        documento.close()


        prompt = """
        Eres Vectia IA, un auditor financiero y energético experto en la normativa de servicios públicos de Colombia (CREG, CRA, SSPD).
        Tu misión es analizar facturas (energía, acueducto, gas natural) y extraer los datos con precisión contable, ignorando la publicidad.

        REGLAS DE EXTRACCIÓN Y AUDITORÍA:
        1. CLASIFICACIÓN INICIAL: Determina si el recibo es de "energia", "agua" o "gas".
        2. SANITIZACIÓN FINANCIERA: Extrae los valores netos sin símbolos '$' ni separadores de miles (,). Usa punto (.) estrictamente para decimales.
        3. DIRECCIÓN Y CIUDAD: Separa la nomenclatura de la sede de la ciudad (Ej. Dirección: "CR 51 167 50", Ciudad: "Bogotá").
        4. FECHAS: Usa estrictamente el formato ISO "YYYY-MM-DD". Si no es deducible, devuelve null.

        5. REGLAS DE AUDITORÍA (MÓDULO DE ALERTAS):
           Evalúa minuciosamente los conceptos facturados e inyecta alertas en el objeto 'detalles_adicionales' SOLO si existen:
           - Si el servicio es ENERGÍA: Busca activamente recargos por "Energía Reactiva" (CREG 015), "Penalización", "Contribución de Solidaridad" (Ley 142) o "Recargo estrato comercial". Si existen, márcalos como true.
           - Si el servicio es AGUA: Busca "Recargo por exceso de consumo", "Cobro por vertimientos" o "Servicio de Aseo" si está unificado.
           - Si el servicio es GAS: Busca "Revisión técnica obligatoria" o "Penalizaciones por reconexión".

        ESTRUCTURA JSON ESTRICTA:
        {
          "factura": {
            "tipo_servicio": "energia",
            "proveedor": "string",
            "numero_factura": "string",
            "numero_cliente": "string",
            "direccion_sede": "string",
            "ciudad": "string",
            "valor_total": 0.0,
            "periodo_inicio": "YYYY-MM-DD",
            "periodo_fin": "YYYY-MM-DD",
            "detalles_adicionales": {
                // Genera dinámicamente estas claves solo si detectas las anomalías, con valor boolean true
                "alerta_energia_reactiva": boolean,
                "cobro_contribucion_solidaridad": boolean,
                "recargo_exceso_consumo": boolean
            }
          },
          "medidores": [
            {
              "numero_medidor": "string (Si no hay medidor explícito, usa 'MED-UNICO')",
              "consumo": 0.0,
              "unidad_medida": "kWh",
              "detalles_adicionales": {}
            }
          ]
        }
        """


        mensajes = [{"type": "text", "text": prompt}]
        for img in imagenes_base64:
            mensajes.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}", "detail": "auto"}})

        # 3. Invocación al LLM
        respuesta = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": mensajes}],
            temperature=0.1
        )

        datos_extraidos = json.loads(respuesta.choices[0].message.content)

        # 4. Inserción en Supabase
        datos_factura = datos_extraidos["factura"]
        datos_factura["id_usuario"] = id_usuario
        res_factura = supabase.table("facturas").insert(datos_factura).execute()
        id_factura = res_factura.data[0]["id"]

        lista_medidores = datos_extraidos.get("medidores", [])
        if lista_medidores:
            for m in lista_medidores: m["id_factura"] = id_factura
            supabase.table("consumos_medidores").insert(lista_medidores).execute()

        # 5. Respuesta para el Frontend (Necesario para el renombramiento de archivo inteligente)
        return {
            "status": "success", 
            "id_factura": id_factura,
            "proveedor": datos_factura.get("proveedor"),
            "numero_factura": datos_factura.get("numero_factura"),
            "detalles_adicionales": datos_factura.get("detalles_adicionales", {})
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# ENDPOINT 2: DATOS PARA EL DASHBOARD
# ==========================================
@app.get("/api/analytics")
async def obtener_analiticas(
    id_usuario: int = Query(...), 
    api_key: str = Depends(verify_api_key)
):
    try:
        res = supabase.table("facturas").select("*, consumos_medidores(*)").eq("id_usuario", id_usuario).execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
