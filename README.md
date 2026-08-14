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
| `-P`  | Playlist/season mode: search for whole-season playlists instead of a single episode (skips the `-E` requirement; implies the playlists-only YouTube filter) |
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
| `EXPECTED_DURATION` | Expected episode runtime in seconds; when set, results are filtered to this ± `EP_DURATION_BUFFER` at the search stage so multi-episode compilations and non-episode content are excluded (default empty). Set by the indexer from TVDB/TVMaze runtimes |
| `EP_DURATION_BUFFER` | ± seconds around `EXPECTED_DURATION` (default 60); yt-dlp fetches more pages to keep the per-query count filled within the window |
| `RESOLVE_TOP`  | Number of top candidates to re-probe for resolution metadata (default 5, `0` disables) |
| `SEARCH_PARALLEL` | Number of YouTube queries run concurrently (default 3; set `1` for fully sequential execution). Applies to both single-episode and season-playlist searches |
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
When `EXPECTED_DURATION` is known the duration window already excluded anything
outside it, so duration no longer influences ranking (constant score); the
classic range score only applies in the fallback (no expected runtime).

### `indexer.py`

A minimal Torznab HTTP server that wraps `yt-episode-search.sh`. Python
standard library only — no third-party dependencies.

```sh
python3 indexer.py
```

**Endpoints**

- `?t=caps` — capabilities
- `?t=tvsearch&q=<show>&season=<n>&ep=<n>[&tvdbid=<id>]` — episode search
- `?t=tvsearch&q=<show>&season=<n>[&tvdbid=<id>]` — full-season search
  (interactive; see below)
- `?t=search&q=<term>` — generic search
- `?t=rss` — latest matches

**Full-season search (season packs)**

Sonarr sends `t=tvsearch&season=N` *without* `ep` for a whole-season search
(the `AddTvIdPageableRequests` / interactive "search for all episodes" flow).
`indexer.py` answers with **season packs**: it searches YouTube for playlists
matching `"<show> season N"` and returns each playlist as one release whose
title is `<Series> S0<N> WEB - <real playlist title>`, with the playlist's
video count folded into the size estimate. When `q` is missing, the series
name is resolved from `tvdbid` via TVMaze, matching the single-episode path.
When episode runtimes for the season are available (TVDB, else TVMaze), the
size is computed from the *summed* per-episode runtimes instead of
`1500 × playlist_count`, so the packaged size reflects how much content the
playlist actually carries.

Each season-pack enclosure is a magnet that carries the *playlist* URL (not a
single video) plus the TVDB series id so the download spoofer can map videos
to episodes:

```
magnet:?xt=urn:btih:<sha1(url)>&dn=<release title>&x.ytindexer=<base64url(playlist)>&x.ytindexertvdbid=<tvdbid>
```

`x.ytindexertvdbid` is present only on season-pack items. The single-episode
(`ep=<n>`) flow is unchanged.

**Episode-title lookup (TVMaze + TheTVDB)**

Sonarr only sends `q` + `season`/`ep` — never the episode title. When a
`tvdbid` is supplied (advertised in caps), `indexer.py` resolves the episode
title and passes it to the search script as `-t`, so title-only-named videos
(e.g. season-0 specials) surface with a `title_score`. It also resolves the
episode's **runtime** (minutes → seconds, TVDB first then the keyless TVMaze
fallback) and passes it as `EXPECTED_DURATION`, so the search window excludes
multi-episode compilations and clips. Unresolved runtimes leave the search
unfiltered. Lookups are cached in
memory + on disk (default TTL 7 days); any failure degrades gracefully to the
number-only search.

**Localized episode titles (languages)**

