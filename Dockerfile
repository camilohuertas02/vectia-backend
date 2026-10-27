FROM python:3.11-slim

# Evita archivos .pyc y fuerza logs en tiempo real para Cloud Run
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

# Dependencias del sistema ligeras para PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Google Cloud Run inyecta la variable $PORT (por defecto 8080).
# Usamos Gunicorn con 2 workers Uvicorn y timeout amplio (120s) para PDFs pesados.
CMD exec gunicorn main:app \
    --bind 0.0.0.0:${PORT} \
    --workers 2 \
    --worker-class uvicorn.workers.UvicornWorker \
    --timeout 120 \
    --graceful-timeout 30 \
    --max-requests 200 \
    --max-requests-jitter 20
