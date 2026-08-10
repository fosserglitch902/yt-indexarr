# YouTube Indexer

Index and find YouTube videos matching a specific TV episode, exposed to
*arr apps as a Torznab indexer.

## Scripts

### `yt-episode-search.sh`

Search YouTube for a specific episode of a series and rank the results by how
likely each video is the actual full episode.

```sh
./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave"
```

Requires `yt-dlp` and `jq`. See `./yt-episode-search.sh -h` for all options.

**Options**

| Flag | Meaning |
| ---- | ------- |
| `-s`  | Series / show name (required) |
| `-S`  | Season number |
| `-E`  | Episode number |
| `-t`  | Episode title |
| `-n`  | Max results to return (default 20) |
| `-p`  | Results per yt-dlp query (default 5) |
| `-d`  | Delay between queries in seconds (default 1) |
| `-o`  | Download directory (with `-D`) |
| `-D`  | Download the best candidate |
| `-b`  | Print only the best candidate URL |
| `-t`  | Episode title; repeatable for localized/alternate titles |
| `-j`  | Emit JSONL (used by the indexer) |
| `-h`  | Help |

**Environment**

| Variable | Meaning |
| -------- | ------- |
| `MIN_DURATION` | Minimum video length in seconds (default 300) |
| `RESOLVE_TOP`  | Number of top candidates to re-probe for resolution metadata (default 5, `0` disables) |
| `PLAYER_CLIENT` | yt-dlp YouTube player client(s), comma-separated fallback chain (default `tv_embedded,android_vr,web,tv_simply,android`; set `""` to use yt-dlp's default) |
| `POT_PROVIDER` | Optional GVS PO token provider base URL (e.g. `http://127.0.0.1:4416`); passed to the probe as `--extractor-args youtubepot-bgutilhttp:base_url=`. Empty (default) disables. Needed to unlock high-res formats on SABR-forced videos (see below). Requires the provider plugin + a Deno 2.3+ runtime with the `yt-dlp-ejs` scripts installed on the host running the probe. |

**Output fields** (JSONL, `-j`): `score`, `probable`, `has_episode`,
`normalized_title`, `id`, `title`, `url`, `duration`, `timestamp`, `channel`,
`views`, per-factor scores, `queries`, plus `resolution` (e.g. `1080p`),
`language` (audio ISO code, e.g. `en`) and `pub_date` (RFC 2822) added by the
quality pass. When `-t` is given, the episode title is folded into the
normalized title (`Bluey S03E38 Cubby WEB`); when resolution is known it is
appended (`Bluey S03E38 Cubby WEB 1080p`).

**Ranking**: results are sorted by tier first — candidates whose video title
carries a season/episode token (`has_episode`) always sort above title-only
candidates — then by score. The episode title acts as a boost *within* a tier,
not a substitute for the episode number. Title-only candidates (e.g. season-0
specials named only by episode title) are still returned, just ranked below.

### `indexer.py`

A minimal Torznab HTTP server that wraps `yt-episode-search.sh`. Python
standard library only — no third-party dependencies.

```sh
python3 indexer.py
```

**Endpoints**

- `?t=caps` — capabilities
- `?t=tvsearch&q=<show>&season=<n>&ep=<n>[&tvdbid=<id>]` — episode search
- `?t=search&q=<term>` — generic search
- `?t=rss` — latest matches

**Episode-title lookup (TVMaze + TheTVDB)**

Sonarr only sends `q` + `season`/`ep` — never the episode title. When a
`tvdbid` is supplied (advertised in caps), `indexer.py` resolves the episode
title and passes it to the search script as `-t`, so title-only-named videos
(e.g. season-0 specials) surface with a `title_score`. Lookups are cached in
memory + on disk (default TTL 7 days); any failure degrades gracefully to the
number-only search.

**Localized episode titles (languages)**

TVMaze has no episode translations, so localized titles come from **TheTVDB v4**
(the same source Sonarr uses) when `TVDB_API_KEY` is set — otherwise the lookup
falls back to TVMaze (English). Sonarr does not transmit a series' language
over Torznab, so the wanted language(s) are sent via the `language` (or `lang`)
query param, comma-separated ISO-639-1 codes (max **2**, e.g. `language=fr,en`).
In Sonarr, add that to the indexer's **Additional parameters** field
(Settings → Indexers → the yt-indexarr indexer → Additional parameters).

Titles are matched in the requested order plus English as a safety net; the
search script accepts repeated `-t` flags and scores against any of them. If
`TVDB_API_KEY` is absent, a `language` param is ignored (English/TVMaze only).

**Release enclosures (magnet carrier)**

Each `<enclosure>` is a magnet that encodes the release title and the real
YouTube URL — the contract for the download-client spoof in this monorepo:

```
magnet:?xt=urn:btih:<sha1(url)>&dn=<release title>&x.ytindexer=<base64url(url)>
```

- `dn` — the release title (used as the downloaded file name, Sonarr-parseable)
- `x.ytindexer` — base64url of the real `https://www.youtube.com/watch?v=...`

`<link>`/`guid` keep the real YouTube URL for human use.

The Torznab `<title>` is the rebuilt, Sonarr-parseable name (`Bluey S03E24
Faceytalk WEB`) with the **real YouTube title appended** after ` - ` so it is
visible in Sonarr's interactive search (Sonarr's release parser needs the
season/episode token in the title to associate the result with the episode;
without it the release is rejected as "unable to identify correct episode").
The magnet `dn` keeps only the short parseable name so downloaded filenames
stay clean. The real YouTube title is also sent as `<description>`.

