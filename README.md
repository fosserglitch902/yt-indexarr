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

**Torznab attributes** per item: `seeders`/`peers` (views), `size`,
`resolution` (e.g. `1080p`, only when probed), `source` (`web`), and
`language` (audio ISO code, e.g. `en`, only when probed). Items past the
`RESOLVE_TOP` probe get no `resolution`/`language` attrs.

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
rejected.

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
| `YT_QBT_REQUIRE_AUTH` | Enforce login: `1`/`0` (default `1`) |
| `YT_QBT_YTDLP` | `yt-dlp` binary (default `yt-dlp`) |
| `YT_QBT_DL_DIR` | Save path when the client sends none (default `~/downloads`) |
| `YT_QBT_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |

Without `ffmpeg`, downloads use a single-file best format preferring `mp4`
(`b[ext=mp4]/b`); with `ffmpeg` installed it merges best video+audio into mp4.

**Endpoint coverage**: `auth/login|logout`, `app/version|webapiVersion|buildInfo|preferences|shutdown`, `torrents/info|add|delete|pause|resume|recheck|reannounce|setShareLimits|setCategory|properties|files|trackers|peers|categories|tags|createCategory|deleteCategory`, `sync/maindata`, `log/main`.

## Setup with Prowlarr / Sonarr

1. Add a **Generic Torznab** indexer in Prowlarr.
2. URL: `http://<host>:9117/torznab`
3. API key: leave blank (auth is off by default). If you set
   `YT_INDEXER_REQUIRE_KEY=1`, enter `youtubeindexer` (or your
   `YT_INDEXER_API_KEY`).
4. Run: `setsid python3 /youtube-indexer/indexer.py >/tmp/indexer.log 2>&1 < /dev/null &`

Each search invokes `yt-dlp`, so searches take a few seconds. Caching/Prowlarr
sync helps keep the load down.
