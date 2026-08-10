FROM denoland/deno:2.5.0 AS deno

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        jq \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        yt-dlp==2026.7.4 \
        bgutil-ytdlp-pot-provider==1.3.1 \
        yt-dlp-ejs==0.8.0

COPY --from=deno /usr/bin/deno /usr/bin/deno

COPY . /app
WORKDIR /app

ENV YT_INDEXER_HOST=0.0.0.0 \
    YT_QBT_HOST=0.0.0.0 \
    YT_QBT_DL_DIR=/data/downloads \
    YT_INDEXER_BASE_URL=http://localhost:9117

EXPOSE 9117 9177

VOLUME /data

ENTRYPOINT ["/app/docker-entrypoint.sh"]