TVMaze has no episode translations, so localized titles come from **TheTVDB v4**
(the same source Sonarr uses) when `TVDB_API_KEY` is set — otherwise the lookup
falls back to TVMaze (English). Sonarr does not transmit a series' language
over Torznab, so the wanted language(s) are sent via the `language` (or `lang`)
query param, comma-separated ISO-639-1 codes (max **2**, e.g. `&language=fr,en`).
In Sonarr, add that to the indexer's **Additional parameters** field
(Settings → Indexers → the yt-indexarr indexer → Additional parameters).

The value must **start with `&`** — Sonarr (and Prowlarr) append it to the
search URL verbatim and validate it against the regex `(&.+?=.+?)+`, so
`language=fr,en` is rejected while `&language=fr,en` passes. If the indexer is
managed through **Prowlarr** and synced to Sonarr, set the same `&language=...`
value in Prowlarr (Settings → Indexers → this indexer → Advanced → Additional
parameters) instead — Prowlarr pushes that field to Sonarr on sync for Torznab.

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

`<link>`/`guid` keep the real YouTube URL; the Torznab `<comments>` element
carries the same URL so Sonarr's "More info" / the release title in interactive
search open the YouTube video (Sonarr's Torznab parser uses `<comments>` as the
info/comment URL and trims a trailing `#comments`).

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
download size is only known after yt-dlp runs. Setting `YT_CODEC` scales the
estimate to match the chosen codec (av1 baseline, vp9 ×1.4, h264 ×2.0).

**Video codec choice (`YT_CODEC`).** `auto` (default) keeps yt-dlp's stock
selection — for YouTube that is AV1 (`av01`) at every resolution. The explicit
choices `av1`, `vp9` and `h264` restrict downloads to that video codec and
fall back to the generic best format when the codec is unavailable, so a
download can never fail because of the preference. Known limits:

- **h264 caps at 1080p** — YouTube's public clients expose AVC (avc1) streams
  only up to 1080p. Requesting h264 for a 4K video silently downloads 1080p
  (no error). Use `av1`/`vp9` for full resolution.
- **SABR-restricted videos are 360p regardless** — such videos only expose a
  single 360p avc1 stream to yt-dlp until a GVS PO token unlocks high-res
  formats (see the SABR section below). Until then every codec choice yields
  360p.
- **No-ffmpeg hosts get 360p regardless** — without ffmpeg yt-dlp can only
  grab a single combined file, and YouTube serves one only at 360p. Any
  higher resolution needs the merge path, so install ffmpeg for quality.
- The `size` estimate reflects the codec multiplier, but the *actual* file
  can differ (e.g. a SABR 360p download is far smaller than its estimate).

