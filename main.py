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

        # 2. Prompt estricto y adaptado a la base de datos relacional
        prompt = """
        Eres Vectia IA, un motor experto en extracción estructurada de facturas de servicios públicos en Colombia (energía, acueducto, gas).
        Analiza visualmente todas las páginas de la factura adjunta y extrae la información requerida.
        
        REGLAS ESTRICTAS DE EXTRACCIÓN:
        1. Formato numérico: Extrae valores monetarios y de consumo como números flotantes o enteros puros (sin símbolos $, sin puntos de mil, usa punto únicamente para decimales si aplica). Ejemplo: si dice "$123.830", devuelve 123830.00.
        2. Formato de fechas: Convierte todas las fechas al formato estricto ISO "YYYY-MM-DD". Traduce meses en texto (ej. "03 JUL/2026" -> "2026-07-03"). Si no existe el año, asume el año del periodo.
        3. Clasificación: "tipo_servicio" debe ser normalizado en minúsculas: "energia", "agua" o "gas".
        4. Multimedidor: "medidores" debe ser SIEMPRE un arreglo (lista), incluso si la factura solo tiene un medidor.
        5. Detalles adicionales: 
           - En "factura.detalles_adicionales", extrae en un JSON rubros como: cobro_aseo, estrato, subsidio, contribucion, alumbrado_publico, intereses_mora, etc.
           - En "medidores.detalles_adicionales", incluye datos técnicos propios del medidor si existen: energia_reactiva, factor_multiplicador, lectura_actual, lectura_anterior.
        6. Devuelve ÚNICAMENTE un JSON válido con la siguiente estructura.

        ESTRUCTURA JSON REQUERIDA:
        {
          "factura": {
            "tipo_servicio": "energia",
            "proveedor": "string (ej. Enel Colombia, Acueducto de Bogotá, Vanti)",
            "numero_factura": "string",
            "numero_cliente": "string (cuenta contrato, número de cliente o referencia de pago)",
            "valor_total": 0.0,
            "periodo_inicio": "YYYY-MM-DD o null",
            "periodo_fin": "YYYY-MM-DD o null",
            "fecha_vencimiento": "YYYY-MM-DD o null",
            "estado_pago": "pendiente",
            "detalles_adicionales": {}
          },
          "medidores": [
            {
              "numero_medidor": "string con el serial",
              "consumo": 0.0,
              "unidad_medida": "kWh",
              "detalles_adicionales": {}
            }
          ]
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
        
        # 4.1 Insertar la tabla padre (Facturas)
        datos_factura = datos_extraidos["factura"]
        datos_factura["id_usuario"] = id_usuario

        resultado_factura = supabase.table("facturas").insert(datos_factura).execute()
        
        if not resultado_factura.data:
            raise HTTPException(status_code=500, detail="Error al insertar factura en BD")
            
        id_factura_generado = resultado_factura.data[0]["id"]

        # 4.2 Insertar la tabla hija (Consumos Medidores)
        lista_medidores = datos_extraidos.get("medidores", [])
        
        if lista_medidores:
            # Asignar el ID de la factura a cada medidor extraído
            for medidor in lista_medidores:
                medidor["id_factura"] = id_factura_generado

            # Supabase permite la inserción de listas de diccionarios en bloque (Batch Insert)
            resultado_medidores = supabase.table("consumos_medidores").insert(lista_medidores).execute()
            
            if not resultado_medidores.data:
                # Opcional: Podrías hacer un rollback manual borrando la factura si fallan los medidores
                print("Advertencia: No se pudieron insertar los medidores asociados.")

        # 5. Retornar confirmación
        return {
            "status": "success",
            "mensaje": f"Factura procesada y guardada correctamente con {len(lista_medidores)} medidor(es).",
            "id_factura": id_factura_generado
        }

    except Exception as e:
        print(f"Error procesando factura: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
