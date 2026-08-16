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

# Persisted state lives under /data/config (config.json, history.json,
# cookies.txt, API caches) so any deployment that mounts /data/config survives
# container redeploys even when its compose omits these env vars. Downloads go
# to YT_QBT_DL_DIR (default /data/downloads). Do not rely on /data being a
# volume on its own; see the compose file for the two-volume layout.
ENV YT_INDEXER_HOST=0.0.0.0 \
    YT_QBT_HOST=0.0.0.0 \
    YT_QBT_DL_DIR=/data/downloads \
    YT_INDEXER_BASE_URL=http://localhost:9117 \
    YT_QBT_HISTORY_FILE=/data/config/history.json \
    YT_UI_CONFIG_FILE=/data/config/config.json \
    YT_QBT_COOKIES=/data/config/cookies.txt \
    TVMAZE_CACHE_FILE=/data/config/cache/tvmaze.json \
    TVDB_TOKEN_FILE=/data/config/cache/tvdb_token.json \
    TVDB_CACHE_FILE=/data/config/cache/tvdb.json

EXPOSE 9117 9177

ENTRYPOINT ["/app/docker-entrypoint.sh"]
