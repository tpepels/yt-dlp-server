FROM denoland/deno:bin AS deno_bin
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1

COPY --from=deno_bin /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

EXPOSE 4545

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4545/health', timeout=2)"

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:4545", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
