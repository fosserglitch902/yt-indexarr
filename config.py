#!/usr/bin/env python3
"""Runtime configuration shared by the indexer and the downloader.

Precedence: process environment (docker-compose) > /data/config.json (edited
through the dashboard UI) > built-in default.  Call get() at the point of use
so UI edits apply without a restart; the JSON file is only re-read when its
mtime changes (a cheap stat per call), and writes are atomic + chmod 600.
"""

import json
import os
import threading
import time

CONFIG_FILE = os.environ.get("YT_UI_CONFIG_FILE", "/data/config.json")
_REFETCH_INTERVAL = 1.0

_lock = threading.Lock()
_cached = {}
_cached_mtime = 0.0
_last_stat = 0.0


def _load():
    global _cached, _cached_mtime, _last_stat
    now = time.time()
    try:
        st = os.stat(CONFIG_FILE)
    except OSError:
        _cached, _cached_mtime, _last_stat = {}, 0.0, now
        return
    if st.st_mtime == _cached_mtime:
        if now - _last_stat < _REFETCH_INTERVAL:
            return
        _last_stat = now
        return
    _cached_mtime = st.st_mtime
    _last_stat = now
    try:
        with open(CONFIG_FILE) as fh:
            _cached = json.load(fh)
    except (OSError, ValueError):
        _cached = {}


def _save():
    global _cached_mtime, _last_stat
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_cached, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
        os.replace(tmp, CONFIG_FILE)
        st = os.stat(CONFIG_FILE)
        _cached_mtime = st.st_mtime
        _last_stat = time.time()
    except OSError:
        raise ValueError(f"cannot write config file: {CONFIG_FILE}")


def get(key, default=""):
    """Resolve a config value: env wins, then the UI file, then default."""
    env = os.environ.get(key)
    if env is not None:
        return env
    with _lock:
        _load()
        v = _cached.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def file_value(key):
    """Raw value from the config file only (None when absent)."""
    with _lock:
        _load()
        return _cached.get(key)


def is_env_set(key):
    return os.environ.get(key) is not None


def set_many(values):
    """Merge {key: value|None} into the config file (None/'' deletes)."""
    with _lock:
        _load()
        for k, v in (values or {}).items():
            if v is None or v == "":
                _cached.pop(k, None)
            else:
                _cached[k] = v
        _save()
