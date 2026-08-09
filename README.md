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
| `-j`  | Emit JSONL (used by the indexer) |
| `-h`  | Help |

**Environment**

| Variable | Meaning |
| -------- | ------- |
| `MIN_DURATION` | Minimum video length in seconds (default 300) |
| `RESOLVE_TOP`  | Number of top candidates to re-probe for resolution metadata (default 5, `0` disables) |

**Output fields** (JSONL, `-j`): `score`, `probable`, `normalized_title`,
`id`, `title`, `url`, `duration`, `timestamp`, `channel`, `views`, per-factor
scores, `queries`, plus `resolution` (e.g. `1080p`) and `pub_date` (RFC 2822)
added by the quality pass. When `-t` is given, the episode title is folded
into the normalized title (`Bluey S03E38 Cubby WEB`); when resolution is
known it is appended (`Bluey S03E38 Cubby WEB 1080p`).

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

**Episode-title lookup (TVMaze)**

Sonarr only sends `q` + `season`/`ep` — never the episode title. When a
`tvdbid` is supplied (advertised in caps), `indexer.py` resolves the episode
title via TVMaze and passes it to the search script as `-t`, so
title-only-named videos (e.g. season-0 specials) surface with a `title_score`.
Lookups are cached in memory + on disk (default TTL 7 days); any failure
degrades gracefully to the number-only search.

**Release enclosures (magnet carrier)**

Each `<enclosure>` is a magnet that encodes the release title and the real
YouTube URL — the contract for the download-client spoof in this monorepo:

```
magnet:?xt=urn:btih:<sha1(url)>&dn=<release title>&x.ytindexer=<base64url(url)>
```

- `dn` — the release title (used as the downloaded file name, Sonarr-parseable)
- `x.ytindexer` — base64url of the real `https://www.youtube.com/watch?v=...`

`<link>`/`guid` keep the real YouTube URL for human use.

**Authentication**

Requests must include the API key, either as `?apikey=...` (or `?key=...`)
query parameter or as a Bearer token in the `Authorization` header. Default key:
`youtubeindexer` (set `YT_INDEXER_API_KEY` to change it).

**Environment**

| Variable | Meaning |
| -------- | ------- |
| `YT_INDEXER_HOST` | Bind host (default `127.0.0.1`) |
| `YT_INDEXER_PORT` | Bind port (default `9117`) |
| `YT_INDEXER_API_KEY` | API key (default `youtubeindexer`) |
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

## Setup with Prowlarr / Sonarr

1. Add a **Generic Torznab** indexer in Prowlarr.
2. URL: `http://<host>:9117/torznab`
3. API key: `youtubeindexer` (or whatever `YT_INDEXER_API_KEY` is set to).
4. Run: `setsid python3 /youtube-indexer/indexer.py >/tmp/indexer.log 2>&1 < /dev/null &`

Each search invokes `yt-dlp`, so searches take a few seconds. Caching/Prowlarr
sync helps keep the load down.