**Torznab attributes** per item: `seeders`/`peers` (views), `size`,
`resolution` (e.g. `1080p`, only when probed), `source` (`web`), and
`language` (audio ISO code, e.g. `en`, only when probed). Items past the
`RESOLVE_TOP` probe get no `resolution`/`language` attrs. `size` is a
resolution-aware estimate of the resulting file (bitrate per quality tier,
e.g. ~0.8 Mbps at 360p up to ~12 Mbps at 2160p, plus audio), since the real
download size is only known after yt-dlp runs.

**Authentication**

Auth is **optional by default** — requests are accepted without a key, so a
Prowlarr "Generic Torznab" indexer needs nothing beyond the URL. Set
`YT_INDEXER_REQUIRE_KEY=1` to enforce the API key on every request; when
enforced, requests must include it as `?apikey=...` (or `?key=...`). Default
key: `youtubeindexer` (set `YT_INDEXER_API_KEY` to change it).

> Only run without a key on a trusted network — with auth off, the indexer is
> open to anyone who can reach the port.

**Environment**

| Variable | Meaning |
| -------- | ------- |
| `YT_INDEXER_HOST` | Bind host (default `127.0.0.1`) |
| `YT_INDEXER_PORT` | Bind port (default `9117`) |
| `YT_INDEXER_API_KEY` | API key (default `youtubeindexer`) |
| `YT_INDEXER_REQUIRE_KEY` | Enforce the API key: `1`/`0` (default `0` — auth off) |
| `YT_INDEXER_NAME` | Indexer name reported in caps (default `yt-indexarr`) |
| `YT_INDEXER_BASE_URL` | Public base URL for self-references (default from request) |
| `YT_INDEXER_SCRIPT` | Path to the search script (default `./yt-episode-search.sh`) |
| `YT_INDEXER_FALLBACK_QUERY` | Query to use when tvsearch gets no episode (default `<show> season <n>`) |
| `YT_INDEXER_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |
| `MIN_DURATION` | Passed through to the search script |
| `RESOLVE_TOP` | Passed through to the search script |
| `TVMAZE_API` | TVMaze base URL (default `https://api.tvmaze.com`) |
| `TVMAZE_TIMEOUT` | Per-lookup timeout seconds (default 5) |
| `TVMAZE_CACHE_FILE` | Disk cache path (default `~/.cache/yt-indexarr/tvmaze.json`) |
| `TVMAZE_CACHE_TTL` | Cache TTL seconds (default 7 days) |
| `TVDB_API_KEY` | TheTVDB v4 API key (enables localized episode titles) |
| `TVDB_PIN` | TheTVDB subscriber PIN (only for user-supported keys) |
| `TVDB_API` | TheTVDB v4 base URL (default `https://api4.thetvdb.com/v4`) |
| `TVDB_TIMEOUT` | Per-lookup timeout seconds (default 5) |
| `TVDB_TOKEN_FILE` | Token cache path (default `~/.cache/yt-indexarr/tvdb_token.json`) |
| `TVDB_CACHE_FILE` | Disk cache path (default `~/.cache/yt-indexarr/tvdb.json`) |
| `TVDB_CACHE_TTL` | Cache TTL seconds (default 7 days) |

