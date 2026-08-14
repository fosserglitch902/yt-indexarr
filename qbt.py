#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qBittorrent Web API v2 spoofer that downloads YouTube via yt-dlp.

Sonarr (or any qBittorrent client) connects to this on its own port (default
9177) as if it were a qBittorrent instance.  Adding a "torrent" decodes the
magnet carrier emitted by indexer.py:

    magnet:?xt=urn:btih:<sha1(url)>&dn=<release title>&x.ytindexer=<base64url(url)>

and downloads the video with yt-dlp, faking torrent progress/state so Sonarr
can track, import and eventually delete it.  Magents without the x.ytindexer
field are rejected (we are not a real torrent client).

Season packs: a magnet whose x.ytindexer is a YouTube *playlist* URL (emitted
by the indexer's full-season search) is downloaded as a whole season.  The
playlist is enumerated, each video is mapped to an episode (TVDB title match,
falling back to playlist order when TVDB is unconfigured) and files are named
Bluey S03E01.mp4 etc. so Sonarr can import every episode.

Run it separately from indexer.py so the two stay on different ports:
    python3 qbt.py
"""

import base64
import collections
import glob
import json
import logging
import os
import re
import shutil
import secrets
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import tvdb
import tvmaze

APP_VERSION = "v4.6.7"
WEBAPI_VERSION = "2.11.3"

HOST = os.environ.get("YT_QBT_HOST", "0.0.0.0")
PORT = int(os.environ.get("YT_QBT_PORT", "9177"))
YTDLP = os.environ.get("YT_QBT_YTDLP", "yt-dlp")
PLAYER_CLIENT = os.environ.get(
    "YT_QBT_PLAYER_CLIENT",
    "tv_embedded,android_vr,web,tv_simply,android").strip()
DL_DIR = os.environ.get("YT_QBT_DL_DIR", os.path.expanduser("~/downloads"))
# +/- seconds around an episode's reported runtime when checking whether a
# playlist video is the real episode (vs a compilation/extras).  Matches the
# search script's EP_DURATION_BUFFER.  Only used when season metadata exists.
EP_DURATION_BUFFER = int(os.environ.get("YT_QBT_EP_DURATION_BUFFER", "60"))
# Maximum concurrent yt-dlp processes across all torrents.  Season packs and
# single videos each hold one slot while downloading; extra torrents wait in a
# Sonarr-visible "Queued" state.  Lower values throttle YouTube requests to
# reduce rate-limit / bot-check failures on rapid sequential downloads.
MAX_PARALLEL = max(1, int(os.environ.get("YT_QBT_MAX_PARALLEL", "2")))
# Pacing between YouTube requests, in seconds.  Applied once before each
# episode download and again before every retry rung of the fallback ladder, so
# even without cookies a 5s gap between requests looks far less bot-like to
# YouTube (fewer 429/403s).  Tune here if the server's IP reputation improves.
DL_DELAY = 5.0

log = logging.getLogger("yt-qbt")
logging.basicConfig(
    level=getattr(
        logging,
        os.environ.get("YT_QBT_LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    ),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# The settings below resolve lazily via config (env > /data/config.json >
# default) so the dashboard UI can change them without a restart; docker-compose
# env vars still win over any UI edit.
def _username():
    return config.get("YT_QBT_USERNAME", "admin")


def _password():
    return config.get("YT_QBT_PASSWORD", "adminadmin")


def _auth_enabled():
    return config.get("YT_QBT_REQUIRE_AUTH", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _pot_provider():
    # POT_PROVIDER is shared with the search script (one value, one name).  The
    # old YT_QBT_POT_PROVIDER name still works but is deprecated.
    return (config.get("POT_PROVIDER", "").strip()
            or os.environ.get("YT_QBT_POT_PROVIDER", "").strip())


def _codec():
    # Video codec preference for downloads: auto|av1|vp9|h264.  Shared with the
    # indexer's size estimate via the same YT_CODEC var.
    return config.get("YT_CODEC", "auto").strip().lower()


def _output_ext():
    # Output container for merged downloads: mkv (default, holds every codec
    # incl. av01/vp9 + opus natively) or mp4 (max direct-play compatibility, but
    # cannot hold opus audio).  Only honoured when ffmpeg is available.
    ext = config.get("YT_QBT_OUTPUT_EXT", "mkv").strip().lower()
    return ext if ext in ("mkv", "mp4") else "mkv"


def _cookies_path():
    # Optional Netscape-format browser cookies file passed to yt-dlp as
    # --cookies.  Empty disables the flag.
    return config.get("YT_QBT_COOKIES", "").strip()


def _cookies_arg():
    path = _cookies_path()
    return [f"--cookies", path] if path else []


def _apply_log_level():
    log.setLevel(getattr(
        logging,
        config.get("YT_QBT_LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    ))


if os.environ.get("YT_QBT_POT_PROVIDER", "").strip():
    log.warning(
        "YT_QBT_POT_PROVIDER is deprecated and will be removed; "
        "set POT_PROVIDER instead"
    )

_DL_SEM = threading.BoundedSemaphore(MAX_PARALLEL)

if _cookies_path():
    log.info("using yt-dlp cookies from %s", _cookies_path())

_PUBLIC_PATHS = {
    "/api/v2/auth/login",
    "/api/v2/app/version",
    "/api/v2/app/webapiVersion",
}

# Dashboard UI: a tiny vanilla-JS page served from ./ui plus a small JSON API,
# all on the downloader's own port.  No framework, no database, no image
# proxying (posters/thumbnails are URL strings the browser fetches directly).
UI_DIR = os.environ.get(
    "YT_QBT_UI_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
)
COOKIES_FILE_DEFAULT = "/data/cookies.txt"
_UI_MIME = {
    "html": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
    "json": "application/json",
    "svg": "image/svg+xml",
    "png": "image/png",
    "ico": "image/x-icon",
}

# Settings exposed to the dashboard: (env key, kind, secret, options, default).
# kind is text | password | select | bool.  docker-compose env vars always win
# over a UI edit (config resolves env first); each row flags "set by compose".
UI_SETTINGS = [
    ("TVDB_API_KEY", "password", True, None, ""),
    ("POT_PROVIDER", "text", False, None, ""),
    ("YT_QBT_OUTPUT_EXT", "select", False, ["mkv", "mp4"], "mkv"),
    ("YT_CODEC", "select", False, ["auto", "av1", "vp9", "h264"], "auto"),
    ("YT_INDEXER_LOG_LEVEL", "select", False,
     ["DEBUG", "INFO", "WARNING", "ERROR"], "INFO"),
    ("YT_QBT_LOG_LEVEL", "select", False,
     ["DEBUG", "INFO", "WARNING", "ERROR"], "INFO"),
    ("YT_INDEXER_API_KEY", "text", True, None, "youtubeindexer"),
    ("YT_INDEXER_REQUIRE_KEY", "bool", False, None, "0"),
    ("YT_QBT_USERNAME", "text", False, None, "admin"),
    ("YT_QBT_PASSWORD", "password", True, None, "adminadmin"),
    ("YT_QBT_REQUIRE_AUTH", "bool", False, None, "0"),
]
_UI_SETTINGS_MAP = {s[0]: s for s in UI_SETTINGS}


def _sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(name)).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "video"


# Total-size parser for yt-dlp --newline progress lines, e.g.
#   [download]  45.3% of ~432.10MiB at    8.12MiB/s ETA 00:38
# Returns bytes or None. The "~" marks an approximate size (server estimate);
# we treat it as good enough for reporting progress.
_SIZE_RE = re.compile(r"of\s+~?\s*([\d.]+)\s*(ti|gi|mi|ki)?b\b", re.IGNORECASE)
_SIZE_MULT = {"": 1, "ki": 1 << 10, "mi": 1 << 20, "gi": 1 << 30, "ti": 1 << 40}


def _parse_total_size(line):
    m = _SIZE_RE.search(line)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return int(val * _SIZE_MULT[(m.group(2) or "").lower()])


def _parse_magnet(url):
    """Extract btih hash, dn, x.ytindexer, tvdbid and channel, or None."""
    if not url.lower().startswith("magnet:?"):
        return None
    try:
        params = urllib.parse.parse_qs(url[len("magnet:?"):])
    except ValueError:
        return None
    info = {}
    for xt in params.get("xt", []):
        if xt.startswith("urn:btih:"):
            info["hash"] = xt[len("urn:btih:"):].lower()
            break
    if params.get("dn"):
        info["dn"] = params["dn"][0]
    if params.get("x.ytindexer"):
        info["x.ytindexer"] = params["x.ytindexer"][0]
    if params.get("x.ytindexertvdbid"):
        info["tvdbid"] = params["x.ytindexertvdbid"][0]
    if params.get("x.ytindexerchannel"):
        info["channel"] = params["x.ytindexerchannel"][0]
    return info


def _b64url_decode(value):
    try:
        pad = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


class Torrent:
    def __init__(self, hash_, name, save_path, category, tags, real_url, paused,
                 tvdbid="", channel=""):
        self.hash = hash_
        self.name = name
        self.save_path = save_path
        self.category = category or ""
        self.tags = tags or ""
        self.real_url = real_url
        self.tvdbid = tvdbid or ""
        self.channel = channel or ""
        self.poster_url = ""
        self.thumbnail_url = ""
        self.episodes = []
        self.lock = threading.Lock()
        self.progress = 0.0
        self.state = "pausedDL" if paused else "forcedDL"
        self.downloaded = 0
        self.uploaded = 0
        self.size = 0
        self.ratio = 0.0
        self.added_on = int(time.time())
        self.completion_on = 0
        self.last_activity = self.added_on
        self.error = None
        self.file_path = None
        self.file_paths = []
        self.proc = None
        self._started = False


_registry = {}
_registry_lock = threading.Lock()
_categories = {}
_categories_lock = threading.Lock()
_sids = set()
_sid_lock = threading.Lock()


def _issue_sid():
    sid = secrets.token_hex(16)
    with _sid_lock:
        _sids.add(sid)
    return sid


def _valid_sid(sid):
    if not sid:
        return False
    with _sid_lock:
        return sid in _sids


def _lookup(hash_):
    hash_ = (hash_ or "").strip().lower()
    if not hash_:
        return None
    with _registry_lock:
        t = _registry.get(hash_)
        if t:
            return t
        for h, t in _registry.items():
            if h.startswith(hash_):
                return t
    return None


def _categories_dict():
    with _categories_lock:
        return {
            name: {"name": name, "savePath": save_path, "error": ""}
            for name, save_path in _categories.items()
        }


def _download_dir(t):
    return os.path.join(t.save_path, _sanitize(t.name))


# ---- dashboard history (persisted, survives restarts) -----------------------

HISTORY_FILE = os.environ.get("YT_QBT_HISTORY_FILE", "/data/history.json")
_HISTORY_FLUSH_EVERY = 5.0
_history = {}
_history_lock = threading.Lock()
_history_dirty = False


def _torrent_record(t):
    """Serializable snapshot of a torrent for the history file / UI."""
    with t.lock:
        rec = {
            "hash": t.hash,
            "name": t.name,
            "title": t.name,
            "url": t.real_url,
            "is_playlist": _is_playlist_url(t.real_url),
            "channel": t.channel,
            "category": t.category,
            "tags": t.tags,
            "tvdbid": t.tvdbid,
            "poster_url": t.poster_url,
            "thumbnail_url": t.thumbnail_url,
            "state": t.state,
            "size": t.size,
            "progress": t.progress,
            "added_on": t.added_on,
            "completion_on": t.completion_on,
            "error": t.error,
            "episodes": [
                {
                    "num": e.get("num"),
                    "index": e.get("num"),
                    "url": e.get("url", ""),
                    "title": e.get("title", ""),
                    "state": e.get("state", ""),
                    "size": e.get("size", 0),
                    "completion_on": e.get("completion_on", 0),
                    "error": e.get("error"),
                }
                for e in t.episodes
            ],
        }
    return rec


def _record_torrent(t):
    """Refresh t's record in the in-memory history (flushed periodically)."""
    global _history_dirty
    with _history_lock:
        _history[t.hash] = _torrent_record(t)
        _history_dirty = True


def _history_save():
    global _history_dirty
    with _history_lock:
        data = list(_history.values())
        _history_dirty = False
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE) or ".", exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, HISTORY_FILE)
    except OSError:
        log.warning("could not write history file: %s", HISTORY_FILE)


