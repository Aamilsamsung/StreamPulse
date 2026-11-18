FROM node:22-bookworm-slim AS jsruntime
FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/app/data/huggingface HF_HUB_DISABLE_XET=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-liberation && rm -rf /var/lib/apt/lists/*
COPY --from=jsruntime /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements-ai.txt 'yt-dlp[default]>=2025.11.1,<2028'
COPY backend ./backend
COPY frontend ./frontend
RUN mkdir -p /app/data && chown -R nobody:nogroup /app/data
USER nobody
EXPOSE 10000
CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