The same `YT_CODEC` is read by both the indexer (size estimate) and the
download spoofer (format selection), so they stay consistent. There is no
h265/HEVC option because YouTube's public clients do not expose HEVC streams.

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
| `YT_INDEXER_HOST` | Bind host (default `0.0.0.0`) |
| `YT_INDEXER_PORT` | Bind port (default `9117`) |
| `YT_INDEXER_API_KEY` | API key (default `youtubeindexer`) |
| `YT_INDEXER_REQUIRE_KEY` | Enforce the API key: `1`/`0` (default `0` — auth off) |
| `YT_INDEXER_INDEXER_NAME` | Indexer name reported in caps (default `YouTube`) |
| `YT_INDEXER_BASE_URL` | Public base URL for self-references (default `http://localhost:9117`) |
| `YT_INDEXER_SCRIPT` | Path to the search script (default `./yt-episode-search.sh`) |
| `YT_INDEXER_FALLBACK_QUERY` | Query used when the series name can't be resolved (default `tv episode`) |
| `YT_INDEXER_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |
| `MIN_DURATION` | Passed through to the search script |
| `EXPECTED_DURATION` | Sets the search window the indexer would resolve automatically — only useful to override or force an unfiltered search with `0` (see the script env table) |
| `EP_DURATION_BUFFER` | Passed through to the search script (see the script env table) |
| `RESOLVE_TOP` | Passed through to the search script |
| `SEARCH_PARALLEL` | Passed through to the search script (see the script env table) |
| `PLAYER_CLIENT` | Passed through to the search script (see the script env table) |
| `POT_PROVIDER` | Passed through to the search script (see the script env table) |
| `YT_CODEC` | Video codec the downloader will use, `auto`/`av1`/`vp9`/`h264` (default `auto`). Also scales the reported Torznab `size` (av1 baseline, vp9 ×1.4, h264 ×2.0) so the size tracks what is downloaded. See the codec notes below |
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
| `YT_QBT_HOST` | Bind host (default `0.0.0.0`) |
| `YT_QBT_PORT` | Bind port (default `9177`) |
| `YT_QBT_USERNAME` | Login username (default `admin`) |
| `YT_QBT_PASSWORD` | Login password (default `adminadmin`) |
| `YT_QBT_REQUIRE_AUTH` | Enforce login: `1`/`0` (default `0` — auth off, like the indexer) |
| `YT_QBT_YTDLP` | `yt-dlp` binary (default `yt-dlp`) |
| `YT_QBT_PLAYER_CLIENT` | yt-dlp YouTube player client(s) for downloads, comma-separated fallback chain (default `tv_embedded,android_vr,web,tv_simply,android`) |
| `YT_QBT_POT_PROVIDER` | Optional GVS PO token provider base URL for downloads (e.g. `http://127.0.0.1:4416`); empty (default) disables. Appended as `--extractor-args youtubepot-bgutilhttp:base_url=`. See SABR note below. |
| `YT_QBT_DL_DIR` | Save path when the client sends none (default `~/downloads`) |
| `YT_QBT_EP_DURATION_BUFFER` | ± seconds around a mapped episode's runtime when checking whether a playlist video is that episode (default 60; mirrors the search script's `EP_DURATION_BUFFER`) |
| `YT_CODEC` | Video codec for downloads, `auto`/`av1`/`vp9`/`h264` (default `auto`). Falls back to best format when the codec is unavailable. See the codec notes in the indexer section. Shared with the indexer's size estimate |
| `YT_QBT_OUTPUT_EXT` | Output container for downloads, `mkv`/`mp4` (default `mkv`). `mkv` holds every codec YouTube serves (incl. av01/vp9 + opus), so the highest-quality stream flows through; `mp4` biases to h264/AAC-paired streams so direct-play stays universal (opaque to browsers/Apple) |
| `YT_QBT_MAX_PARALLEL` | Maximum concurrent yt-dlp downloads across all torrents (default 2). Extra torrents wait in a Sonarr-visible "Queued" state until a slot frees. Lower values throttle YouTube requests and reduce rate-limit / bot-check failures on rapid sequential episode downloads |
| `YT_QBT_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |

Each item (single video or season episode) is attempted through a small fallback
ladder: the configured best format, a plain retry, then `b[ext=mp4]/b`. This
handles SABR-only videos (see the SABR note) that list their highest format once
a PO token is presented but still refuse the stream fetch with HTTP 403 — the
final rung keeps such episodes at a working 360p instead of failing them.

Without `ffmpeg`, downloads use a single-file best format preferring `mp4`
(`b[ext=mp4]/b`) and the container cannot be changed, so `YT_QBT_OUTPUT_EXT`
only takes effect when ffmpeg is installed, where the merge step remuxes into
the requested container (`mkv` by default). The `YT_CODEC` preference narrows
the video stream to `av01`/`vp9`/`avc1` and always keeps a generic-best
fallback, so an unavailable codec downgrades quality rather than failing the
download.
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

**Season-pack downloads (playlists)**

When the added magnet's real URL is a YouTube *playlist* (from a full-season
search), `qbt.py` switches to season mode: it enumerates the playlist with
`yt-dlp --flat-playlist`, maps each video to an episode, and downloads one
file per episode as `<Series> S0<N>E0<M>.mkv` (or `.mp4` with
`YT_QBT_OUTPUT_EXT=mp4`) in the torrent's download folder.

Episode mapping prefers explicit `S03E24`-style tokens in the video title,
then season episode metadata (titles + per-episode runtimes) from **TheTVDB**
when `tvdbid` is present in the magnet and `TVDB_API_KEY` is configured,
falling back to the **keyless TVMaze** season lookup when TheTVDB is
unavailable (no key, lookup failure, or empty season map). Before mapping,
videos are skipped when they look like extras (BTS/trailers/interviews/
recaps/highlights/...), when nothing in the title matches any episode (≥0.6
fuzzy score), when the episode is already mapped, or when the video's
duration (from `--flat-playlist`) falls outside the episode's runtime ±
`YT_QBT_EP_DURATION_BUFFER` — so compilations, clips and bonus content in a
season playlist are never downloaded. Videos are mapped to episodes 1..N in
playlist order *only* when no season metadata can be obtained at all. The
mapping is the only part that touches the metadata APIs; a `TVDB_API_KEY` in
the qbt process is optional. Failed downloads are
logged and skipped; the torrent reports `uploading` when the whole season
finishes, and `torrents/files` lists every episode file. Single-video magnets
behave exactly as before.

**SABR-restricted videos.** Some videos (YouTube experiment, see
[yt-dlp#12482](https://github.com/yt-dlp/yt-dlp/issues/12482)) serve high-res
formats only over SABR: every client except `tv_simply` exposes just a 360p
format, and `tv_simply`'s high-res formats lack URLs until a GVS PO token is
presented. Without a PO provider such videos download at 360p (still
functional). To unlock their full quality, run the
[`brainicism/bgutil-ytdlp-pot-provider`](https://hub.docker.com/r/brainicism/bgutil-ytdlp-pot-provider)
container — the [`compose.yml`](./compose.yml) in this repo starts it by
default (it is also what makes the bundled deno/yt-dlp-ejs useful):

```sh
docker run -d --name bgutil-provider --init -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
```

Then set `YT_QBT_POT_PROVIDER=http://127.0.0.1:4416` (qbt.py) and
`POT_PROVIDER=http://127.0.0.1:4416` (search script). The host also needs the
`bgutil-ytdlp-pot-provider` yt-dlp plugin and a **Deno 2.3+** runtime with the
`yt-dlp-ejs` challenge scripts installed (deno is yt-dlp's preferred runtime;
node must be v22+). With both, `tv_simply` downloads the example SABR video
`zIoxr8k3rh0` at 1920x1080 instead of 360p. The provider stays optional — unset
the env vars (or drop the `pot` service) and behavior is unchanged, with such
videos downloading at 360p.

