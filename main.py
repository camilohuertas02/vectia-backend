from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF
from openai import AsyncOpenAI
import json
import os

app = FastAPI()

# Configuración CORS: Permite que tu web en Vercel se comunique con este backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # En el futuro, cambiarás "*" por "https://tu-dominio.com"
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# El cliente asíncrono de OpenAI
client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

@app.post("/api/procesar-factura")
async def procesar_factura(archivo: UploadFile = File(...)):
    if not archivo.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    try:
        # 1. Leer archivo en memoria RAM
        contenido_pdf = await archivo.read()
        documento = fitz.open(stream=contenido_pdf, filetype="pdf")
        
        # Extraer todo el texto
        texto_extraido = "".join(pagina.get_text() for pagina in documento)
        documento.close()

        # 2. Instrucción estricta para el modelo LLM
        prompt = """
        Eres Vectia IA, un asistente experto en analizar facturas de servicios públicos en Colombia (Enel, Acueducto, EPM, Vanti, etc.).
        Extrae la información del texto proporcionado y devuélvela ÚNICAMENTE como un objeto JSON válido con esta estructura exacta:
        {
          "proveedor": "Nombre de la empresa",
          "valor_total": 0,
          "consumo": 0,
          "unidad_medida": "kWh o m3",
          "fecha_pago": "YYYY-MM-DD"
        }
        Si no encuentras algún dato, usa null. No devuelvas texto adicional ni comillas invertidas de markdown.
        """

        # 3. Consultar a GPT-4o-mini
        respuesta = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": texto_extraido}
            ],
            temperature=0.1
        )

        # 4. Retornar el JSON estructurado al frontend
        return json.loads(respuesta.choices[0].message.content)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
