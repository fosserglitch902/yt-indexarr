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
import re
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
    data = _show_for_tvdbid(tvdbid)
    return data.get("id") if isinstance(data, dict) else None


def show_name_by_tvdbid(tvdbid):
    """Return the TVMaze show name for a thetvdb id, or None."""
    data = _show_for_tvdbid(tvdbid)
    return data.get("name") if isinstance(data, dict) else None


def _show_for_tvdbid(tvdbid):
    """TVMaze show dict (id+name) for a thetvdb id, cached; or None."""
    key = f"show:{tvdbid}"
    cached = _cache_get(key)
    if isinstance(cached, dict):
        return cached
    result = None
    try:
        data = _get_json(f"/lookup/shows?thetvdb={tvdbid}")
        if isinstance(data, dict) and data.get("id"):
            result = data
    except (OSError, ValueError):
        log.warning("TVMaze lookup failed for tvdbid %s", tvdbid)
    _cache_set(key, result)
    return result


def _strip_trailing_year(name):
    """Drop Sonarr's year disambiguation suffix, e.g. 'Bluey 2018' -> 'Bluey'."""
    return re.sub(r"[\(\s]*(?:19|20)\d{2}\s*\)?\s*$", "", name.strip()).rstrip()


def show_by_name(name):
    """Resolve a series name to a TVMaze show id, or None.

    Uses singlesearch (best match).  When that fails for names with a trailing
    year token (Sonarr appends it to disambiguate, TVMaze titles do not carry
    it), retries without the year.  Results are cached keyed by name.
    """
    name = (name or "").strip()
    if not name:
        return None
    key = "name:" + name.casefold()
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = None
    for cand in dict.fromkeys([name, _strip_trailing_year(name)]):
        if not cand:
            continue
        try:
            data = _get_json("/singlesearch/shows?q=" + urllib.parse.quote(cand))
            if isinstance(data, dict) and data.get("id"):
                result = data["id"]
                break
        except (OSError, ValueError):
            log.warning("TVMaze name lookup failed for %r", cand)
            continue
    _cache_set(key, result)
    return result


def episode_title(show_id, season, number):
    """Return the episode name, or None (also None on 404/timeout)."""
    ep = _episode_data(show_id, season, number)
    return ep.get("name") if ep else None


def episode_runtime(show_id, season, number):
    """Episode runtime in minutes, or None (also None on 404/timeout)."""
    ep = _episode_data(show_id, season, number)
    if not ep:
        return None
    try:
        return int(ep.get("runtime") or 0) or None
    except (TypeError, ValueError):
        return None


def _episode_data(show_id, season, number):
    """Full episode object (name+runtime) for one episode, cached; or None."""
    key = f"ep:{show_id}:{season}:{number}"
    cached = _cache_get(key)
    if isinstance(cached, dict):
        return cached
    result = None
    try:
        data = _get_json(
            f"/shows/{show_id}/episodebynumber?season={season}&number={number}"
        )
        if isinstance(data, dict) and data.get("name"):
            result = {
                "name": data["name"],
                "runtime": data.get("runtime"),
            }
    except (OSError, ValueError):
        log.warning(
            "TVMaze episode lookup failed for show %s s%se%s",
            show_id, season, number,
        )
    _cache_set(key, result)
    return result


def season_episodes(tvdbid, season):
    """Return {episode_number: {name, runtime}} for a whole season; {} on fail.

    Keyless: resolves the show from tvdbid, then fetches all episodes once and
    filters to the season client-side (TVMaze's ?season= filter is not
    honored).  Runtime is in minutes when reported, else None.
    """
    if not tvdbid or not str(tvdbid).isdigit():
        return {}
    show_id = show_by_tvdbid(int(tvdbid))
    if not show_id:
        return {}
    key = f"season:{show_id}:{season}"
    cached = _cache_get(key)
    if isinstance(cached, dict):
        return cached
    result = {}
    try:
        data = _get_json(f"/shows/{show_id}/episodes")
        if isinstance(data, list):
            for ep in data:
                if ep.get("season") != season or not ep.get("number"):
                    continue
                result[int(ep["number"])] = {
                    "name": ep.get("name") or "",
                    "runtime": ep.get("runtime"),
                }
    except (OSError, ValueError):
        log.warning("TVMaze season episodes failed for show %s", show_id)
    _cache_set(key, result)
    return result


def resolve_title(tvdbid, season: int, number: int, name: str = ""):
    """One-call helper: tvdbid or name + season + number -> episode name.

    Prefers the tvdbid lookup; falls back to a series-name lookup when tvdbid
    is absent (Prowlarr does not always forward it).  Returns None when
    nothing resolves.
    """
    if tvdbid and str(tvdbid).isdigit():
        show_id = show_by_tvdbid(int(tvdbid))
    elif name:
        show_id = show_by_name(name)
    else:
        return None
    if not show_id:
        return None
    return episode_title(show_id, season, number)


def resolve_runtime(tvdbid, season: int, number: int, name: str = ""):
    """One-call helper: tvdbid or name + season + number -> runtime minutes.

    Returns None when nothing resolves (callers degrade to no filter).
    """
    if tvdbid and str(tvdbid).isdigit():
        show_id = show_by_tvdbid(int(tvdbid))
    elif name:
        show_id = show_by_name(name)
    else:
        return None
    if not show_id:
        return None
    return episode_runtime(show_id, season, number)