**Endpoint coverage**: `auth/login|logout`, `app/version|webapiVersion|buildInfo|preferences|shutdown`, `torrents/info|add|delete|pause|resume|recheck|reannounce|setShareLimits|topPrio|setCategory|properties|files|trackers|peers|categories|tags|createCategory|deleteCategory`, `sync/maindata`, `log/main`. Categories are kept in memory (`torrents/categories`), so Sonarr's category check/create passes. `/api/v2/app/preferences` reports `dht: true` and `queueing_enabled: true`, which Sonarr requires before it will accept a trackerless magnet and non-default priorities.

## Docker

A prebuilt image is published to **GHCR** on every push to `main` (and on
`v*` tags), for `linux/amd64` and `linux/arm64`:

```
ghcr.io/fosserglitch902/yt-indexarr
```

The container runs **both** the indexer (`9117`) and the download spoofer
(`9177`) from a single entrypoint, and exits if either process dies so the
orchestrator can restart it.

The image ships the `yt-dlp-ejs` challenge scripts, the
`bgutil-ytdlp-pot-provider` plugin, and the Deno JS runtime, so the PO-token
path works out of the box — no host-side installs. It is inert unless
`YT_QBT_POT_PROVIDER`/`POT_PROVIDER` point at a running provider.

```sh
docker run -d \
  --name yt-indexarr \
  -p 9117:9117 \
  -p 9177:9177 \
  -v yt-indexarr-data:/data \
  ghcr.io/fosserglitch902/yt-indexarr:latest
```

