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
            pixmap = pagina.get_pixmap(dpi=150)
            img_b64 = base64.b64encode(pixmap.tobytes("jpeg")).decode('utf-8')
            imagenes_base64.append(img_b64)
        documento.close()

        prompt = """
        Eres Vectia IA, experto en extracción estructurada de facturas de servicios públicos en Colombia.
        REGLAS:
        1. Numéricos puros: sin '$', sin puntos de mil. Decimales con punto.
        2. Fechas: Formato ISO "YYYY-MM-DD".
        3. "tipo_servicio": "energia", "agua" o "gas".
        4. "medidores" es SIEMPRE un arreglo.
        5. Separa la dirección de la sede y la ciudad basándote en la información del cliente.
        
        ESTRUCTURA:
        {
          "factura": {
            "tipo_servicio": "energia",
            "proveedor": "string",
            "numero_factura": "string",
            "numero_cliente": "string",
            "direccion_sede": "string (Solo la nomenclatura)",
            "ciudad": "string",
            "valor_total": 0.0,
            "periodo_inicio": "YYYY-MM-DD o null",
            "periodo_fin": "YYYY-MM-DD o null",
            "fecha_vencimiento": "YYYY-MM-DD o null",
            "estado_pago": "pendiente",
            "detalles_adicionales": {}
          },
          "medidores": [
            {
              "numero_medidor": "string",
              "consumo": 0.0,
              "unidad_medida": "kWh",
              "detalles_adicionales": {}
            }
          ]
        }
        """

        mensajes = [{"type": "text", "text": prompt}]
        for img in imagenes_base64:
            mensajes.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}", "detail": "high"}})

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
# ENDPOINT 3: GENERACIÓN DE INFORME PDF
# ==========================================
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
        pdf.cell(0, 15, "Reporte Analitico - Vectia Enterprise", ln=True, align="C")
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 10, f"Fecha de generacion: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=True, align="C")
        pdf.ln(10)

        pdf.set_font("Helvetica", style="B", size=14)
        pdf.cell(0, 10, f"Resumen Financiero", ln=True)
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
            servicio = str(f.get("tipo_servicio", "N/A")).upper()
            dir_sede = str(f.get("direccion_sede") or "No registrada")[:45] 
            ciudad = str(f.get("ciudad") or "N/A")[:20]
            valor = f"${f.get('valor_total', 0):,.2f}"

            pdf.cell(30, 10, servicio, border=1)
            pdf.cell(85, 10, dir_sede, border=1)
            pdf.cell(35, 10, ciudad, border=1)
            pdf.cell(40, 10, valor, border=1, ln=True)

        pdf.ln(15)
        pdf.set_font("Helvetica", style="I", size=10)
        pdf.cell(0, 10, "Generado automaticamente por Inteligencia Artificial Vectia.", ln=True, align="C")

        pdf_bytes = pdf.output(dest="S").encode("latin-1")
        return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=informe_vectia.pdf"})

    except Exception as e:
        raise HTTPException(status_code=500, detail="Error generando el informe PDF")