### `qbt.py` — qBittorrent-compatible download spoofer

A minimal qBittorrent Web API v2 server that pretends to be a torrent client and
downloads the video with `yt-dlp` instead. Sonarr connects to it as a normal
qBittorrent Download Client; adding a torrent decodes the magnet-carrier
enclosure from the indexer (`x.ytindexer` → real YouTube URL, `dn` → release
title) and runs `yt-dlp` on it, faking torrent progress/state so Sonarr can
track, import and later delete the file. Magents without `x.ytindexer` are
rejected. Auth is off by default (`YT_QBT_REQUIRE_AUTH=0`), so Sonarr can
connect without credentials — like the indexer, only run without auth on a
trusted network. If credentials are configured, login still works and issues
a session cookie.

```sh
python3 qbt.py
```

Run it on its **own port** (default `9177`) — separate from the indexer on
`9117`.

**Environment**

| Variable | Meaning |
| -------- | ------- |
| `YT_QBT_HOST` | Bind host (default `127.0.0.1`) |
| `YT_QBT_PORT` | Bind port (default `9177`) |
| `YT_QBT_USERNAME` | Login username (default `admin`) |
| `YT_QBT_PASSWORD` | Login password (default `adminadmin`) |
| `YT_QBT_REQUIRE_AUTH` | Enforce login: `1`/`0` (default `0` — auth off, like the indexer) |
| `YT_QBT_YTDLP` | `yt-dlp` binary (default `yt-dlp`) |
| `YT_QBT_PLAYER_CLIENT` | yt-dlp YouTube player client(s) for downloads, comma-separated fallback chain (default `tv_embedded,android_vr,web,tv_simply,android`) |
| `YT_QBT_POT_PROVIDER` | Optional GVS PO token provider base URL for downloads (e.g. `http://127.0.0.1:4416`); empty (default) disables. Appended as `--extractor-args youtubepot-bgutilhttp:base_url=`. See SABR note below. |
| `YT_QBT_DL_DIR` | Save path when the client sends none (default `~/downloads`) |
| `YT_QBT_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |

Without `ffmpeg`, downloads use a single-file best format preferring `mp4`
(`b[ext=mp4]/b`); with `ffmpeg` installed it merges best video+audio into mp4.
YouTube blocks some videos on yt-dlp's default player client (reported as
"This video is not available"), so downloads and the search script's quality
probe use a client fallback chain by default — high-resolution clients first
(`tv_embedded`, `android_vr`, `web`, `tv_simply`), with `android` as the last
resort that unblocks otherwise-DRM/blocked videos at up to 360p. This way Sonarr
gets the highest quality a video offers (e.g. 1080p/4K) where available, while
still downloading videos that only the `android` client can serve. Set
`YT_QBT_PLAYER_CLIENT`/`PLAYER_CLIENT` to a single client or to empty for
yt-dlp's default. The script probes download URLs anyway, so even results that
get a quality tag during search are downloaded at max quality regardless of
what was reported.

**SABR-restricted videos.** Some videos (YouTube experiment, see
[yt-dlp#12482](https://github.com/yt-dlp/yt-dlp/issues/12482)) serve high-res
formats only over SABR: every client except `tv_simply` exposes just a 360p
format, and `tv_simply`'s high-res formats lack URLs until a GVS PO token is
presented. Without a PO provider such videos download at 360p (still
functional). To unlock their full quality, run the optional
[`brainicism/bgutil-ytdlp-pot-provider`](https://hub.docker.com/r/brainicism/bgutil-ytdlp-pot-provider)
container:

```sh
docker run -d --name bgutil-provider --init -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
```

Then set `YT_QBT_POT_PROVIDER=http://127.0.0.1:4416` (qbt.py) and
`POT_PROVIDER=http://127.0.0.1:4416` (search script). The host also needs the
`bgutil-ytdlp-pot-provider` yt-dlp plugin and a **Deno 2.3+** runtime with the
`yt-dlp-ejs` challenge scripts installed (deno is yt-dlp's preferred runtime;
node must be v22+). With both, `tv_simply` downloads the example SABR video
`zIoxr8k3rh0` at 1920x1080 instead of 360p. The provider stays fully optional —
unset env vars leave current behavior unchanged.

