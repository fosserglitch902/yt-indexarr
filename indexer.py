#!/usr/bin/env python3
"""Torznab-compatible indexer for Sonarr/Prowlarr backed by yt-episode-search.sh."""

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tvdb
import tvmaze
import config

log = logging.getLogger("yt-indexer")


def _now_rfc2822() -> str:
    return format_datetime(datetime.now(timezone.utc))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEARCH_SCRIPT = os.environ.get(
    "YT_INDEXER_SCRIPT", os.path.join(SCRIPT_DIR, "yt-episode-search.sh")
)

HOST = os.environ.get("YT_INDEXER_HOST", "0.0.0.0")
PORT = int(os.environ.get("YT_INDEXER_PORT", "9117"))
# Auth is optional by default; set YT_INDEXER_REQUIRE_KEY=1 (or true/yes/on)
# to enforce the API key on every request.  Resolved lazily so the dashboard
# UI can change key + enforcement without a restart.
def _api_key():
    return config.get("YT_INDEXER_API_KEY", "youtubeindexer")


def _key_required():
    return config.get("YT_INDEXER_REQUIRE_KEY", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _apply_log_level():
    level = config.get("YT_INDEXER_LOG_LEVEL", "INFO")
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))


def _search_env():
    """Env for the search script: inherited env plus UI-file settings that
    aren't real process env vars (e.g. POT_PROVIDER set via the dashboard)."""
    env = dict(os.environ)
    env["POT_PROVIDER"] = config.get("POT_PROVIDER", "").strip()
    return env


INDEXER_NAME = os.environ.get("YT_INDEXER_INDEXER_NAME", "YouTube")
BASE_URL = os.environ.get("YT_INDEXER_BASE_URL", f"http://localhost:{PORT}")

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
ATOM_NS = "http://www.w3.org/2005/Atom"
RSS_MIME = "application/rss+xml; charset=utf-8"
XML_MIME = "text/xml; charset=utf-8"

# Torznab categories we advertise.
TV_CATEGORY = "5000"
SUBCATS = {
    "5030": "TV/HD",
    "5040": "TV/SD",
}


def _caps_xml() -> bytes:
    root = ET.Element("caps")
    server = ET.SubElement(root, "server")
    server.set("version", "1.0")
    server.set("title", INDEXER_NAME)
    server.set("strapline", "YouTube search for Sonarr/Prowlarr")
    server.set("url", BASE_URL)
    ET.SubElement(root, "limits", max="100", default="20")
    ET.SubElement(root, "registration", available="no", open="no")
    searching = ET.SubElement(root, "searching")
    ET.SubElement(
        searching, "search", available="no", supportedParams="q"
    )
    ET.SubElement(
        searching, "tv-search", available="yes", supportedParams="q,season,ep,tvdbid,language"
    )
    ET.SubElement(
        searching, "movie-search", available="no", supportedParams="q"
    )
    categories = ET.SubElement(root, "categories")
    tv = ET.SubElement(categories, "category", id=TV_CATEGORY, name="TV")
    for cid, name in SUBCATS.items():
        ET.SubElement(tv, "subcat", id=cid, name=name)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _magnet_url(item: dict) -> str:
    """Magnet carrier encoding the release title + the real YouTube URL.

    Contract shared with the download-client spoof (same monorepo):
      xt=urn:btih:<sha1(youtube_url)>   deterministic fake info-hash
      dn=<url-encoded release title>    used as the torrent/file name
      x.ytindexer=<base64url(url)>      the real URL the spoof runs yt-dlp on
      x.ytindexertvdbid=<id>            TVDB series id (season packs), optional
      x.ytindexerchannel=<channel>      YouTube channel, optional
    """
    url = item["url"]
    title = item.get("title") or "yt"
    btih = hashlib.sha1(url.encode("utf-8")).hexdigest()
    dn = urllib.parse.quote(title)
    xurl = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")
    magnet = f"magnet:?xt=urn:btih:{btih}&dn={dn}&x.ytindexer={xurl}"
    if item.get("tvdbid"):
        magnet += f"&x.ytindexertvdbid={urllib.parse.quote(str(item['tvdbid']))}"
    channel = (item.get("channel") or "").strip()
    if channel:
        magnet += "&x.ytindexerchannel=" + urllib.parse.quote(channel)
    return magnet


