# yt-indexarr

Find TV episodes on YouTube and download them through your *arr stack. Two
services ship in one container:

- **Indexer** (`indexer.py`) — a Torznab server that searches YouTube for the
  episode Sonarr/Prowlarr asks for, ranks results by how likely each video is
  the full episode, and returns Sonarr-parseable releases.
- **Downloader** (`qbt.py`) — a qBittorrent-compatible spoofer that decodes the
  indexer's releases and grabs the real video with `yt-dlp` at the best
  available quality, reporting progress/state to Sonarr like a normal torrent
  client.

## Features

- **Episode search** — matches by series + season/episode, including
  title-only-named specials; results are ranked by episode-number tokens first,
  then title match, with a duration window that excludes compilations.
- **Season packs** — whole-season searches return YouTube playlists as single
  releases, sized from real episode runtimes, with quality and view counts
  probed from the playlist's first video.
- **Title metadata** — episode titles and runtimes are resolved automatically
  (keyless TVMaze, plus TheTVDB for localized titles) so searches match and
  filter precisely.
- **Best-quality downloads** — codec and container preferences, a player-client
  + PO-token fallback chain, and a safe 360p last resort for SABR-restricted
  videos (see the [technical doc](./docs/technical.md#sabr-restricted-videos)).
- **Dashboard UI** — download history, one-click manual downloads of any video
  or playlist, and live settings editing.
- **Zero dependencies** — Python standard library + one shell script; a
  vanilla-JS dashboard with no framework or database.

## PO token provider (recommended for SABR-restricted videos)

YouTube restricts some videos via a SABR (Segment And Byte Range) experiment
([yt-dlp#12482](https://github.com/yt-dlp/yt-dlp/issues/12482)) that serves
high-resolution formats only to clients that present a valid GVS PO token.
Without one, those videos download at 360p only.

The `docker-compose.yml` in this repo bundles an optional
[`brainicism/bgutil-ytdlp-pot-provider`](https://hub.docker.com/r/brainicism/bgutil-ytdlp-pot-provider)
container (`pot`) that generates these tokens. It is shared by both the search
script and the downloader via the `POT_PROVIDER` env var, and unlocks the full
quality for SABR-restricted videos. It is entirely optional — leave it out and
such videos still download at 360p. See the
[technical doc](./docs/technical.md#sabr-restricted-videos) for details.

## Quick start (Docker)

A prebuilt image is published to GHCR on every push to `main`:

```sh
docker run -d \
  --name yt-indexarr \
  -p 9117:9117 \
  -p 9177:9177 \
  -v yt-indexarr-config:/data/config \
  -v yt-indexarr-data:/data/downloads \
  ghcr.io/fosserglitch902/yt-indexarr:latest
```

Or with compose — the repo's [`docker-compose.yml`](./docker-compose.yml) runs
the image plus the optional PO-token provider:

```sh
docker compose up -d
```

State is split across two volumes: **config** (`config.json`, `history.json`,
`cookies.txt`, API caches) and **downloads** (the media files). Compose sets
the state paths explicitly; the [technical doc](./docs/technical.md#docker)
has the full compose reference.

## The dashboard

The downloader doubles as a small web UI on its own port
(`http://<host>:9177/`), no extra service:

- **Downloads** — every download (completed, failed, in-progress) with size,
  channel, finish time, and a poster; playlist packs expand per episode. The
  **manual download** box at the top queues any YouTube video or playlist URL
  through the exact same downloader path as a Sonarr grab.
- **Settings** — edit the settings below live: TVDB API key, PO provider,
  codec/container, log levels, credentials, and a paste box for `cookies.txt`.
  Compose env vars **always win** over UI edits; secrets are never echoed back.
  When auth is enabled you sign in with the same credentials Sonarr uses.

## Configuration

Every setting resolves as **environment variable → `/data/config/config.json` →
default**; the dashboard writes the config file, and anything set in compose
overrides the UI. These are the settings shown in the dashboard:

| Variable | Meaning |
| -------- | ------- |
| `TVDB_API_KEY` | TheTVDB v4 API key. Enables localized (per-language) episode titles for precise matching; without it, lookups fall back to keyless TVMaze (English only) |
| `POT_PROVIDER` | GVS PO-token provider URL (e.g. `http://pot:4416`). Unlocks high-res formats on SABR-restricted videos; shared by the search script and the downloader |
| `YT_CODEC` | Video codec for downloads, `auto` \| `av1` \| `vp9` \| `h264` (default `auto`). Also scales the reported size |
| `YT_QBT_OUTPUT_EXT` | Output container, `mkv` (default, holds every codec) \| `mp4` (universal direct-play). Takes effect when `ffmpeg` is installed |
| `YT_INDEXER_LOG_LEVEL` | Indexer log level, `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` (default `INFO`) |
| `YT_QBT_LOG_LEVEL` | Downloader log level (default `INFO`) |
| `YT_INDEXER_API_KEY` | Torznab API key (default `youtubeindexer`) |
| `YT_INDEXER_REQUIRE_KEY` | Enforce the API key, `1`/`0` (default `0` — auth off) |
| `YT_QBT_USERNAME` | Downloader login username (default `admin`) |
| `YT_QBT_PASSWORD` | Downloader login password (default `adminadmin`) |
| `YT_QBT_REQUIRE_AUTH` | Require login on the downloader, `1`/`0` (default `0`) |
| `YT_QBT_COOKIES` | Netscape-format browser cookies file passed to `yt-dlp` (managed from the Settings tab). The most reliable way to reduce YouTube 403 / SABR failures |

Everything else — ports, bind hosts, search-script knobs, cache paths, and
more — is documented in the
[technical reference](./docs/technical.md#environment-variables-quick-index).

## Setup with Prowlarr / Sonarr

1. Add a **Generic Torznab** indexer in Prowlarr.
2. URL: `http://<host>:9117/torznab` (blank API key by default).
3. Point Sonarr at a **qBittorrent** download client on `<host>:9177` with the
   spoofer's `YT_QBT_USERNAME`/`YT_QBT_PASSWORD`.

The indexer only ever returns YouTube videos and every search runs `yt-dlp`, so
treat it as a **manual, targeted** source:

- Set the indexer's **sync profile to "Interactive Search only"** (disable RSS
  and Automatic Search) in Sonarr and Prowlarr, so nothing polls it in the
  background.
- Use it only with this repo's downloader — the enclosures are fake magnets
  that only the spoofer can decode.
- Give the downloader a **lower priority** than your main client (1–50, higher
  = lower), so normal releases stay on your main client and only YouTube grabs
  fall through to it.

## Docs

- [**Technical reference**](./docs/technical.md) — every script, endpoint,
  environment variable, knob, and the SABR / cookies / Docker deep-dive.

## Acknowledgements

This project relies on several excellent open-source projects:

- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — the workhorse that does the
  actual YouTube extraction and downloading. Everything here wraps around it.
- **[brainicism/bgutil-ytdlp-pot-provider](https://hub.docker.com/r/brainicism/bgutil-ytdlp-pot-provider)**
  — the PO token provider that unblocks SABR-restricted videos.
- **[Deno](https://deno.land/)** / **[Node.js](https://nodejs.org/)** — runtimes
  for the bundled `yt-dlp-ejs` challenge scripts (used by the token provider).
- **[TVMaze](https://www.tvmaze.com/)** and **[TheTVDB](https://thetvdb.com/)** —
  episode metadata APIs (keyless TVMaze + optional TheTVDB for localized titles).

## Disclaimer

This project is **not affiliated with, endorsed by, or connected to** yt-dlp,
Google, or YouTube in any way. yt-dlp is a trademark of its respective owners,
and YouTube is a trademark of Google LLC. Use of these names and trademarks is
for identification purposes only and does not imply any affiliation or
endorsement.
