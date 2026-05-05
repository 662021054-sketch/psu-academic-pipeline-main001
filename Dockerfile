FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY scripts/ ./scripts/
COPY config.yaml .

CMD ["sh", "-c", "python scripts/trigger_server.py & python scripts/watcher.py"]