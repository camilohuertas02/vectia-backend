from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Query
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from openai import AsyncOpenAI
from supabase import create_client, Client
from fpdf import FPDF
import fitz  # PyMuPDF
import base64
import json
import os
from datetime import datetime

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
    # Define esta variable de entorno en Render. Ej: "vectia_secreto_123"
    SECRET_KEY = os.environ.get("VECTIA_BACKEND_SECRET") 
    
    if api_key != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Acceso denegado: API KEY inválida")
    return api_key


# ==========================================
# ENDPOINT 1: SUBIDA Y EXTRACCIÓN
# ==========================================
@app.post("/api/procesar-factura")
async def procesar_factura(
    archivo: UploadFile = File(...),
    id_usuario: int = Form(...), # Next.js inyectará esto de forma segura
    api_key: str = Depends(verify_api_key)
):
    if not archivo.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    try:
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

        respuesta = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": mensajes}],
            temperature=0.1
        )

        datos_extraidos = json.loads(respuesta.choices[0].message.content)

        datos_factura = datos_extraidos["factura"]
        datos_factura["id_usuario"] = id_usuario
        res_factura = supabase.table("facturas").insert(datos_factura).execute()
        id_factura = res_factura.data[0]["id"]

        lista_medidores = datos_extraidos.get("medidores", [])
        if lista_medidores:
            for m in lista_medidores: m["id_factura"] = id_factura
            supabase.table("consumos_medidores").insert(lista_medidores).execute()

        return {"status": "success", "id_factura": id_factura}

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


# ==========================================
# ENDPOINT 3: GENERACIÓN DE INFORME PDF (Seguro)
# ==========================================
def limpiar_texto(texto):
    """Limpia tildes y caracteres que rompen FPDF"""
    if not texto: return "N/A"
    reemplazos = {'á':'a', 'é':'e', 'í':'i', 'ó':'o', 'ú':'u', 'Á':'A', 'É':'E', 'Í':'I', 'Ó':'O', 'Ú':'U', 'ñ':'n', 'Ñ':'N'}
    texto = str(texto)
    for original, nuevo in reemplazos.items():
        texto = texto.replace(original, nuevo)
    # Ignora cualquier otro caracter extraño
    return texto.encode('latin-1', 'ignore').decode('latin-1')

@app.get("/api/informe-pdf")
async def generar_informe_pdf(
    id_usuario: int = Query(...), 
    api_key: str = Depends(verify_api_key)
):
    try:
        res = supabase.table("facturas").select("*, consumos_medidores(*)").eq("id_usuario", id_usuario).execute()
        facturas = res.data

        if not facturas:
            raise HTTPException(status_code=404, detail="No hay datos para este usuario")

        gasto_total = sum(f.get("valor_total", 0) for f in facturas)
        total_sedes = len(set(f.get("direccion_sede") for f in facturas if f.get("direccion_sede")))

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", style="B", size=20)
        pdf.cell(0, 15, limpiar_texto("Reporte Analítico - Vectia Enterprise"), ln=True, align="C")
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 10, f"Fecha de generacion: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=True, align="C")
        pdf.ln(10)

        pdf.set_font("Helvetica", style="B", size=14)
        pdf.cell(0, 10, "Resumen Financiero", ln=True)
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 8, f"- Gasto Total Consolidado: ${gasto_total:,.2f} COP", ln=True)
        pdf.cell(0, 8, f"- Sedes Analizadas: {total_sedes}", ln=True)
        pdf.ln(10)

        pdf.set_font("Helvetica", style="B", size=12)
        pdf.cell(0, 10, "Desglose por Servicio", ln=True)
        
        pdf.set_font("Helvetica", style="B", size=9)
        pdf.cell(30, 10, "Servicio", border=1)
        pdf.cell(85, 10, "Direccion / Sede", border=1)
        pdf.cell(35, 10, "Ciudad", border=1)
        pdf.cell(40, 10, "Valor Total", border=1, ln=True)

        pdf.set_font("Helvetica", size=9)
        for f in facturas:
            servicio = limpiar_texto(f.get("tipo_servicio", "N/A")).upper()
            dir_sede = limpiar_texto(f.get("direccion_sede") or "No registrada")[:45] 
            ciudad = limpiar_texto(f.get("ciudad") or "N/A")[:20]
            valor = f"${f.get('valor_total', 0):,.2f}"

            pdf.cell(30, 10, servicio, border=1)
            pdf.cell(85, 10, dir_sede, border=1)
            pdf.cell(35, 10, ciudad, border=1)
            pdf.cell(40, 10, valor, border=1, ln=True)

        pdf.ln(15)
        pdf.set_font("Helvetica", style="I", size=10)
        pdf.cell(0, 10, limpiar_texto("Generado automáticamente por Inteligencia Artificial Vectia."), ln=True, align="C")

        pdf_bytes = pdf.output(dest="S").encode("latin-1")
        return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=informe_vectia.pdf"})

    except Exception as e:
        print(f"Error PDF: {e}")
        raise HTTPException(status_code=500, detail="Error interno generando el informe PDF")