def _rss_xml(items: list, self_url: str) -> bytes:
    ET.register_namespace("torznab", TORZNAB_NS)
    ET.register_namespace("atom", ATOM_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = INDEXER_NAME
    ET.SubElement(channel, "link").text = BASE_URL
    ET.SubElement(channel, "description").text = "YouTube Torznab indexer"
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        href=self_url,
        rel="self",
        type="application/rss+xml",
    )
    for it in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = it.get("display_title") or it["title"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = it["guid"]
        ET.SubElement(item, "link").text = it["url"]
        # Sonarr renders the release title as a clickable link to this
        # (Torznab GetInfoUrl uses <comments>); point it at the YouTube video.
        ET.SubElement(item, "comments").text = it["url"]
        ET.SubElement(item, "category").text = TV_CATEGORY
        ET.SubElement(item, "pubDate").text = it.get("pub_date") or _now_rfc2822()
        desc = it.get("description", "")
        if desc:
            ET.SubElement(item, "description").text = desc
        ET.SubElement(
            item,
            "enclosure",
            url=_magnet_url(it),
            length=str(it["size"]),
            type="application/x-bittorrent",
        )
        ET.SubElement(
            item, f"{{{TORZNAB_NS}}}attr", name="seeders", value=str(it["views"])
        )
        ET.SubElement(
            item, f"{{{TORZNAB_NS}}}attr", name="peers", value=str(it["views"])
        )
        ET.SubElement(
            item, f"{{{TORZNAB_NS}}}attr", name="size", value=str(it["size"])
        )
        res = it.get("resolution")
        if res:
            ET.SubElement(
                item, f"{{{TORZNAB_NS}}}attr", name="resolution", value=res
            )
        ET.SubElement(
            item, f"{{{TORZNAB_NS}}}attr", name="source", value=it.get("source") or "web"
        )
        lang = it.get("language")
        if lang:
            ET.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="language", value=lang)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _empty_rss(self_url: str) -> bytes:
    """Valid empty RSS feed for queries we cannot answer.

    Sonarr's Torznab parser treats an <error code="100"> response as an
    ApiKeyException (codes 100-199), which is misleading for a plain "no
    results" case (e.g. a full-season search).  Returning an empty feed is
    the correct, graceful way to say "nothing to report".
    """
    return _rss_xml([], self_url)


_RES_BITRATE = {
    "2160": 12.0,
    "1440": 7.0,
    "1080": 4.5,
    "720": 2.5,
    "480": 1.2,
    "360": 0.8,
    "0": 1.5,
}

# Relative file-size for a given video codec vs the AV1-sized baseline table
# above (measured on live YouTube formats: vp9 ~1.4x, h264 ~2.0x at equal
# resolution).  auto/av1 stay at 1.0.  Shared with the downloader via YT_CODEC.
_CODEC_MULT = {
    "vp9": 1.4,
    "h264": 2.0,
}


def estimate_size(duration: int, resolution: str = "") -> int:
    """Rough size estimate for a video of given duration in seconds.

    Video bitrate chosen from the resolution (falling back to ~1.5 Mbps),
    plus 128 kbps audio and overhead.  Scaled by the YT_CODEC video codec
    choice so the reported size tracks what the downloader will actually
    produce (h264/vp9 files are larger than AV1 for the same quality).
    """
    mbit = _RES_BITRATE.get(str(resolution).split("p", 1)[0], 1.5)
    mbit *= _CODEC_MULT.get(config.get("YT_CODEC", "auto").strip().lower(), 1.0)
    return int(duration * ((mbit * 1024 * 1024 / 8) + (128 * 1024 / 8)))


