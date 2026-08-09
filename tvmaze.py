#!/usr/bin/env python3
"""TVMaze episode-title lookup for tvdbid-based tvsearch.

Sonarr sends only q + season/ep on tvsearch (no episode title).  Resolve the
episode title via TVMaze (keyless) using the tvdbid Sonarr already knows, so
the search script can include it as -t and earn title_score.

Lookups are cached in memory and on disk (TTL-bounded) so repeated Sonarr
polls do not re-hit TVMaze.
"""

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("yt-tvmaze")

TVMAZE_API = os.environ.get("TVMAZE_API", "https://api.tvmaze.com")
TVMAZE_TIMEOUT = float(os.environ.get("TVMAZE_TIMEOUT", "5"))
CACHE_FILE = os.environ.get(
    "TVMAZE_CACHE_FILE",
    os.path.join(
        os.path.expanduser("~"), ".cache", "yt-indexarr", "tvmaze.json"
    ),
)
CACHE_TTL = int(os.environ.get("TVMAZE_CACHE_TTL", str(7 * 24 * 3600)))

_cache = {}
_cache_lock = threading.Lock()
_cache_loaded = False


def _load_cache():
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        with open(CACHE_FILE) as fh:
            _cache.update(json.load(fh))
    except (OSError, ValueError):
        pass


def _save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_cache, fh)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        log.warning("could not write TVMaze cache: %s", CACHE_FILE)


def _cache_get(key):
    _load_cache()
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > CACHE_TTL:
            _cache.pop(key, None)
            return None
        return entry.get("value")


def _cache_set(key, value):
    _load_cache()
    with _cache_lock:
        _cache[key] = {"ts": time.time(), "value": value}
        _save_cache()


def _get_json(path):
    url = TVMAZE_API.rstrip("/") + path
    req = urllib.request.Request(url, headers={"User-Agent": "yt-indexarr/1.0"})
    with urllib.request.urlopen(req, timeout=TVMAZE_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def show_by_tvdbid(tvdbid):
    """Return the TVMaze show id for a thetvdb id, or None.

    TVMaze answers this lookup with a 301 redirect to the canonical show URL;
    urllib follows redirects automatically.
    """
    key = f"show:{tvdbid}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = None
    try:
        data = _get_json(f"/lookup/shows?thetvdb={tvdbid}")
        if isinstance(data, dict) and data.get("id"):
            result = data["id"]
    except (OSError, ValueError):
        log.warning("TVMaze lookup failed for tvdbid %s", tvdbid)
    _cache_set(key, result)
    return result


def episode_title(show_id, season, number):
    """Return the episode name, or None (also None on 404/timeout)."""
    key = f"title:{show_id}:{season}:{number}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = None
    try:
        data = _get_json(
            f"/shows/{show_id}/episodebynumber?season={season}&number={number}"
        )
        if isinstance(data, dict) and data.get("name"):
            result = data["name"]
    except (OSError, ValueError):
        log.warning(
            "TVMaze episode lookup failed for show %s s%se%s",
            show_id, season, number,
        )
    _cache_set(key, result)
    return result


def resolve_title(tvdbid: str, season: int, number: int):
    """One-call helper: tvdbid + season + number -> episode name or None."""
    tvdbid = str(tvdbid).strip()
    if not tvdbid.isdigit():
        return None
    show_id = show_by_tvdbid(int(tvdbid))
    if not show_id:
        return None
    return episode_title(show_id, season, number)
