#!/usr/bin/env python3
"""TheTVDB v4 localized episode-title lookup.

Sonarr's per-indexer "Additional parameters" can send `&language=fr,en` (the
value is appended to the search URL, so it must start with `&`; Sonarr/Prowlarr
enforce `(&.+?=.+?)+`) so the indexer knows which localized episode titles to
match against on YouTube.
TVMaze has no episode translations, so localized titles come from TheTVDB v4
(the same source Sonarr uses).  When TVDB_API_KEY is not configured, callers
fall back to the TVMaze (English) lookup.

Auth: POST /login with {apikey, pin?} -> bearer token (valid ~1 month).  The
token is cached in memory and on disk.  Episode ids, default names and
localized names are cached with a TTL so repeated Sonarr polls do not
re-hit TVDB.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("yt-tvdb")

TVDB_API = os.environ.get("TVDB_API", "https://api4.thetvdb.com/v4")
TVDB_API_KEY = os.environ.get("TVDB_API_KEY", "")
TVDB_PIN = os.environ.get("TVDB_PIN", "")
TVDB_TIMEOUT = float(os.environ.get("TVDB_TIMEOUT", "5"))
TOKEN_FILE = os.environ.get(
    "TVDB_TOKEN_FILE",
    os.path.join(os.path.expanduser("~"), ".cache", "yt-indexarr", "tvdb_token.json"),
)
CACHE_FILE = os.environ.get(
    "TVDB_CACHE_FILE",
    os.path.join(os.path.expanduser("~"), ".cache", "yt-indexarr", "tvdb.json"),
)
CACHE_TTL = int(os.environ.get("TVDB_CACHE_TTL", str(7 * 24 * 3600)))
TOKEN_TTL = 27 * 24 * 3600  # tokens last ~1 month; refresh slightly early

# ISO 639-1 -> TheTVDB (ISO 639-2 terminology) code.  Unknown codes are
# ignored and the default (English) title is used instead.
LANG_MAP = {
    "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita",
    "pt": "por", "nl": "nld", "sv": "swe", "no": "nor", "da": "dan",
    "fi": "fin", "pl": "pol", "tr": "tur", "ru": "rus", "uk": "ukr",
    "cs": "ces", "sk": "slk", "hu": "hun", "ro": "ron", "bg": "bul",
    "el": "ell", "ar": "ara", "he": "heb", "hi": "hin", "ur": "urd",
    "ja": "jpn", "ko": "kor", "zh": "zho", "th": "tha", "vi": "vie",
    "id": "ind", "ms": "msa", "ca": "cat", "eu": "eus", "hr": "hrv",
    "sr": "srp", "sl": "slv", "et": "est", "lv": "lav", "lt": "lit",
    "is": "isl", "ga": "gle", "gl": "glg", "fa": "fas", "bn": "ben",
    "ta": "tam", "te": "tel", "ml": "mal", "kn": "kan", "mr": "mar",
    "pa": "pan", "gu": "guj", "my": "mya", "km": "khm", "sw": "swa",
}


def enabled():
    """True when a TVDB API key is configured (localized titles available)."""
    return bool((TVDB_API_KEY or "").strip())


def _to_tvdb_lang(code):
    """Map an ISO 639-1 code (or pass through a 3-letter TVDB code)."""
    code = (code or "").strip().lower()
    if not code:
        return None
    if len(code) == 3:
        return code
    return LANG_MAP.get(code)


_token = {"value": None, "ts": 0}
_token_lock = threading.Lock()
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
        log.warning("could not write TVDB cache: %s", CACHE_FILE)


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


def _save_token(token):
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as fh:
            json.dump({"ts": time.time(), "token": token}, fh)
    except OSError:
        log.warning("could not write TVDB token cache: %s", TOKEN_FILE)


def _get_token():
    with _token_lock:
        if _token["value"] and time.time() - _token["ts"] < TOKEN_TTL:
            return _token["value"]
    try:
        with open(TOKEN_FILE) as fh:
            data = json.load(fh)
        if data.get("token") and time.time() - data.get("ts", 0) < TOKEN_TTL:
            with _token_lock:
                _token["value"] = data["token"]
                _token["ts"] = data["ts"]
            return data["token"]
    except (OSError, ValueError):
        pass
    return None


def _login():
    body = {"apikey": TVDB_API_KEY}
    if (TVDB_PIN or "").strip():
        body["pin"] = TVDB_PIN.strip()
    req = urllib.request.Request(
        TVDB_API.rstrip("/") + "/login",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TVDB_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = (data.get("data") or {}).get("token")
    except (OSError, ValueError):
        log.warning("TVDB login failed (check TVDB_API_KEY/TVDB_PIN)")
        return None
    if token:
        with _token_lock:
            _token["value"] = token
            _token["ts"] = time.time()
        _save_token(token)
    return token


def _request(path, _retry=True):
    token = _get_token()
    if not token:
        token = _login()
        if not token:
            return None
    req = urllib.request.Request(
        TVDB_API.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TVDB_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry:
            with _token_lock:
                _token["value"] = None
                _token["ts"] = 0
            try:
                os.remove(TOKEN_FILE)
            except OSError:
                pass
            return _request(path, _retry=False)
        if e.code in (400, 404):
            return None
        log.warning("TVDB request failed %s (%s)", path, e.code)
        return None
    except OSError:
        log.warning("TVDB request failed: %s", path)
        return None


def _episode(tvdbid, season, number):
    """Return {id, name, runtime} for one episode, cached; None on failure."""
    key = f"ep:{tvdbid}:{season}:{number}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = None
    path = (
        f"/series/{tvdbid}/episodes/default?page=0"
        f"&season={season}&episodeNumber={number}"
    )
    data = _request(path)
    if data:
        episodes = (data.get("data") or {}).get("episodes") or []
        if episodes:
            e0 = episodes[0]
            result = {
                "id": e0.get("id"),
                "name": e0.get("name"),
                "runtime": e0.get("runtime"),
            }
    _cache_set(key, result)
    return result


def episode_runtime(tvdbid, season: int, number: int):
    """Episode runtime in minutes, or None when TVDB is unconfigured/unknown."""
    if not enabled():
        return None
    ep = _episode(tvdbid, season, number)
    if not ep or not ep.get("name"):
        return None
    try:
        return int(ep["runtime"])
    except (TypeError, ValueError):
        return None


def episode_name(tvdbid, season, number, lang2=None):
    """Episode title in the requested language, or None.

    lang2 is an ISO 639-1 code (e.g. 'fr'); None returns the default
    (English) name.
    """
    ep = _episode(tvdbid, season, number)
    if not ep or not ep.get("id"):
        return None
    if not lang2:
        return ep.get("name")
    lang3 = _to_tvdb_lang(lang2)
    if not lang3:
        return None
    key = f"tr:{ep['id']}:{lang3}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = None
    data = _request(f"/episodes/{ep['id']}/translations/{lang3}")
    if data:
        result = (data.get("data") or {}).get("name")
    _cache_set(key, result)
    return result


def resolve_titles(tvdbid, season, number, languages):
    """List of episode-title match targets, deduped; empty if unavailable.

    Localized titles for the requested languages (ISO 639-1) come first, then
    the default (English) name as a safety net.  Returns [] when TVDB is not
    configured or the episode cannot be found, so callers can fall back.
    """
    if not enabled():
        return []
    titles = []
    for lang in languages:
        name = episode_name(tvdbid, season, number, lang)
        if name and name not in titles:
            log.info(
                "TVDB: tvdbid=%s s%se%s %s -> %r",
                tvdbid, season, number, lang, name,
            )
            titles.append(name)
    default = episode_name(tvdbid, season, number)
    if default and default not in titles:
        titles.append(default)
    return titles


def season_episodes(tvdbid, season):
    """Return {episode_number: {name, runtime}} for a whole season; {} on failure.

    Runtime is in minutes when TheTVDB reports it, else None.  Used by the
    download-client spoof to map playlist videos to episodes (and reject
    extras) when renaming a downloaded season pack.  Returns {} when TVDB is
    not configured or the season cannot be found, so callers can fall back.
    """
    if not enabled():
        return {}
    key = f"se2:{(tvdbid or '').strip()}:{season}"
    cached = _cache_get(key)
    if cached is not None:
        # JSON cache keys round-trip as strings; episode numbers must stay
        # ints (callers format them, e.g. S03E%02d, and compare to ints).
        return {int(k): v for k, v in cached.items()}
    result = {}
    page = 0
    while page < 50:
        data = _request(
            f"/series/{tvdbid}/episodes/default?page={page}&season={season}"
        )
        if not data:
            break
        episodes = (data.get("data") or {}).get("episodes") or []
        if not episodes:
            break
        for ep in episodes:
            num = ep.get("number")
            name = ep.get("name")
            if num is not None and name:
                result[int(num)] = {
                    "name": name,
                    "runtime": ep.get("runtime"),
                }
        links = data.get("links") or {}
        if not links.get("next"):
            break
        page += 1
    _cache_set(key, result)
    return result