def run_search(
    series: str, season: str, episode: str, episode_titles: list = None,
    expected_duration: int = 0,
) -> list:
    cmd = [
        SEARCH_SCRIPT,
        "-s", series,
        "-S", str(season),
        "-E", str(episode),
        "-j",
    ]
    for title in episode_titles or []:
        cmd += ["-t", title]
    env = _search_env()
    if expected_duration and expected_duration > 0:
        env["EXPECTED_DURATION"] = str(int(expected_duration))
    log.info("running search: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if proc.returncode != 0:
        log.error("search script failed: %s", proc.stderr.strip())
        return []

    items = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            log.warning("skipping non-JSON line: %s", line)
            continue
        if not data.get("probable"):
            continue
        raw_title = data.get("title", "")
        title = data.get("normalized_title") or raw_title
        channel = (data.get("channel") or "").strip()
        display_title = title
        if raw_title and raw_title != title:
            display_title = f"{display_title} - {raw_title}"
        if channel:
            display_title = f"{display_title} - {channel}"
        items.append(
            {
                "title": title,
                "display_title": display_title,
                "guid": f"yt-{data.get('id', '')}",
                "url": data.get("url", ""),
                "views": int(data.get("views", 0) or 0),
                "size": estimate_size(
                    int(data.get("duration", 0) or 0),
                    data.get("resolution") or "",
                ),
                "description": raw_title,
                "resolution": data.get("resolution") or "",
                "language": data.get("language") or "",
                "source": "web",
                "pub_date": data.get("pub_date") or _now_rfc2822(),
            }
        )
    return items


def handle_tvsearch(params: dict, self_url: str) -> bytes:
    series = (params.get("q") or "").strip()
    season = params.get("season") or ""
    episode = params.get("ep") or params.get("episode") or ""
    tvdbid = (params.get("tvdbid") or "").strip()

    # tvdbid/rid-only queries we cannot answer.
    if not series and not tvdbid:
        return _empty_rss(self_url)

    # A season without an episode is a full-season (interactive) search:
    # return playlist season-packs instead of an empty feed.  Only route
    # here when a season is actually present; a bare tvdbid-only request
    # falls through to an empty feed.
    if episode == "" and season != "":
        return handle_season_search(params, self_url)

    if not str(episode).replace("/", "").isdigit():
        return _empty_rss(self_url)

    # Multi-episode ranges like "12/20" - take the first.
    episode = str(episode).split("/")[0]

    season_num = 0
    if season != "" and str(season).isdigit():
        season_num = int(season)

    if not str(episode).isdigit():
        return _empty_rss(self_url)

    # Prowlarr may send tvdbid instead of (or in addition to) q.  When q is
    # missing, resolve the series name from TVMaze so we still have a YouTube
    # search term; last resort is the fallback query.
    if not series:
        if tvdbid:
            series = tvmaze.show_name_by_tvdbid(tvdbid) or ""
            if series:
                log.info("TVMaze: tvdbid=%s -> series name %r", tvdbid, series)
        if not series:
            series = os.environ.get("YT_INDEXER_FALLBACK_QUERY", "tv episode")

    # Localized title matching.  Sonarr's per-indexer "Additional parameters"
    # can send `&language=fr,en` (the value is appended to the search URL, so
    # it must start with `&`; Sonarr/Prowlarr enforce `(&.+?=.+?)+`).  TVDB
    # provides localized episode titles when
    # TVDB_API_KEY is configured, otherwise we fall back to the TVMaze
    # (English) title.  Failures degrade gracefully to number-only search.
    languages = _parse_languages(params)
    episode_titles = []
    if tvdbid and tvdb.enabled():
        episode_titles = tvdb.resolve_titles(
            tvdbid, season_num, int(episode), languages
        )
        if episode_titles:
            log.info(
                "TVDB: tvdbid=%s s%se%s titles=%r",
                tvdbid, season_num, episode, episode_titles,
            )
    if not episode_titles:
        if languages and not tvdb.enabled():
            log.info(
                "language=%r requested but TVDB_API_KEY not set; "
                "using TVMaze English title",
                languages,
            )
        episode_title = tvmaze.resolve_title(
            tvdbid, season_num, int(episode), name=series
        )
        if episode_title:
            episode_titles = [episode_title]
            log.info(
                "TVMaze: tvdbid=%r s%se%s -> %r",
                tvdbid, season_num, episode, episode_title,
            )

    # Expected runtime (minutes -> seconds) for the episode, used to filter
    # out multi-episode compilations at the search stage.  TVDB first, TVMaze
    # fallback; unresolved stays 0 so the search runs unfiltered as before.
    expected_duration = 0
    if tvdb.enabled():
        tvm = tvdb.episode_runtime(tvdbid, season_num, int(episode))
        if tvm:
            expected_duration = int(tvm) * 60
    if not expected_duration:
        tm = tvmaze.resolve_runtime(
            tvdbid, season_num, int(episode), name=series
        )
        if tm:
            expected_duration = int(tm) * 60
    if expected_duration:
        log.info(
            "expected runtime: tvdbid=%s s%se%s -> %ss",
            tvdbid, season_num, episode, expected_duration,
        )

    items = run_search(
        series, season_num, int(episode), episode_titles,
        expected_duration=expected_duration,
    )
    return _rss_xml(items, self_url)


def handle_season_search(params: dict, self_url: str) -> bytes:
    """Full-season interactive search: return matching YouTube playlists.

    Sonarr sends t=tvsearch&season=N with no ep for a season search.  There is
    no per-episode matching here; each playlist is surfaced as a season-pack
    release, and the download-client spoof downloads it as a whole season.
    """
    series = (params.get("q") or "").strip()
    season = params.get("season") or ""
    tvdbid = (params.get("tvdbid") or "").strip()

    season_num = 0
    if season != "" and str(season).isdigit():
        season_num = int(season)

    if not series and tvdbid:
        series = tvmaze.show_name_by_tvdbid(tvdbid) or ""
        if series:
            log.info("TVMaze: tvdbid=%s -> series name %r", tvdbid, series)
    if not series:
        series = os.environ.get("YT_INDEXER_FALLBACK_QUERY", "tv episode")

    items = run_season_search(series, season_num, tvdbid=tvdbid)
    return _rss_xml(items, self_url)


def _season_total_runtime(tvdbid: str, season: int) -> int:
    """Sum of episode runtimes (seconds) for a season, or 0 when unavailable.

    TVDB first (when a key is configured), then the keyless TVMaze fallback.
    Only entries with a named runtime count.
    """
    if tvdbid and str(tvdbid).isdigit():
        if tvdb.enabled():
            season_eps = tvdb.season_episodes(tvdbid, season)
            if season_eps:
                total = 0
                for meta in season_eps.values():
                    try:
                        total += int(meta.get("runtime") or 0)
                    except (TypeError, ValueError):
                        continue
                if total > 0:
                    return total * 60
        tm = tvmaze.season_episodes(tvdbid, season)
        if tm:
            total = 0
            for meta in tm.values():
                try:
                    total += int(meta.get("runtime") or 0)
                except (TypeError, ValueError):
                    continue
            if total > 0:
                return total * 60
    return 0


def run_season_search(series: str, season: int, tvdbid: str = "") -> list:
    """Search YouTube for playlists of a whole season via the search script.

    The script runs in playlist mode (yt-dlp results URL with the playlists
    sp filter), returning one JSON object per playlist.  Each playlist becomes
    a season-pack release; the magnet carries the playlist URL and, when known,
    the TVDB series id so the download-client spoof can map videos to episodes.
    """
    cmd = [
        SEARCH_SCRIPT,
        "-s", series,
        "-S", str(season),
        "-P",  # playlist/season mode
        "-j",
    ]
    log.info("running season search: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          env=_search_env())
    if proc.returncode != 0:
        log.error("season search failed: %s", proc.stderr.strip())
        return []

    season_sec = _season_total_runtime(tvdbid, season)

    items = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            log.warning("skipping non-JSON line: %s", line)
            continue
        pid = data.get("id")
        if not pid:
            continue
        title = data.get("title", "")
        count = int(data.get("playlist_count", 0) or 0)
        channel = (data.get("channel") or "").strip()
        # Quality + popularity of the pack = its first video's (probed by the
        # script; playlists carry no view count of their own).  Unknown
        # playlists fall back to the 1080 baseline for the size estimate.
        res = data.get("resolution") or ""
        res_or = res or "1080"
        views = int(data.get("views") or 0)
        if season_sec:
            size = estimate_size(season_sec, res_or)
        else:
            size = estimate_size(1500, res_or) * count
        base = f"{series} S{season:02d} WEB"
        if res:
            base = f"{base} {res}"
        display = base
        if title and title != base:
            display = f"{display} - {title}"
        if channel:
            display = f"{display} - {channel}"
        if count:
            display = f"{display} x{count}"
        items.append(
            {
                "title": base,
                "display_title": display,
                "guid": f"ytpl-{pid}",
                "url": data.get("url")
                or f"https://www.youtube.com/playlist?list={pid}",
                "views": views,
                "size": size,
                "description": title,
                "resolution": res,
                "language": "",
                "source": "web",
                "pub_date": _now_rfc2822(),
                "tvdbid": tvdbid,
            }
        )
    return items


def _parse_languages(params: dict) -> list:
    """Parse the language/lang param into a list of ISO-639-1 codes (max 2)."""
    raw = (params.get("language") or params.get("lang") or "").strip()
    langs = []
    for part in raw.split(","):
        code = part.strip().lower()
        if 2 <= len(code) <= 3 and code.isalpha() and code not in langs:
            langs.append(code)
        if len(langs) == 2:
            break
    return langs


def handle_search(params: dict, self_url: str) -> bytes:
    q = (params.get("q") or "").strip()
    # Prowlarr's connectivity test calls t=search with no q; use a fallback
    # so the test returns results instead of an empty feed.
    if not q:
        q = os.environ.get("YT_INDEXER_FALLBACK_QUERY", "tv episode")
    items = run_generic_search(q)
    return _rss_xml(items, self_url)


def run_generic_search(query: str, limit: int = 10) -> list:
    """Run a plain yt-dlp search, no episode matching.

    Used by t=search (Prowlarr test) where there is no season/episode.
    """
    min_duration = int(os.environ.get("MIN_DURATION", "300"))
    cmd = [
        "yt-dlp",
        "--ignore-config",
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--retries", "3",
        "--match-filter", f"duration >= {min_duration}",
        f"ytsearch{limit}:{query}",
    ]
    log.info("running generic search: %s", query)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          env=_search_env())
    items = []
    if proc.returncode != 0:
        log.error("generic search failed: %s", proc.stderr.strip())
        return items
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = data.get("id")
        if not vid:
            continue
        dur = int(data.get("duration", 0) or 0)
        items.append(
            {
                "title": data.get("title", ""),
                "guid": f"yt-{vid}",
                "url": data.get("webpage_url")
                or f"https://www.youtube.com/watch?v={vid}",
                "views": int(data.get("view_count", 0) or 0),
                "size": estimate_size(dur),
                "description": data.get("title", ""),
                "pub_date": _now_rfc2822(),
            }
        )
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "YTIndexer/1.0"

    def _send(self, body: bytes, mime: str, status: int = 200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("client disconnected during response write")
        except OSError:
            log.warning("socket error during response write")

    def do_GET(self):
        _apply_log_level()
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        flat = {k: (v[0] if v else "") for k, v in params.items()}
        self_url = f"{BASE_URL}{self.path}"

        key = flat.get("apikey") or flat.get("key")
        if _key_required() and (not key or key != _api_key()):
            # Return an empty feed rather than <error> (codes 100-199 read as
            # ApiKeyException in Sonarr) so auth failures don't surface as a
            # confusing "API key invalid" against this indexer.
            body = _empty_rss(self_url)
            self._send(body, RSS_MIME, status=401)
            return

        t = (flat.get("t") or "").lower()
        if t == "caps":
            self._send(_caps_xml(), XML_MIME)
            return
        if t in ("tvsearch", "tv", "tvsearch2"):
            body = handle_tvsearch(flat, self_url)
            self._send(body, RSS_MIME)
            return
        if t in ("search", "movie"):
            body = handle_search(flat, self_url)
            self._send(body, RSS_MIME)
            return
        if t == "rss":
            self._send(_rss_xml([], self_url), RSS_MIME)
            return

        body = _empty_rss(self_url)
        self._send(body, RSS_MIME, status=400)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(
        level=os.environ.get("YT_INDEXER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not os.path.exists(SEARCH_SCRIPT):
        log.error("search script not found: %s", SEARCH_SCRIPT)
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    auth = "on" if _key_required() else "off"
    log.info(
        "Torznab indexer listening on http://%s:%d (api key required: %s)",
        HOST, PORT, auth,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