def _history_load():
    try:
        with open(HISTORY_FILE) as fh:
            data = json.load(fh)
        with _history_lock:
            for rec in data:
                h = rec.get("hash")
                if h:
                    _history[h] = rec
    except (OSError, ValueError, AttributeError):
        pass


def _history_loop():
    while True:
        time.sleep(_HISTORY_FLUSH_EVERY)
        if _history_dirty:
            _history_save()


# ---- series poster / thumbnail metadata -------------------------------------

def _youtube_id(url):
    m = re.search(r"(?:[?&]v=|/watch/|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _youtube_thumbnail(url):
    vid = _youtube_id(url)
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""


def _fill_metadata(t):
    """Best-effort series poster / thumbnail for a torrent (background)."""
    if t.poster_url or t.thumbnail_url:
        return
    thumb = _youtube_thumbnail(t.real_url)
    poster = ""
    if t.tvdbid and str(t.tvdbid).isdigit():
        try:
            poster = tvmaze.poster_by_tvdbid(t.tvdbid) or ""
        except Exception:  # noqa: BLE001
            poster = ""
    with t.lock:
        t.thumbnail_url = t.thumbnail_url or thumb
        t.poster_url = poster
    _record_torrent(t)


def _is_playlist_url(url):
    """True for a YouTube playlist URL (full-season pack carrier)."""
    return "youtube.com/playlist?list=" in url or (
        "list=PL" in url and "youtu.be" in url
    )


def _parse_season_info(name):
    """Split a season-pack torrent name into (series, season).

    "Bluey S03 WEB" -> ("Bluey", 3); "Bluey Season 3 WEB" -> ("Bluey", 3).
    Returns (name, None) when no season token is found.
    """
    m = re.match(r"^(.*?)\s+S(\d{1,2})(?:\s|$)", name)
    if m:
        return m.group(1).strip(), int(m.group(2))
    m = re.search(r"\bSeason\s+(\d{1,2})\b", name, re.I)
    if m:
        series = re.sub(r"\bSeason\s+\d{1,2}\b.*$", "", name, re.I).strip()
        return series, int(m.group(1))
    return name, None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _episode_score(title, ep_name):
    """0..1 similarity between a video title and an episode name."""
    t = _norm(title)
    e = _norm(ep_name)
    if not e:
        return 0.0
    if e == t:
        return 1.0
    if e in t:
        return 0.9
    if t in e:
        return 0.8
    ew = [w for w in e.split() if len(w) >= 3]
    if not ew:
        return 0.0
    matched = sum(1 for w in ew if w in t)
    return matched / len(ew)


def _explicit_episode(title, season):
    """Episode number from an S03E24-style token in the title, or None."""
    m = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", title, re.I)
    if m and int(m.group(1)) == season:
        return int(m.group(2))
    return None


def _enumerate_playlist(url):
    """Flat-enumerate a playlist -> list of {id, title, url, duration}."""
    cmd = [
        YTDLP, "--ignore-config", "--flat-playlist", "--dump-json",
        "--no-warnings", "--retries", "3", "--", url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return []
    vids = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not d.get("id") or d.get("_type") == "playlist":
            continue
        vids.append({
            "id": d["id"],
            "title": d.get("title") or "",
            "url": d.get("webpage_url")
            or f"https://www.youtube.com/watch?v={d['id']}",
            "duration": int(d.get("duration") or 0),
        })
    return vids


_EXTRAS_TOKENS = (
    "behind the scenes", "behind-the-scenes", "bts", "trailer", "teaser",
    "interview", "bloopers", "reaction", "review", "commentary",
    "documentary", "making of", "deleted scene", "recap", "promo",
)


def _looks_like_extra(title):
    """True when a video title carries obvious non-episode content."""
    t = title.lower()
    return any(tok in t for tok in _EXTRAS_TOKENS)


def _duration_ok(video_dur, runtime_min, buffer_sec):
    """True when a video's duration matches an episode runtime (+/- buffer).

    Unknown runtime/duration falls through as acceptable so we never drop a
    real episode just because metadata is missing a value.
    """
    if not runtime_min:
        return True
    expected = int(runtime_min) * 60
    if not video_dur:
        return True
    return abs(video_dur - expected) <= max(buffer_sec, 0)


def _map_videos_to_episodes(vids, tvdbid, season):
    """Map playlist videos -> [(episode_number, url, title)].

    Season metadata (explicit S03E24 tokens, episode titles + runtimes) comes
    from TheTVDB when a key is configured, else the keyless TVMaze fallback.
    Videos whose duration does not match the mapped episode's runtime, or that
    look like extras/BTS/interviews, are skipped so non-episode playlist
    content is not downloaded.  Falls back to playlist order only when no
    season metadata is available at all.
    """
    if not season:
        return []
    season_eps = {}
    source = ""
    if tvdbid:
        if tvdb.enabled():
            season_eps = tvdb.season_episodes(tvdbid, season)
            if season_eps:
                source = "TVDB"
        if not season_eps:
            season_eps = tvmaze.season_episodes(tvdbid, season)
            if season_eps:
                source = "TVMaze"
    if not season_eps:
        log.warning(
            "no season metadata (tvdbid=%r); using playlist order", tvdbid
        )
        return [(i, v["url"], v["title"]) for i, v in enumerate(vids, 1)]

    used = set()
    plan = []
    for v in vids:
        if not v.get("title"):
            continue
        if _looks_like_extra(v["title"]):
            log.info("season skip (extra): %s", v["title"][:60])
            continue
        ep = _explicit_episode(v["title"], season)
        matched = None
        if ep is not None and ep in season_eps:
            matched = (ep, season_eps[ep].get("runtime"))
        else:
            best_ep, best_score = None, 0.0
            for num, meta in season_eps.items():
                sc = _episode_score(v["title"], meta.get("name") or "")
                if sc > best_score:
                    best_ep, best_score = num, sc
            if best_ep is not None and best_score >= 0.6:
                matched = (best_ep, season_eps[best_ep].get("runtime"))
        if not matched:
            log.info("season skip (no episode match): %s", v["title"][:60])
            continue
        ep_num, runtime = matched
        # Defensive: cache keys can arrive as strings (JSON round-trip);
        # episode numbers must be ints for S03E%02d formatting and sorting.
        ep_num = int(ep_num)
        if ep_num in used:
            log.info("season skip (already mapped): %s", v["title"][:60])
            continue
        if not _duration_ok(v.get("duration"), runtime, EP_DURATION_BUFFER):
            log.info(
                "season skip (duration %ss vs %s min): %s",
                v.get("duration") or "?", runtime, v["title"][:60],
            )
            continue
        used.add(ep_num)
        plan.append((ep_num, v["url"], v["title"]))
    log.info(
        "season mapped via %s: %d episodes from %d playlist videos "
        "(buffer %ds)", source, len(plan), len(vids), EP_DURATION_BUFFER,
    )
    plan.sort(key=lambda x: x[0])
    return plan


_VIDEO_PREFIX = {
    "av1": "av01",
    "vp9": "vp9",
    "h264": "avc1",
}


def _format_spec(codec, have_ffmpeg):
    """Return (fmt, merge) for a video codec preference and ffmpeg availability.

    auto keeps yt-dlp's stock selection.  av1/vp9/h264 restrict the video
    stream to that codec's VCODEC prefix and fall back to the generic best
    format if the codec is not offered (e.g. h264 on a 4K video, or any
    codec on a SABR-restricted video served at 360p only).

    The output container comes from OUTPUT_EXT.  mkv (default) holds every
    codec YouTube serves, so the stream selection stays codec-agnostic and
    av01/vp9+opus pairs flow through.  mp4 cannot hold opus audio, so the
    selection biases to mp4/m4a-paired streams (h264/AAC) before merging.
    Without ffmpeg there is no merge step: a single-file (combined) stream is
    used and the container cannot be changed, so it stays mp4.
    """
    if codec not in _VIDEO_PREFIX:
        codec = "auto"
    if not have_ffmpeg:
        # Single-file only (no merge).  YouTube serves a combined stream just
        # for 360p, so anything better needs ffmpeg regardless of codec.
        if codec == "auto":
            return "b[ext=mp4]/b", []
        pref = _VIDEO_PREFIX[codec]
        return f"b[ext=mp4][vcodec^={pref}]/b[ext=mp4]/b", []
    merge = ["--merge-output-format", _output_ext()]
    if _output_ext() == "mp4":
        # mp4 can't hold opus audio, so prefer m4a-paired mp4 streams.
        base = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio"
        if codec == "auto":
            return f"{base}/b[ext=mp4]/b", merge
        pref = _VIDEO_PREFIX[codec]
        return (f"bestvideo[vcodec^={pref}]+bestaudio[ext=m4a]/"
                f"bestvideo[vcodec^={pref}]+bestaudio/{base}/b[ext=mp4]/b",
                merge)
    # mkv holds every codec; keep the selection codec-agnostic so the best
    # stream (incl. av01/vp9 + opus) flows through instead of h264/AAC.
    if codec == "auto":
        return "bestvideo+bestaudio/b[ext=mp4]/b", merge
    pref = _VIDEO_PREFIX[codec]
    return (f"bestvideo[vcodec^={pref}]+bestaudio/"
            f"bestvideo+bestaudio/b[ext=mp4]/b", merge)


def _is_fragment(name):
    """True for yt-dlp merge intermediates ('x.f401.mp4', 'x.f251.m4a'),
    partial files ('.part') and sidecar info files ('.ytdl'), which must never
    be treated as a finished episode file."""
    base = os.path.basename(name)
    if base.endswith((".part", ".ytdl")):
        return True
    return bool(re.search(r"\.f\d+\.", base))


def _fallback_attempts(fmt, merge):
    """(label, fmt, merge) ladder for one download item, best quality first.

    SABR-only videos (yt-dlp#12482) can list high-res formats once a PO token
    is presented yet still refuse the stream fetch with HTTP 403, so three
    PO-token attempts (initial + two retries, each spaced DL_DELAY apart) and a
    guaranteed combined-stream fallback are tried before the item is given up
    on.  SABR-restricted videos expose their 360p combined mp4 to every client,
    so the final rung always resolves.
    """
    ladder = [("best", fmt, merge)]
    ladder += [(f"retry{i}", fmt, merge) for i in (1, 2)]
    ladder.append(("safe", "b[ext=mp4]/b", ()))
    return ladder


_COOKIE_HINTS_ISSUED = set()


def _cookies_stale_hint(tail):
    """Once-per-run actionable hint when a failure tail suggests cookies.

    yt-dlp prints characteristic markers when a session is rejected; when one
    appears the operator should re-export cookies.txt (or, with no cookies
    configured, consider exporting some).  The hint fires at most once per run
    per kind so a whole failing season doesn't spam the logs.
    """
    blob = " | ".join(tail) if tail else ""
    if not re.search(r"HTTP Error 403|unable to download video data|"
                     r"Sign in to confirm|Sign in with Google", blob,
                     re.IGNORECASE):
        return ""
    key = "stale" if _cookies_path() else "none"
    if key in _COOKIE_HINTS_ISSUED:
        return ""
    _COOKIE_HINTS_ISSUED.add(key)
    if _cookies_path():
        return ("cookies may be stale - re-export cookies.txt; "
                "if 403s persist the video is likely still SABR-restricted")
    return ("403 detected - consider setting YT_QBT_COOKIES "
            "(see README SABR note)")


def _run_ytdlp(t, cmd, on_line):
    """Run one yt-dlp invocation, wiring t.proc/t.state and streaming stdout.

    on_line(line, m) is called for every line where m is the pct match (None
    when the line is not progress output).  Returns (rc, tail) where tail is a
    deque of the last non-progress lines (the real failure reason).
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return -1, collections.deque([f"cannot launch yt-dlp: {e}"])
    with t.lock:
        t.proc = proc
        t.state = "downloading"
    pct = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
    tail = collections.deque(maxlen=10)
    for line in proc.stdout:
        m = pct.search(line)
        if on_line:
            on_line(line, m)
        if not m:
            s = line.strip()
            if s:
                tail.append(s)
    rc = proc.wait()
    return rc, tail


def _run_season_download(t):
    """Download a whole season playlist, one file per episode."""
    dl_dir = _download_dir(t)
    try:
        os.makedirs(dl_dir, exist_ok=True)
    except OSError as e:
        with t.lock:
            t.state = "error"
            t.error = f"cannot create download dir: {e}"
        _record_torrent(t)
        _history_save()
        return
    series, season = _parse_season_info(t.name)
    if not season:
        with t.lock:
            t.state = "error"
            t.error = f"cannot parse season from title: {t.name!r}"
        return
    season = int(season)  # defensive: must be int for S%02d formatting
    vids = _enumerate_playlist(t.real_url)
    if not vids:
        with t.lock:
            t.state = "error"
            t.error = "cannot enumerate playlist"
        return
    plan = _map_videos_to_episodes(vids, getattr(t, "tvdbid", ""), season)
    if not plan:
        with t.lock:
            t.state = "error"
            t.error = "no playlist videos mapped to episodes"
        return

    fmt, merge = _format_spec(_codec(), bool(shutil.which("ffmpeg")))
    extractor_args = []
    if PLAYER_CLIENT:
        extractor_args = ["--extractor-args",
                          f"youtube:player_client={PLAYER_CLIENT}"]
    if _pot_provider():
        extractor_args += ["--extractor-args",
                           f"youtubepot-bgutilhttp:base_url={_pot_provider()}"]

    total = len(plan)
    done = 0
    completed_files = []
    total_size = 0
    log.info("season download start: %s (%d episodes)", t.hash[:8], total)
    for ep, url, vtitle in plan:
        ep_tag = f"S{season:02d}E{ep:02d}"
        out = os.path.join(dl_dir, f"{series} {ep_tag}.%(ext)s")
        ep_rec = {
            "num": ep, "url": url, "title": vtitle,
            "state": "downloading", "size": 0, "completion_on": 0, "error": None,
        }
        t.episodes.append(ep_rec)
        _record_torrent(t)
        log.info(
            "season item start %s/%s: %s (%s)",
            done + 1, total, ep_tag, vtitle,
        )
        item_started = time.time()
        ep_size = 0
        fp = None

        def on_line(line, m):
            nonlocal ep_size
            if not m:
                return
            slot = float(m.group(1)) / 100.0
            if not ep_size:
                ep_size = _parse_total_size(line) or 0
            with t.lock:
                t.progress = min(0.999, (done + slot) / total)
                if ep_size:
                    # Rough whole-season estimate: completed episodes get
                    # their real size; the rest are guessed at this episode's
                    # size so the Sonarr bar moves smoothly.
                    est = int(ep_size * (done + slot))
                    t.size = est
                    t.downloaded = int(est * t.progress)

        # Pacing: DL_DELAY before each episode so rapid sequential downloads
        # don't trip YouTube rate-limit / bot checks.
        time.sleep(DL_DELAY)
        for i, (label, afmt, amerge) in enumerate(
                _fallback_attempts(fmt, merge)):
            if i:
                # Pacing: DL_DELAY between retry rungs as well.
                time.sleep(DL_DELAY)
            cmd = [YTDLP, "--newline", "--no-playlist", *extractor_args,
                   *_cookies_arg(), "-f", afmt, *amerge, "-o", out, "--", url]
            rc, tail = _run_ytdlp(t, cmd, on_line)
            if rc == -1:
                log.warning("cannot launch yt-dlp for %s: %s",
                            ep_tag, " | ".join(tail))
                break
            if rc != 0:
                hint = _cookies_stale_hint(tail)
                log.warning(
                    "season item %s (%s) failed (rc=%s): %s%s",
                    ep_tag, label, rc, " | ".join(tail) or "no yt-dlp output",
                    (f" | {hint}" if hint else ""),
                )
                continue
            found = sorted(
                fp_ for fp_ in glob.glob(
                    os.path.join(dl_dir, f"{series} {ep_tag}.*")
                ) if not _is_fragment(fp_)
            )
            if not found:
                hint = _cookies_stale_hint(tail)
                log.warning(
                    "season item %s (%s): rc=0 but no output file found (%s)%s",
                    ep_tag, label, " | ".join(tail) or "no yt-dlp output",
                    (f" | {hint}" if hint else ""),
                )
                continue
            fp = found[0]
            completed_files.append(fp)
            size = os.path.getsize(fp)
            total_size += size
            ep_rec["state"] = "completed"
            ep_rec["size"] = size
            ep_rec["completion_on"] = int(time.time())
            _record_torrent(t)
            log.info(
                "season item complete %s/%s: %s -> %s (%.0f MB, %ds)",
                done + 1, total, ep_tag, os.path.basename(fp),
                size / (1024 * 1024), int(time.time() - item_started),
            )
            break
        if fp is None:
            ep_rec["state"] = "error"
            ep_rec["error"] = "all attempts failed"
            _record_torrent(t)
        done += 1
        with t.lock:
            t.progress = (done / total) if done < total else 1.0

    if not completed_files:
        with t.lock:
            t.state = "error"
            t.error = "no episode files downloaded"
        _record_torrent(t)
        _history_save()
        return
    with t.lock:
        t.progress = 1.0
        t.state = "uploading"
        t.completion_on = int(time.time())
        t.last_activity = t.completion_on
        t.file_paths = completed_files
        t.file_path = completed_files[0]
        t.size = total_size
        t.downloaded = total_size
        t.uploaded = total_size
        t.ratio = 1.0
    _record_torrent(t)
    _history_save()
    log.info(
        "season download complete: %s (%d episodes, %d MB)",
        t.hash[:8], len(completed_files), total_size // (1024 * 1024),
    )


def _run_download(t):
    # Gate concurrent downloads: at most MAX_PARALLEL yt-dlp processes run at
    # once across all torrents.  A torrent waiting for a slot reports queuedDL
    # so Sonarr shows it queued, and it resumes when a slot frees.
    got = _DL_SEM.acquire(timeout=0)
    if not got:
        with t.lock:
            t.state = "queuedDL"
        _DL_SEM.acquire()
        with t.lock:
            t.state = "downloading"
    try:
        if not _lookup(t.hash):
            return
        with t.lock:
            if t.state == "pausedDL":
                return
        _run_download_locked(t)
    finally:
        _DL_SEM.release()


def _run_download_locked(t):
    if _is_playlist_url(t.real_url):
        _run_season_download(t)
        return
    dl_dir = _download_dir(t)
    try:
        os.makedirs(dl_dir, exist_ok=True)
    except OSError as e:
        with t.lock:
            t.state = "error"
            t.error = f"cannot create download dir: {e}"
        return
    base = _sanitize(t.name)
    out = os.path.join(dl_dir, base + ".%(ext)s")
    fmt, merge = _format_spec(_codec(), bool(shutil.which("ffmpeg")))
    # YouTube blocks some videos on yt-dlp's default player client (seen as
    # "This video is not available"); the android client usually still serves
    # them. Set YT_QBT_PLAYER_CLIENT to "" to disable the override.
    extractor_args = []
    if PLAYER_CLIENT:
        extractor_args = ["--extractor-args",
                          f"youtube:player_client={PLAYER_CLIENT}"]
    if _pot_provider():
        extractor_args += ["--extractor-args",
                           f"youtubepot-bgutilhttp:base_url={_pot_provider()}"]
    log.info("download start: %s (%s)", t.hash[:8], t.name)
    with t.lock:
        t.state = "downloading"
        t._started = True

    def on_line(line, m):
        if not m:
            return
        with t.lock:
            slot = float(m.group(1)) / 100.0
            t.progress = min(0.999, slot)
            total = _parse_total_size(line)
            if total and t.size == 0:
                t.size = total
            if t.size:
                # scale downloaded/amount_left live so Sonarr's activity
                # bar animates; exactness isn't important, closeness is.
                t.downloaded = int(t.size * t.progress)

    found = None
    # Pacing: DL_DELAY before the download and between retry rungs.
    time.sleep(DL_DELAY)
    for i, (label, afmt, amerge) in enumerate(
            _fallback_attempts(fmt, merge)):
        if i:
            time.sleep(DL_DELAY)
        cmd = [YTDLP, "--newline", "--no-playlist", *extractor_args,
               *_cookies_arg(), "-f", afmt, *amerge, "-o", out, "--", t.real_url]
        rc, tail = _run_ytdlp(t, cmd, on_line)
        if rc == -1:
            with t.lock:
                t.state = "error"
                t.error = f"cannot launch yt-dlp: {' | '.join(tail)}"
            log.warning("download %s: %s", t.hash[:8], t.error)
            _record_torrent(t)
            _history_save()
            return
        if rc != 0:
            hint = _cookies_stale_hint(tail)
            log.warning(
                "download %s (%s) failed (rc=%s): %s%s",
                t.hash[:8], label, rc, " | ".join(tail) or "no yt-dlp output",
                (f" | {hint}" if hint else ""),
            )
            continue
        found = None
        try:
            for fn in os.listdir(dl_dir):
                fp = os.path.join(dl_dir, fn)
                if os.path.isfile(fp) and fn.lower().endswith(
                    (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".flv", ".mov")
                ) and not _is_fragment(fp):
                    found = fp
                    break
        except OSError:
            pass
        if not found:
            hint = _cookies_stale_hint(tail)
            log.warning(
                "download %s (%s): rc=0 but no output file found (%s)%s",
                t.hash[:8], label, " | ".join(tail) or "no yt-dlp output",
                (f" | {hint}" if hint else ""),
            )
            continue
        break
    if not found:
        hint = _cookies_stale_hint(tail)
        with t.lock:
            t.state = "error"
            t.error = f"yt-dlp exited with code {rc}"
        log.warning(
            "download failed: %s (%s): %s%s",
            t.hash[:8], t.error, " | ".join(tail) or "no yt-dlp output",
            (f" | {hint}" if hint else ""),
        )
        _record_torrent(t)
        _history_save()
        return
    with t.lock:
        t.progress = 1.0
        t.state = "uploading"
        t.completion_on = int(time.time())
        t.last_activity = t.completion_on
        t.file_path = found
        t.size = os.path.getsize(found)
        t.downloaded = t.size
        t.uploaded = t.size
        t.ratio = 1.0
    _record_torrent(t)
    _history_save()
    log.info("download complete: %s (%s)", t.hash[:8], found)


def _start_download(t):
    if not t._started:
        threading.Thread(target=_run_download, args=(t,), daemon=True).start()
    else:
        # resume a paused download
        proc = t.proc
        if proc:
            try:
                proc.send_signal(subprocess.signal.SIGCONT)
            except (ProcessLookupError, OSError):
                pass
        with t.lock:
            t.state = "downloading"


def _torrent_dict(t):
    with t.lock:
        progress = t.progress
        state = t.state
        downloaded = t.downloaded
        size = t.size
        error = t.error
        file_path = t.file_path
        file_paths = list(t.file_paths)
    eta = -1 if progress >= 1.0 else 8640000
    # save_path must stay the base download dir while content_path points at
    # the torrent's actual content. Sonarr refuses to import when the two are
    # equal ("Path matches client base download directory"); a season pack
    # points content_path at the per-torrent folder so every episode imports.
    if file_paths:
        content_path = _download_dir(t)
    elif file_path:
        content_path = file_path
    else:
        content_path = _download_dir(t)
    return {
        "added_on": t.added_on,
        "amount_left": int(size * (1 - progress)),
        "auto_tmm": False,
        "availability": 1.0,
        "category": t.category,
        "completed": int(downloaded),
        "completion_on": t.completion_on,
        "content_path": content_path,
        "dl_limit": -1,
        "dlspeed": 0,
        "downloaded": int(downloaded),
        "downloaded_session": int(downloaded),
        "eta": eta,
        "f_l_piece_prio": False,
        "force_start": True,
        "hash": t.hash,
        "infohash_v1": t.hash,
        "infohash_v2": "",
        "last_activity": t.last_activity,
        "magnet_uri": f"magnet:?xt=urn:btih:{t.hash}&dn={urllib.parse.quote(t.name)}",
        "max_ratio": -1,
        "max_seeding_time": -1,
        "name": t.name,
        "num_complete": 0,
        "num_incomplete": 0,
        "num_leechs": 0,
        "num_seeds": 0,
        "priority": 0,
        "progress": progress,
        "ratio": t.ratio,
        "ratio_limit": -2,
        "save_path": t.save_path,
        "seeding_time": 0 if progress < 1.0 else int(time.time() - (t.completion_on or t.added_on)),
        "seeding_time_limit": -2,
        "seen_complete": 0,
        "seq_dl": False,
        "size": int(size),
        "state": "error" if (state == "error" or error) else state,
        "super_seeding": False,
        "tags": t.tags,
        "time_active": 0,
        "total_size": int(size),
        "tracker": "",
        "trackers_count": 0,
        "up_limit": -1,
        "uploaded": int(t.uploaded),
        "uploaded_session": int(t.uploaded),
        "upspeed": 0,
    }


def _filter_torrents(params):
    with _registry_lock:
        torrents = list(_registry.values())
    hash_ = params.get("hash")
    if hash_:
        torrents = [t for t in torrents if t.hash == hash_]
    hashes = params.get("hashes")
    if hashes:
        wanted = set(h for h in hashes.split(",") if h)
        torrents = [t for t in torrents if t.hash in wanted]
    category = params.get("category")
    if category:
        torrents = [t for t in torrents if t.category == category]
    tag = params.get("tag")
    if tag:
        torrents = [t for t in torrents if tag in (t.tags or "").split(",")]
    filt = params.get("filter")
    if filt == "completed":
        torrents = [t for t in torrents if t.progress >= 1.0]
    elif filt == "downloading":
        torrents = [t for t in torrents if t.progress < 1.0]
    elif filt == "errored":
        torrents = [t for t in torrents if t.state == "error"]
    elif filt == "seeding":
        torrents = [t for t in torrents if t.progress >= 1.0]
    elif filt == "paused":
        torrents = [t for t in torrents if t.state in ("pausedDL", "pausedUP")]
    return torrents


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

    # -- helpers ---------------------------------------------------------

    def _send(self, body, ctype, status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(json.dumps(data), "application/json", status)

    def _text(self, text, status=200):
        self._send(text, "text/plain; charset=utf-8", status)

    def _sid(self):
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "SID":
                return v
        return ""

    # -- dashboard UI ----------------------------------------------------

    def _serve_static(self, path):
        if path in ("/", "/index.html"):
            rel = "index.html"
        else:
            rel = os.path.basename(path[len("/ui/"):]) if path.startswith("/ui/") else ""
        if not rel:
            self._text("Not Found", 404)
            return
        fp = os.path.join(UI_DIR, rel)
        try:
            with open(fp, "rb") as fh:
                body = fh.read()
        except OSError:
            self._text("Not Found", 404)
            return
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else "html"
        self._send(body, _UI_MIME.get(ext, "application/octet-stream"))

    def _ui_history(self):
        recs = []
        with _history_lock:
            recs = list(_history.values())
        # Overlay live registry state so in-progress torrents show fresh
        # progress/size; the persisted copy is the fallback for deleted items.
        with _registry_lock:
            live = {t.hash: _torrent_record(t) for t in _registry.values()}
        merged = {r["hash"]: r for r in recs}
        merged.update(live)
        items = sorted(merged.values(), key=lambda r: r.get("added_on", 0),
                       reverse=True)
        self._json({"items": items})

    def _ui_settings_get(self):
        out = []
        for key, kind, secret, options, default in UI_SETTINGS:
            env_set = config.is_env_set(key)
            file_set = config.file_value(key) is not None
            if secret:
                has = bool(config.get(key, default).strip())
                out.append({
                    "key": key, "kind": kind, "secret": True, "set": has,
                    "value": "", "env_set": env_set, "file_set": file_set,
                    "options": options, "default": default,
                })
            else:
                out.append({
                    "key": key, "kind": kind, "secret": False, "set": True,
                    "value": config.get(key, default), "env_set": env_set,
                    "file_set": file_set, "options": options, "default": default,
                })
        cpath = config.get("YT_QBT_COOKIES", "").strip()
        self._json({
            "settings": out,
            "cookies": {
                "active": bool(cpath),
                "path": cpath,
                "env_set": config.is_env_set("YT_QBT_COOKIES"),
                "file_set": config.file_value("YT_QBT_COOKIES") is not None,
            },
        })

    def _ui_settings_post(self, params, json_body):
        data = json_body if isinstance(json_body, dict) else params
        settings = data.get("settings")
        updates = {}
        if isinstance(settings, dict):
            for key, raw in settings.items():
                if key not in _UI_SETTINGS_MAP:
                    continue
                key2, kind, secret, options, default = _UI_SETTINGS_MAP[key]
                val = "" if raw is None else str(raw)
                if kind == "select" and val and options and val not in options:
                    self._json({"ok": False,
                                "error": f"invalid value for {key}: {val}"}, 400)
                    return
                if kind == "bool":
                    val = "1" if val.strip().lower() in ("1", "true", "yes", "on") else "0"
                updates[key] = val
        cookies = data.get("cookies")
        if isinstance(cookies, str) and cookies.strip():
            cpath = config.get("YT_QBT_COOKIES", "").strip() or COOKIES_FILE_DEFAULT
            try:
                os.makedirs(os.path.dirname(cpath) or ".", exist_ok=True)
                with open(cpath, "w") as fh:
                    fh.write(cookies)
            except OSError:
                self._json({"ok": False, "error": "cannot write cookies file"}, 500)
                return
            updates.setdefault("YT_QBT_COOKIES", cpath)
        if not updates and not (isinstance(cookies, str) and cookies.strip()):
            self._json({"ok": True})
            return
        try:
            config.set_many(updates)
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        _apply_log_level()
        self._json({"ok": True})

    # -- routing ---------------------------------------------------------

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _read_body(self):
        """Read the POST body once; return (form, json_obj)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}, None
        raw = self.rfile.read(length)
        try:
            return {}, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            try:
                return urllib.parse.parse_qs(raw.decode("utf-8")), None
            except ValueError:
                return {}, None

    def _route(self):
        _apply_log_level()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        form, json_body = self._read_body() if self.command == "POST" else ({}, None)
        params = {k: (v[0] if v else "") for k, v in {**qs, **form}.items()}

        # Dashboard UI (same port as the downloader).  Static assets stay
        # public so the page can load and prompt for login; the API endpoints
        # enforce the same SID auth as /api/v2 when auth is enabled.
        if path in ("/", "/index.html") or path.startswith("/ui/"):
            self._serve_static(path)
            return
        if path in ("/api/ui/history", "/api/ui/settings"):
            if _auth_enabled() and not _valid_sid(self._sid()):
                self._text("Forbidden", 403)
                return
            if path == "/api/ui/history":
                self._ui_history()
            elif self.command == "POST":
                self._ui_settings_post(params, json_body)
            else:
                self._ui_settings_get()
            return

        if not path.startswith("/api/v2/"):
            self._text("Not Found", 404)
            return
        if path not in _PUBLIC_PATHS and _auth_enabled() and not _valid_sid(self._sid()):
            self._text("Forbidden", 403)
            return

        route = path[len("/api/v2/"):]
        try:
            self._dispatch(route, params)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("error handling %s", route)
            try:
                self._text(f"Internal error: {e}", 500)
            except BrokenPipeError:
                pass

    def _dispatch(self, route, params):
        if route == "auth/login":
            if (params.get("username", "") == _username()
                    and params.get("password", "") == _password()):
                sid = _issue_sid()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Set-Cookie", f"SID={sid}; path=/; HttpOnly")
                body = b"Ok."
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._text("Fails.", 200)
        elif route == "auth/logout":
            self._text("Ok.", 200)
        elif route == "app/version":
            self._text(APP_VERSION)
        elif route == "app/webapiVersion":
            self._text(WEBAPI_VERSION)
        elif route == "app/buildInfo":
            self._json({"qt": "6.4.3", "libtorrent": "2.0.10.0",
                        "boost": "1.82.0", "openssl": "3.1.2", "zlib": "1.2.13"})
        elif route == "app/preferences":
            self._json({
                "save_path": DL_DIR,
                "temp_path_enabled": False,
                "dht": True,
                "pex": True,
                "queueing_enabled": True,
                "max_active_downloads": 5,
                "max_active_uploads": 5,
                "max_ratio_enabled": False,
                "max_ratio": 0,
                "max_seeding_time_enabled": False,
                "max_seeding_time": 0,
                "max_inactive_seeding_time_enabled": False,
                "max_inactive_seeding_time": 0,
                "max_ratio_act": 0,
            })
        elif route == "app/shutdown":
            self._text("Ok.", 200)
        elif route == "torrents/info":
            self._json([_torrent_dict(t) for t in _filter_torrents(params)])
        elif route == "torrents/add":
            self._add(params)
        elif route == "torrents/delete":
            self._delete(params)
        elif route == "torrents/pause":
            self._pause(params, True)
        elif route == "torrents/resume":
            self._pause(params, False)
        elif route == "torrents/recheck":
            self._text("Ok.", 200)
        elif route == "torrents/reannounce":
            self._text("Ok.", 200)
        elif route == "torrents/setShareLimits":
            self._text("Ok.", 200)
        elif route == "torrents/topPrio":
            self._text("Ok.", 200)
        elif route == "torrents/setCategory":
            self._set_category(params)
        elif route == "torrents/properties":
            t = _lookup(params.get("hash", ""))
            self._json(_properties(t) if t else {})
        elif route == "torrents/files":
            t = _lookup(params.get("hash", ""))
            self._json(_files(t) if t else [])
        elif route == "torrents/trackers":
            t = _lookup(params.get("hash", ""))
            self._json(_trackers(t) if t else [])
        elif route == "torrents/peers":
            self._json({"peers": [], "show_flags": False, "num_peers": 0,
                        "num_seeds": 0, "num_leechs": 0})
        elif route == "torrents/categories":
            self._json(_categories_dict())
        elif route == "torrents/tags":
            self._json([])
        elif route == "torrents/createCategory":
            self._create_category(params)
        elif route == "torrents/deleteCategory":
            self._delete_category(params)
        elif route == "sync/maindata":
            self._json({"rid": 1, "torrents": {}})
        elif route == "log/main":
            self._json([])
        else:
            self._text("Not Found", 404)

    # -- actions ---------------------------------------------------------

    def _add(self, params):
        urls = (params.get("urls") or "").strip()
        if not urls:
            self._text("Torrent URLs are missing.", 200)
            return
        url = urls.splitlines()[0].strip()
        info = _parse_magnet(url)
        if not info:
            self._text(f"Unsupported URL: not a magnet.", 200)
            return
        if not info.get("x.ytindexer"):
            self._text("Unsupported URL: magnet is missing x.ytindexer.", 200)
            return
        real_url = _b64url_decode(info["x.ytindexer"])
        if not real_url or not re.match(r"^https?://", real_url):
            self._text("Unsupported URL: invalid x.ytindexer.", 200)
            return
        hash_ = info.get("hash") or secrets.token_hex(20)
        name = info.get("dn") or hash_
        save_path = params.get("savepath") or DL_DIR
        paused = params.get("paused", "false").strip().lower() == "true"
        category = params.get("category", "")
        tags = params.get("tags", "")
        tvdbid = info.get("tvdbid") or ""
        channel = info.get("channel") or ""
        t = Torrent(hash_, name, save_path, category, tags, real_url, paused,
                    tvdbid=tvdbid, channel=channel)
        with _registry_lock:
            _registry[hash_] = t
        _record_torrent(t)
        threading.Thread(target=_fill_metadata, args=(t,), daemon=True).start()
        log.info(
            "torrent added: %s (%s) -> %s%s%s",
            hash_[:8], name, real_url,
            f" [season tvdbid={tvdbid}]" if tvdbid else "",
            f" [channel={channel}]" if channel else "",
        )
        if not paused:
            _start_download(t)
        self._text("Ok.", 200)

    def _delete(self, params):
        hashes = params.get("hashes", "")
        if not hashes and params.get("hash"):
            hashes = params["hash"]
        delete_files = params.get("deleteFiles", "false").strip().lower() in (
            "1", "true",
        )
        for h in (x for x in hashes.split(",") if x):
            t = _lookup(h)
            if t:
                proc = t.proc
                if proc:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                if delete_files:
                    shutil.rmtree(_download_dir(t), ignore_errors=True)
                with _registry_lock:
                    _registry.pop(t.hash, None)
                log.info("torrent deleted: %s (deleteFiles=%s)", h[:8], delete_files)
        self._text("Ok.", 200)

    def _set_category(self, params):
        cat = params.get("category", "")
        for h in (x for x in params.get("hashes", "").split(",") if x):
            t = _lookup(h)
            if t:
                with t.lock:
                    t.category = cat
        if cat:
            with _categories_lock:
                _categories.setdefault(cat, "")
        self._text("Ok.", 200)

    def _create_category(self, params):
        name = params.get("category", "").strip()
        if name:
            with _categories_lock:
                _categories[name] = params.get("savePath", "") or ""
            log.info("category created: %s", name)
        self._text("Ok.", 200)

    def _delete_category(self, params):
        name = params.get("category", "").strip()
        with _categories_lock:
            _categories.pop(name, None)
        log.info("category deleted: %s", name or "(none)")
        self._text("Ok.", 200)

    def _pause(self, params, paused):
        for h in (x for x in params.get("hashes", "").split(",") if x):
            t = _lookup(h)
            if not t:
                continue
            proc = t.proc
            if paused:
                if proc:
                    try:
                        proc.send_signal(subprocess.signal.SIGSTOP)
                    except (ProcessLookupError, OSError):
                        pass
                with t.lock:
                    t.state = "pausedDL" if t.progress < 1.0 else "pausedUP"
            else:
                if proc and t.state in ("pausedDL", "pausedUP"):
                    try:
                        proc.send_signal(subprocess.signal.SIGCONT)
                    except (ProcessLookupError, OSError):
                        pass
                with t.lock:
                    t.state = "downloading" if t.progress < 1.0 else "uploading"
        self._text("Ok.", 200)


def _properties(t):
    return {
        "save_path": t.save_path,
        "creation_date": t.added_on,
        "pieces": 0,
        "comment": "",
        "total_wasted": 0,
        "total_uploaded": int(t.uploaded),
        "total_downloaded": int(t.downloaded),
        "download_speed": 0,
        "upload_speed": 0,
        "dl_limit": -1,
        "up_limit": -1,
        "nb_connections": 0,
        "nb_connections_limit": -1,
        "share_ratio": t.ratio,
        "addition_date": t.added_on,
        "completion_date": t.completion_on,
        "created_by": "qBittorrent 4.6.7",
        "avail_download": 1,
        "avail_upload": 1,
        "total_size": int(t.size),
        "private": False,
        "piece_hashes": [],
        "last_seen": t.last_activity,
        "qbt_seed_status": "never" if t.progress < 1.0 else "complete",
        "reannounce": 0,
        "eta": 8640000 if t.progress < 1.0 else -1,
    }


def _files(t):
    paths = list(t.file_paths) if t.file_paths else (
        [t.file_path] if t.file_path else []
    )
    if not paths:
        return []
    out = []
    for fp in paths:
        try:
            size = int(os.path.getsize(fp))
        except OSError:
            size = int(t.size or 0)
        out.append({
            "name": os.path.basename(fp),
            "size": size,
            "progress": t.progress,
            "priority": 0,
            "is_seed": bool(t.progress >= 1.0),
            "piece_range": [0, 1],
            "availability": 1.0,
        })
    return out


def _trackers(t):
    return []


def main():
    _history_load()
    threading.Thread(target=_history_loop, daemon=True).start()
    log.info(
        "qBittorrent spoofer listening on http://%s:%d (auth: %s, yt-dlp: %s)",
        HOST, PORT, "on" if _auth_enabled() else "off", YTDLP,
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
