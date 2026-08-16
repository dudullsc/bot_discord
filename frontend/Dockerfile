FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Painel importa bot.db do backend
COPY backend/requirements.txt /tmp/backend-requirements.txt
COPY frontend/requirements.txt /tmp/frontend-requirements.txt
RUN pip install --no-cache-dir \
      -r /tmp/backend-requirements.txt \
      -r /tmp/frontend-requirements.txt \
    && rm -f /tmp/backend-requirements.txt /tmp/frontend-requirements.txt

COPY backend/bot /app/backend/bot
COPY frontend/web /app/frontend/web

WORKDIR /app/frontend
ENV PYTHONPATH=/app/backend
ENV PYTHONUNBUFFERED=1
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "web"]
