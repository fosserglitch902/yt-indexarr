FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        jq \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir yt-dlp

COPY . /app
WORKDIR /app

ENV YT_INDEXER_HOST=0.0.0.0 \
    YT_QBT_HOST=0.0.0.0 \
    YT_QBT_DL_DIR=/data/downloads \
    YT_INDEXER_BASE_URL=http://localhost:9117

EXPOSE 9117 9177

VOLUME /data

ENTRYPOINT ["/app/docker-entrypoint.sh"]
