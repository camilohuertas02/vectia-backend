from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

# Inicialización de clientes (Render inyectará las variables de entorno)
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# Evitamos que la app colapse si en local no has puesto las keys aún
if supabase_url and supabase_key:
    supabase: Client = create_client(supabase_url, supabase_key)

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

@app.post("/api/procesar-factura")
async def procesar_factura(
    archivo: UploadFile = File(...),
    id_usuario: int = Form(...) # Recibe el ID del usuario desde el frontend
):
    if not archivo.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    try:
        # 1. Transformar PDF a Imágenes en Memoria
        contenido_pdf = await archivo.read()
        documento = fitz.open(stream=contenido_pdf, filetype="pdf")
        
        imagenes_base64 = []
        for pagina in documento:
            pixmap = pagina.get_pixmap(dpi=150)
            img_bytes = pixmap.tobytes("jpeg")
            img_b64 = base64.b64encode(img_bytes).decode('utf-8')
            imagenes_base64.append(img_b64)
            
        documento.close()

        # 2. Prompt estricto
        prompt = """
        Eres Vectia IA, un sistema de extracción de datos de servicios públicos para Colombia.
        Analiza las imágenes adjuntas de esta factura. Devuelve ÚNICAMENTE un objeto JSON válido.
        El JSON debe contener dos llaves principales ("factura" y "consumo_medidor") que coinciden exactamente con la estructura de la base de datos relacional.
        Respeta los tipos de datos: si un valor numérico no existe usa null, usa formato YYYY-MM-DD para fechas.
        
        ESTRUCTURA EXACTA REQUERIDA:
        {
          "factura": {
            "tipo_servicio": "Energía o Acueducto",
            "proveedor": "Nombre de la empresa",
            "numero_factura": "Número de la factura",
            "numero_cliente": "Número de cuenta o cliente",
            "valor_total": 0, 
            "periodo_inicio": "YYYY-MM-DD",
            "periodo_fin": "YYYY-MM-DD",
            "fecha_vencimiento": "YYYY-MM-DD",
            "estado_pago": "pendiente"
          },
          "consumo_medidor": {
            "numero_medidor": "Número serial del medidor",
            "consumo": 0.0,
            "unidad_medida": "kWh o m3 o unidad correspondiente",
            "detalles_adicionales": {} 
          }
        }
        """

        # 3. Consultar a GPT-4o-mini con Visión
        mensajes_contenido = [{"type": "text", "text": prompt}]
        for img_b64 in imagenes_base64:
            mensajes_contenido.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{img_b64}",
                    "detail": "high"
                }
            })

        respuesta = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": mensajes_contenido}],
            temperature=0.1
        )

        datos_extraidos = json.loads(respuesta.choices[0].message.content)

        # 4. Inserciones en la Base de Datos (Supabase)
        datos_factura = datos_extraidos["factura"]
        datos_factura["id_usuario"] = id_usuario

        resultado_factura = supabase.table("facturas").insert(datos_factura).execute()
        
        if not resultado_factura.data:
            raise HTTPException(status_code=500, detail="Error al insertar factura en BD")
            
        id_factura_generado = resultado_factura.data[0]["id"]

        datos_consumo = datos_extraidos["consumo_medidor"]
        datos_consumo["id_factura"] = id_factura_generado

        supabase.table("consumos_medidores").insert(datos_consumo).execute()

        # 5. Retornar solo confirmación (SIN enviar el JSON completo)
        return {
            "status": "success",
            "mensaje": "Factura procesada y guardada en base de datos correctamente",
            "id_factura": id_factura_generado
        }

    except Exception as e:
        print(f"Error procesando factura: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
