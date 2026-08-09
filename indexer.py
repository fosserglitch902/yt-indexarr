#!/usr/bin/env python3
"""Torznab-compatible indexer for Sonarr/Prowlarr backed by yt-episode-search.sh."""

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

log = logging.getLogger("yt-indexer")


def _now_rfc2822() -> str:
    return format_datetime(datetime.now(timezone.utc))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEARCH_SCRIPT = os.environ.get(
    "YT_INDEXER_SCRIPT", os.path.join(SCRIPT_DIR, "yt-episode-search.sh")
)

HOST = os.environ.get("YT_INDEXER_HOST", "0.0.0.0")
PORT = int(os.environ.get("YT_INDEXER_PORT", "9117"))
API_KEY = os.environ.get("YT_INDEXER_API_KEY", "youtubeindexer")
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
        searching, "tv-search", available="yes", supportedParams="q,season,ep"
    )
    ET.SubElement(
        searching, "movie-search", available="no", supportedParams="q"
    )
    categories = ET.SubElement(root, "categories")
    tv = ET.SubElement(categories, "category", id=TV_CATEGORY, name="TV")
    for cid, name in SUBCATS.items():
        ET.SubElement(tv, "subcat", id=cid, name=name)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


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
        ET.SubElement(item, "title").text = it["title"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = it["guid"]
        ET.SubElement(item, "link").text = it["url"]
        ET.SubElement(item, "category").text = TV_CATEGORY
        ET.SubElement(item, "pubDate").text = it.get("pub_date") or _now_rfc2822()
        desc = it.get("description", "")
        if desc:
            ET.SubElement(item, "description").text = desc
        ET.SubElement(
            item,
            "enclosure",
            url=it["url"],
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
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _error_xml(message: str) -> bytes:
    root = ET.Element("error", code="100", description=message)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def estimate_size(duration: int) -> int:
    """Rough size estimate for a video of given duration in seconds.

    ~1.5 Mbps video + 128 kbps audio, plus overhead.
    """
    return int(duration * ((1.5 * 1024 * 1024 / 8) + (128 * 1024 / 8)))


def run_search(series: str, season: str, episode: str) -> list:
    cmd = [
        SEARCH_SCRIPT,
        "-s", series,
        "-S", str(season),
        "-E", str(episode),
        "-j",
    ]
    log.info("running search: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
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
        items.append(
            {
                "title": data.get("normalized_title") or data.get("title", ""),
                "guid": f"yt-{data.get('id', '')}",
                "url": data.get("url", ""),
                "views": int(data.get("views", 0) or 0),
                "size": estimate_size(int(data.get("duration", 0) or 0)),
                "description": data.get("title", ""),
                "resolution": data.get("resolution") or "",
                "source": "web",
                "pub_date": data.get("pub_date") or _now_rfc2822(),
            }
        )
    return items


def handle_tvsearch(params: dict, self_url: str) -> bytes:
    series = (params.get("q") or "").strip()
    season = params.get("season") or ""
    episode = params.get("ep") or params.get("episode") or ""

    # tvdbid/rid-only queries we cannot answer.
    if not series:
        return _error_xml("No search term (q) provided.")

    # Season may be 0 for date-based series; episode required for tvsearch.
    if episode == "" or not str(episode).replace("/", "").isdigit():
        return _error_xml("tvsearch requires an episode (ep) parameter.")

    # Multi-episode ranges like "12/20" - take the first.
    episode = str(episode).split("/")[0]

    season_num = 0
    if season != "" and str(season).isdigit():
        season_num = int(season)

    if not str(episode).isdigit():
        return _error_xml(f"Invalid episode: {episode}")

    items = run_search(series, season_num, int(episode))
    return _rss_xml(items, self_url)


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
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
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
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        flat = {k: (v[0] if v else "") for k, v in params.items()}
        self_url = f"{BASE_URL}{self.path}"

        key = flat.get("apikey") or flat.get("key")
        if not key or key != API_KEY:
            body = _error_xml("Invalid API key.")
            self._send(body, XML_MIME, status=401)
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

        body = _error_xml(f"Unsupported operation: {t}")
        self._send(body, XML_MIME, status=400)

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
    log.info("Torznab indexer listening on http://%s:%d (key: %s)", HOST, PORT, API_KEY)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
