FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir 'faster-whisper>=1.0,<2'
COPY backend ./backend
COPY frontend ./frontend
RUN mkdir -p /app/data && chown -R nobody:nogroup /app/data
USER nobody
EXPOSE 10000
CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