```yaml
# docker-compose.yml (see ./compose.yml in this repo for the full file)
services:
  yt-indexarr:
    image: ghcr.io/fosserglitch902/yt-indexarr:latest
    container_name: yt-indexarr
    restart: unless-stopped
    ports:
      - "9117:9117"   # Torznab indexer
      - "9177:9177"   # qBittorrent download spoofer
    volumes:
      - yt-indexarr-data:/data   # downloads (default /data/downloads)
    environment:
      - YT_INDEXER_HOST=0.0.0.0
      - YT_INDEXER_PORT=9117
      - YT_INDEXER_API_KEY=youtubeindexer
      - YT_INDEXER_REQUIRE_KEY=0
      - YT_INDEXER_INDEXER_NAME=YouTube
      - YT_INDEXER_BASE_URL=http://localhost:9117
      - YT_QBT_HOST=0.0.0.0
      - YT_QBT_PORT=9177
      - YT_QBT_USERNAME=admin
      - YT_QBT_PASSWORD=change-me
      - YT_QBT_REQUIRE_AUTH=0
      - YT_QBT_DL_DIR=/data/downloads
      - YT_QBT_POT_PROVIDER=http://pot:4416
      - POT_PROVIDER=http://pot:4416
      # - TVDB_API_KEY=your-thetvdb-key   # localized episode titles
      # - TVDB_PIN=your-pin               # only for user-supported keys
  # GVS PO token provider for SABR-restricted videos (see above). Disable by
  # removing it and clearing the two POT_PROVIDER vars above.
  pot:
    image: brainicism/bgutil-ytdlp-pot-provider
    container_name: bgutil-provider
    restart: unless-stopped
    ports:
      - "4416:4416"
volumes:
  yt-indexarr-data:
```

All environment variables from the tables above apply unchanged; both services
already bind `0.0.0.0` by default. The image additionally sets
`YT_QBT_DL_DIR=/data/downloads` and `YT_INDEXER_BASE_URL=http://localhost:9117`
(override the base URL to your host's reachable address so Prowlarr/Sonarr see
a stable indexer URL). The [`compose.yml`](./compose.yml) in this repo lists
every supported variable.

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

### Suggested Sonarr configuration

This indexer only ever returns YouTube videos, and every search runs one or more
`yt-dlp` invocations — so treat it as a *manual, targeted* source, not something
Sonarr should poll in the background:

- **Indexer — sync profile: Interactive Search only.** In Sonarr
  (Settings → Indexers → this indexer), enable **Interactive Search** and
  disable **RSS** and **Automatic Search**. In Prowlarr, set the indexer's
  **sync profile** the same way (or mark the app sync as manual-only). This
  keeps Sonarr from auto-grabbing and from polling the indexer on every
  episode's RSS/automatic pass, which would run yt-dlp for nothing.
- **Use it only with this repo's downloader.** The enclosures are fake magnets
  that only the included qBittorrent spoofer (`qbt.py`, `:9177`) can decode —
  a real download client cannot fetch them. Only add this indexer where the
  spoofer is configured as the download client; don't point it at a separate
  torrent/Usenet setup.
- **Downloader — lower priority than the main one.** Add the spoofer as a
  **qBittorrent** Download Client in Sonarr (Settings → Download Clients) at
  `<host>:9177` with the spoofer's `YT_QBT_USERNAME`/`YT_QBT_PASSWORD`. Set its
  **Priority** to a lower value than your main download client (priority range
  is **1–50, where 1 is the default/highest and 50 is the lowest**), e.g.
  `20`. Your main client keeps handling normal releases; the spoofer only picks
  up the YouTube grabs that fall through to it.
