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

import tvdb
import tvmaze

APP_VERSION = "v4.6.7"
WEBAPI_VERSION = "2.11.3"

HOST = os.environ.get("YT_QBT_HOST", "0.0.0.0")
PORT = int(os.environ.get("YT_QBT_PORT", "9177"))
USERNAME = os.environ.get("YT_QBT_USERNAME", "admin")
PASSWORD = os.environ.get("YT_QBT_PASSWORD", "adminadmin")
REQUIRE_AUTH = os.environ.get("YT_QBT_REQUIRE_AUTH", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
YTDLP = os.environ.get("YT_QBT_YTDLP", "yt-dlp")
PLAYER_CLIENT = os.environ.get(
    "YT_QBT_PLAYER_CLIENT",
    "tv_embedded,android_vr,web,tv_simply,android").strip()
# Optional PO token provider (e.g. http://pot:4416 from the bgutil
# bgutil-ytdlp-pot-provider container). Empty disables the feature. Requires
# the bgutil-ytdlp-pot-provider plugin + a JS runtime (deno/node) installed.
POT_PROVIDER = os.environ.get("YT_QBT_POT_PROVIDER", "").strip()
DL_DIR = os.environ.get("YT_QBT_DL_DIR", os.path.expanduser("~/downloads"))
# +/- seconds around an episode's reported runtime when checking whether a
# playlist video is the real episode (vs a compilation/extras).  Matches the
# search script's EP_DURATION_BUFFER.  Only used when season metadata exists.
EP_DURATION_BUFFER = int(os.environ.get("YT_QBT_EP_DURATION_BUFFER", "60"))
# Video codec preference for downloads: auto|av1|vp9|h264.  Auto keeps the
# stock yt-dlp behaviour; the others restrict the video stream to that codec
# and fall back to "best" when unavailable.  Shared with the indexer's size
# estimate via the same YT_CODEC var, so the reported size tracks the codec.
CODEC = os.environ.get("YT_CODEC", "auto").strip().lower()
# Output container for merged downloads: mkv (default, holds every codec
# incl. av01/vp9 + opus natively) or mp4 (max direct-play compatibility, but
# cannot hold opus audio).  Only honoured when ffmpeg is available; without it
# downloads stay single-file mp4 and the container cannot be changed.
OUTPUT_EXT = os.environ.get("YT_QBT_OUTPUT_EXT", "mkv").strip().lower()
if OUTPUT_EXT not in ("mkv", "mp4"):
    OUTPUT_EXT = "mkv"
LOG_LEVEL = os.environ.get("YT_QBT_LOG_LEVEL", "INFO")

log = logging.getLogger("yt-qbt")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_PUBLIC_PATHS = {
    "/api/v2/auth/login",
    "/api/v2/app/version",
    "/api/v2/app/webapiVersion",
}


def _sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(name)).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "video"


def _parse_magnet(url):
    """Extract btih hash, dn, x.ytindexer and tvdbid from a magnet, or None."""
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
    return info


def _b64url_decode(value):
    try:
        pad = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


class Torrent:
    def __init__(self, hash_, name, save_path, category, tags, real_url, paused,
                 tvdbid=""):
        self.hash = hash_
        self.name = name
        self.save_path = save_path
        self.category = category or ""
        self.tags = tags or ""
        self.real_url = real_url
        self.tvdbid = tvdbid or ""
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
    merge = ["--merge-output-format", OUTPUT_EXT]
    if OUTPUT_EXT == "mp4":
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


def _run_season_download(t):
    """Download a whole season playlist, one file per episode."""
    dl_dir = _download_dir(t)
    try:
        os.makedirs(dl_dir, exist_ok=True)
    except OSError as e:
        with t.lock:
            t.state = "error"
            t.error = f"cannot create download dir: {e}"
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

    fmt, merge = _format_spec(CODEC, bool(shutil.which("ffmpeg")))
    extractor_args = []
    if PLAYER_CLIENT:
        extractor_args = ["--extractor-args",
                          f"youtube:player_client={PLAYER_CLIENT}"]
    if POT_PROVIDER:
        extractor_args += ["--extractor-args",
                           f"youtubepot-bgutilhttp:base_url={POT_PROVIDER}"]

    total = len(plan)
    done = 0
    completed_files = []
    total_size = 0
    log.info("season download start: %s (%d episodes)", t.hash[:8], total)
    for ep, url, _vtitle in plan:
        ep_tag = f"S{season:02d}E{ep:02d}"
        out = os.path.join(dl_dir, f"{series} {ep_tag}.%(ext)s")
        cmd = [YTDLP, "--newline", "--no-playlist", *extractor_args,
               "-f", fmt, *merge, "-o", out, "--", url]
        log.info("season item %s/%s: %s", done + 1, total, ep_tag)
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
            log.warning("cannot launch yt-dlp for %s: %s", ep_tag, e)
            done += 1
            continue
        with t.lock:
            t.proc = proc
            t.state = "downloading"
        pct = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
        for line in proc.stdout:
            m = pct.search(line)
            if m:
                slot = float(m.group(1)) / 100.0
                with t.lock:
                    t.progress = min(0.999, (done + slot) / total)
        rc = proc.wait()
        if rc != 0:
            log.warning("season item %s failed (rc=%s)", ep_tag, rc)
            done += 1
            continue
        import glob
        found = sorted(glob.glob(os.path.join(dl_dir, f"{series} {ep_tag}.*")))
        if found:
            fp = found[0]
            completed_files.append(fp)
            total_size += os.path.getsize(fp)
        done += 1
        with t.lock:
            t.progress = (done / total) if done < total else 1.0

    if not completed_files:
        with t.lock:
            t.state = "error"
            t.error = "no episode files downloaded"
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
    log.info(
        "season download complete: %s (%d episodes, %d MB)",
        t.hash[:8], len(completed_files), total_size // (1024 * 1024),
    )


