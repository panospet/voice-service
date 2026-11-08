FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PYTHONUNBUFFERED=1 \
    VOICE_BIND=127.0.0.1 \
    VOICE_PORT=8002

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import os, urllib.request; port = os.getenv('VOICE_PORT', '8002'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz')" || exit 1

CMD ["python", "app.py"]
