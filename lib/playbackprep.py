# coding=utf-8
"""Prepare the focused episode's playback decision before the click.

The decision request and the metadata reload it depends on are safe to run
early: they do not start playback. A click reuses a decision that is still
for the same item and offset. ``fast_playback_start`` defaults on.
"""
from __future__ import absolute_import

import threading
import time

_LOCK = threading.Lock()
_WAKE = threading.Event()
_WORKER = None
_GEN = 0
_PENDING = None
_PREP = None
_TTL = 20.0
_DEBOUNCE = 0.35


def enabled():
    try:
        from lib import util
        return util.getSetting("fast_playback_start", True)
    except Exception:
        return False


def schedule(video, offset=0):
    """Debounce a background decision for ``video`` at ``offset`` milliseconds."""
    if not enabled() or video is None:
        return
    global _GEN, _PENDING, _WORKER
    with _LOCK:
        _GEN += 1
        _PENDING = (_GEN, video, int(offset or 0))
        worker = _WORKER
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_worker, name="pm4k-playback-prep", daemon=True)
            _WORKER = worker
            worker.start()
    _WAKE.set()


def take(video, offset=0):
    """Return a fresh decision for this item, once, or None."""
    global _PREP
    if not enabled() or video is None:
        return None
    key = _key(video, offset)
    with _LOCK:
        got = _PREP
    if not got or got["key"] != key:
        return None
    if time.time() - got["at"] > _TTL:
        return None
    with _LOCK:
        if _PREP is got:
            _PREP = None
    return got["obj"]


def metadata_is_fresh(video):
    if not enabled() or video is None:
        return False
    stamped = getattr(video, "_pm4k_soft_at", 0) or 0
    return time.time() - stamped < _TTL


def _key(video, offset):
    return (str(getattr(video, "ratingKey", "") or ""), int(offset or 0))


def _worker():
    while True:
        _WAKE.wait()
        _WAKE.clear()
        while True:
            time.sleep(_DEBOUNCE)
            if not _WAKE.is_set():
                break
            _WAKE.clear()
        with _LOCK:
            pending = _PENDING
            gen = _GEN
        if _WAKE.is_set() or not pending or pending[0] != gen:
            continue
        _prepare(gen, pending[1], pending[2])


def _prepare(gen, video, offset):
    try:
        from plexnet import plexplayer
        with _LOCK:
            if _GEN != gen:
                return
        if not metadata_is_fresh(video):
            video.softReload(includeChapters=1)
            video._pm4k_soft_at = time.time()
        with _LOCK:
            if _GEN != gen:
                return
        player = plexplayer.PlexPlayer(video, offset, forceUpdate=True)
        player.build()
        decided = player.getServerDecision()
    except Exception:
        return
    global _PREP
    with _LOCK:
        if _GEN != gen:
            return
        _PREP = {"key": _key(video, offset), "obj": decided, "at": time.time()}


def _reset_for_tests():
    global _GEN, _PENDING, _PREP
    with _LOCK:
        _GEN += 1
        _PENDING = None
        _PREP = None
    _WAKE.clear()