def _run_download(t):
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
    fmt, merge = _format_spec(CODEC, bool(shutil.which("ffmpeg")))
    # YouTube blocks some videos on yt-dlp's default player client (seen as
    # "This video is not available"); the android client usually still serves
    # them. Set YT_QBT_PLAYER_CLIENT to "" to disable the override.
    extractor_args = []
    if PLAYER_CLIENT:
        extractor_args = ["--extractor-args",
                          f"youtube:player_client={PLAYER_CLIENT}"]
    if POT_PROVIDER:
        extractor_args += ["--extractor-args",
                           f"youtubepot-bgutilhttp:base_url={POT_PROVIDER}"]
    cmd = [YTDLP, "--newline", "--no-playlist", *extractor_args,
           "-f", fmt, *merge, "-o", out, "--", t.real_url]
    log.info("download start: %s (%s)", t.hash[:8], t.name)
    with t.lock:
        t.state = "downloading"
        t._started = True
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
        with t.lock:
            t.state = "error"
            t.error = f"cannot launch yt-dlp: {e}"
        return
    with t.lock:
        t.proc = proc
    pct = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
    for line in proc.stdout:
        m = pct.search(line)
        if m:
            with t.lock:
                t.progress = min(0.999, float(m.group(1)) / 100.0)
    rc = proc.wait()
    if rc == 0:
        found = None
        try:
            for fn in os.listdir(dl_dir):
                fp = os.path.join(dl_dir, fn)
                if os.path.isfile(fp) and fn.lower().endswith(
                    (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".flv", ".mov")
                ):
                    found = fp
                    break
        except OSError:
            pass
        with t.lock:
            t.progress = 1.0
            t.state = "uploading"
            t.completion_on = int(time.time())
            t.last_activity = t.completion_on
            if found:
                t.file_path = found
                t.size = os.path.getsize(found)
                t.downloaded = t.size
                t.uploaded = t.size
                t.ratio = 1.0
        log.info("download complete: %s (%s)", t.hash[:8], found or t.name)
    else:
        with t.lock:
            t.state = "error"
            t.error = f"yt-dlp exited with code {rc}"
        log.warning("download failed: %s (%s)", t.hash[:8], t.error)


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
    # A single-video torrent points content_path at the file; a season pack
    # points it at the folder so Sonarr scans and imports every episode.
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
        "save_path": _download_dir(t),
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

    # -- routing ---------------------------------------------------------

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return urllib.parse.parse_qs(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        form = self._read_form() if self.command == "POST" else {}
        params = {k: (v[0] if v else "") for k, v in {**qs, **form}.items()}

        if not path.startswith("/api/v2/"):
            self._text("Not Found", 404)
            return
        if path not in _PUBLIC_PATHS and REQUIRE_AUTH and not _valid_sid(self._sid()):
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
            if params.get("username", "") == USERNAME and params.get("password", "") == PASSWORD:
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
        t = Torrent(hash_, name, save_path, category, tags, real_url, paused,
                    tvdbid=tvdbid)
        with _registry_lock:
            _registry[hash_] = t
        log.info(
            "torrent added: %s (%s) -> %s%s",
            hash_[:8], name, real_url,
            f" [season tvdbid={tvdbid}]" if tvdbid else "",
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
        "save_path": _download_dir(t),
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
    log.info(
        "qBittorrent spoofer listening on http://%s:%d (auth: %s, yt-dlp: %s)",
        HOST, PORT, "on" if REQUIRE_AUTH else "off", YTDLP,
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
