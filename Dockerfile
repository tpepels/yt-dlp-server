FROM denoland/deno:bin AS deno_bin
FROM python:3.13-slim

ARG BGUTIL_VERSION=2.0.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HOME=/tmp/yt-dlp-home \
    XDG_CACHE_HOME=/tmp/yt-dlp-cache \
    DENO_DIR=/tmp/deno \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1 \
    BGUTIL_SERVER_HOME=/opt/bgutil-ytdlp-pot-provider/server \
    YOUTUBE_PLAYER_CLIENT=mweb

COPY --from=deno_bin /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${BGUTIL_VERSION}" \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
      /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && DENO_DIR=/opt/bgutil-deno-cache deno install --allow-scripts=npm:canvas --frozen \
    && chmod -R a+rX /opt/bgutil-ytdlp-pot-provider /opt/bgutil-deno-cache

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime is normally an arbitrary non-root UID/GID from Compose. Some Python
# packages (notably spotDL) create config/cache data under HOME on first import.
# Recreate these temp roots after package installation so no build-time root-owned
# directories can block the runtime user.
RUN rm -rf "$HOME" "$XDG_CACHE_HOME" "$DENO_DIR" \
    && mkdir -p "$HOME" "$XDG_CACHE_HOME" "$DENO_DIR" \
    && chmod 1777 "$HOME" "$XDG_CACHE_HOME" "$DENO_DIR"

COPY app.py staging.py ./
COPY tools ./tools
COPY templates ./templates

EXPOSE 4545

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4545/health', timeout=2)"

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:4545", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
