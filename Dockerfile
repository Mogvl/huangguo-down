# HGXZ — 黄果短剧下载器（NAS 适配镜像）
#
# Multi-arch on purpose: UGREEN / Synology / QNAP boxes ship x86_64 or ARM64
# depending on the model, so the image is built for both instead of hard-coding
# linux/amd64 like the upstream template did.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DAEMON=true \
    SCHEDULE=03:30 \
    HGXZ_CONFIG=/config/config.json \
    HGXZ_MEDIA_ROOT=/media \
    HGXZ_STATE_DB=/data/state.sqlite3 \
    HGXZ_LOG_DIR=/logs \
    HGXZ_PORT=7731 \
    HGXZ_API_PORT=7732 \
    UMASK=022

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tzdata \
        openssl \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY HGXZ /app/HGXZ
COPY config.example.json /app/config.example.json
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Volumes are declared so the NAS GUI shows them; the compose file still has to
# map real host paths.
VOLUME ["/config", "/data", "/logs", "/media"]
EXPOSE 7731 7732

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HGXZ_PORT:-7731}/healthz" || exit 1

# tini reaps zombies: ffmpeg/openssl are spawned as subprocesses on every
# segment, and a PID 1 that does not reap will leak them over a long run.
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["--daemon"]