**Endpoint coverage**: `auth/login|logout`, `app/version|webapiVersion|buildInfo|preferences|shutdown`, `torrents/info|add|delete|pause|resume|recheck|reannounce|setShareLimits|topPrio|setCategory|properties|files|trackers|peers|categories|tags|createCategory|deleteCategory`, `sync/maindata`, `log/main`. Categories are kept in memory (`torrents/categories`), so Sonarr's category check/create passes. `/api/v2/app/preferences` reports `dht: true` and `queueing_enabled: true`, which Sonarr requires before it will accept a trackerless magnet and non-default priorities.

## Docker

A prebuilt image is published to **GHCR** on every push to `main` (and on
`v*` tags), for `linux/amd64` and `linux/arm64`:

```
ghcr.io/fosser-glitch/yt-indexarr
```

The container runs **both** the indexer (`9117`) and the download spoofer
(`9177`) from a single entrypoint, and exits if either process dies so the
orchestrator can restart it.

```sh
docker run -d \
  --name yt-indexarr \
  -p 9117:9117 \
  -p 9177:9177 \
  -v yt-indexarr-data:/data \
  ghcr.io/fosser-glitch/yt-indexarr:latest
```

```yaml
# docker-compose.yml
services:
  yt-indexarr:
    image: ghcr.io/fosser-glitch/yt-indexarr:latest
    container_name: yt-indexarr
    restart: unless-stopped
    ports:
      - "9117:9117"   # Torznab indexer
      - "9177:9177"   # qBittorrent download spoofer
    volumes:
      - yt-indexarr-data:/data   # downloads (default /data/downloads)
    environment:
      - YT_QBT_USERNAME=admin
      - YT_QBT_PASSWORD=change-me
      # - TVDB_API_KEY=your-thetvdb-key
      # - TVDB_PIN=your-pin   # only for user-supported keys
      # - YT_INDEXER_BASE_URL=http://192.168.1.50:9117
      # - YT_QBT_POT_PROVIDER=http://pot:4416   # optional SABR high-res unlock
      # - YT_QBT_PLAYER_CLIENT=tv_embedded,android_vr,web,tv_simply,android
  # Optional: GVS PO token provider for SABR-restricted videos (see above).
  # Enable with: docker compose --profile pot up -d
  pot:
    image: brainicism/bgutil-ytdlp-pot-provider
    container_name: bgutil-provider
    restart: unless-stopped
    profiles: ["pot"]
    ports:
      - "4416:4416"
```

All environment variables from the tables above apply unchanged; the image
only changes the bind defaults to `0.0.0.0` and sets
`YT_QBT_DL_DIR=/data/downloads` and
`YT_INDEXER_BASE_URL=http://localhost:9117` (override the base URL to your
host's reachable address so Prowlarr/Sonarr see a stable indexer URL).

Point Prowlarr at `http://<host>:9117/torznab` (blank API key by default) and
add a **qBittorrent** Download Client in Sonarr at `<host>:9177` with the
spoofer's `YT_QBT_USERNAME`/`YT_QBT_PASSWORD`.

## Setup with Prowlarr / Sonarr

1. Add a **Generic Torznab** indexer in Prowlarr.
2. URL: `http://<host>:9117/torznab`
3. API key: leave blank (auth is off by default). If you set
   `YT_INDEXER_REQUIRE_KEY=1`, enter `youtubeindexer` (or your
   `YT_INDEXER_API_KEY`).
4. Run: `setsid python3 /youtube-indexer/indexer.py >/tmp/indexer.log 2>&1 < /dev/null &`

Each search invokes `yt-dlp`, so searches take a few seconds. Caching/Prowlarr
sync helps keep the load down.
