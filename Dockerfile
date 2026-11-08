FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PYTHONUNBUFFERED=1 \
    VOICE_BIND=127.0.0.1 \
    VOICE_PORT=8002

EXPOSE 8002

CMD ["python", "app.py"]
