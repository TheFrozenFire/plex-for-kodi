# coding=utf-8
"""Prepare the focused episode's playback decision before the click.

The decision request and the metadata reload it depends on are safe to run
early: they do not start playback. A click reuses a decision that is still
for the same item and offset. ``fast_playback_start`` defaults on.

The decision is made with its own session id. If that session is not played,
it is released: when focus moves to another item, when the screen closes,
or after ``playback_prep_hold`` seconds (hidden, default 300). A session
that playback has taken is never released here.
"""
from __future__ import absolute_import

import threading
import time
import uuid
from urllib.parse import quote

_LOCK = threading.Lock()
_WAKE = threading.Event()
_WORKER = None
_GEN = 0
_PENDING = None
_PREP = None
_LEASES = {}
_RELEASED = set()
_TTL = 20.0
_DEBOUNCE = 0.35
_WAIT = 0.5
_HOLD_DEFAULT = 300
_STOP_PATH = "/video/:/transcode/universal/stop?session={0}"
_stop = False


def _aborting():
    try:
        from lib import util
        return bool(util.MONITOR.abortRequested())
    except Exception:
        return False


def _stopping():
    return _stop or _aborting()


def enabled():
    try:
        from lib import util
        return util.getSetting("fast_playback_start", True)
    except Exception:
        return False


def hold_seconds():
    """How long an unused prefetched session may live. Hidden setting, seconds."""
    try:
        from lib import util
        return max(0, int(util.getSetting("playback_prep_hold", _HOLD_DEFAULT)))
    except Exception:
        return _HOLD_DEFAULT


def schedule(video, offset=0):
    """Debounce a background decision for ``video`` at ``offset`` milliseconds."""
    if not enabled() or video is None:
        return
    global _GEN, _PENDING
    key = _key(video, offset)
    with _LOCK:
        fresh = _PREP is not None and _PREP.get("key") == key and time.time() - _PREP["at"] < _TTL
        if fresh:
            return
        _GEN += 1
        _PENDING = (_GEN, video, int(offset or 0))
        _mark_drop_locked(key, "focus")
    _ensure_worker()
    _WAKE.set()


def cancel():
    """The screen closed or focus left episodes. Does not block on the network."""
    global _GEN, _PENDING
    with _LOCK:
        pending = _PENDING is not None
        dropping = False
        for lease in _LEASES.values():
            if lease["adopted"]:
                continue
            if lease.get("drop") != "close":
                lease["drop"] = "close"
                dropping = True
        if not dropping and not pending:
            return
        _GEN += 1
        _PENDING = None
    _ensure_worker()
    _WAKE.set()


def take(video, offset=0):
    """Return a fresh decision for this item, once, or None.

    Taking it marks the session as playing so a later cleanup will not stop it.
    """
    global _PREP
    if not enabled() or video is None:
        return None
    key = _key(video, offset)
    with _LOCK:
        got = _PREP
        if not got or got.get("key") != key:
            return None
        if got.get("session") in _RELEASED:
            _PREP = None
            return None
        if time.time() - got["at"] > _TTL:
            return None
        if _PREP is not got:
            return None
        _PREP = None
        lease = _LEASES.get(got.get("session"))
        if lease is not None:
            lease["adopted"] = True
            lease["drop"] = None
            _LEASES.pop(got.get("session"), None)
    return got["obj"]


def metadata_is_fresh(video):
    if not enabled() or video is None:
        return False
    stamped = getattr(video, "_pm4k_soft_at", 0) or 0
    return time.time() - stamped < _TTL


def release_expired():
    """Stop unused sessions whose hold window has elapsed."""
    now = time.time()
    with _LOCK:
        due = [lease["session"] for lease in _LEASES.values()
               if not lease["adopted"] and now >= lease["deadline"]]
    for session in due:
        lease = _claim(session, "timeout")
        if lease is not None:
            _stop_session(lease, "timeout")


def flush_drops():
    """Stop sessions that focus or a closed screen already gave up."""
    with _LOCK:
        due = [(lease["session"], lease.get("drop") or "focus")
               for lease in _LEASES.values() if lease.get("drop") and not lease["adopted"]]
    for session, reason in due:
        lease = _claim(session, reason)
        if lease is not None:
            _stop_session(lease, reason)


def _key(video, offset):
    return (str(getattr(video, "ratingKey", "") or ""), int(offset or 0))


def shutdown():
    """Unblock the prep worker and wait for it. Safe to call more than once."""
    global _stop
    _stop = True
    _WAKE.set()
    with _LOCK:
        worker = _WORKER
    if worker is not None:
        worker.join(timeout=2)


def _ensure_worker():
    global _WORKER
    if _stop:
        return
    with _LOCK:
        if _stop:
            return
        worker = _WORKER
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(target=_worker, name="pm4k-playback-prep", daemon=True)
        _WORKER = worker
        worker.start()


def _mark_drop_locked(keep_key, reason):
    for lease in _LEASES.values():
        if lease["adopted"] or lease.get("key") == keep_key:
            continue
        lease["drop"] = reason


def _worker():
    while not _stopping():
        timeout = _seconds_until_deadline()
        slice_ = _WAIT if timeout is None else min(_WAIT, timeout)
        signaled = _WAKE.wait(slice_)
        if _stopping():
            return
        if not signaled:
            if timeout is not None and timeout <= _WAIT:
                release_expired()
            continue
        _WAKE.clear()
        if _stopping():
            return
        flush_drops()
        while not _stopping() and _WAKE.wait(min(_WAIT, _DEBOUNCE)):
            _WAKE.clear()
            if _stopping():
                return
            flush_drops()
        if _stopping():
            return
        with _LOCK:
            pending = _PENDING
            gen = _GEN
        if _WAKE.is_set() or not pending or pending[0] != gen:
            continue
        _prepare(gen, pending[1], pending[2])


def _seconds_until_deadline():
    with _LOCK:
        times = [lease["deadline"] for lease in _LEASES.values() if not lease["adopted"]]
    if not times:
        return None
    return max(0.0, min(times) - time.time())


def _prepare(gen, video, offset):
    global _PREP
    session = str(uuid.uuid4())
    server = _server_of(video)
    decided = None
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
        player = plexplayer.PlexPlayer(video, offset, forceUpdate=True, session_id=session)
        player.build()
        decided = player.getServerDecision()
    except Exception:
        _stop_session({"session": session, "server": server, "obj": None}, "error")
        return
    with _LOCK:
        if _GEN != gen:
            abandoned = True
        else:
            abandoned = False
            key = _key(video, offset)
            previous = _PREP
            _PREP = {"key": key, "obj": decided, "at": time.time(), "session": session}
            _LEASES[session] = {
                "session": session,
                "server": server,
                "key": key,
                "obj": decided,
                "adopted": False,
                "drop": None,
                "deadline": time.time() + hold_seconds(),
            }
            if previous and previous.get("session") and previous.get("session") != session:
                prev = _LEASES.get(previous["session"])
                if prev is not None and not prev["adopted"]:
                    prev["drop"] = "replaced"
    if abandoned:
        _stop_session({"session": session, "server": server, "obj": decided}, "replaced")
    flush_drops()


def _server_of(video):
    try:
        return video.getServer()
    except Exception:
        return None


def _claim(session, reason):
    """Return the lease when this caller should stop it. None to leave it alone."""
    global _PREP
    owned = _session_is_playing(session)
    if owned is None:
        with _LOCK:
            lease = _LEASES.get(session)
            if lease is not None and not lease["adopted"]:
                lease["deadline"] = time.time() + 30
                lease["drop"] = None
        _log_cleanup(reason, session, skipped=True)
        return None
    if owned:
        with _LOCK:
            _LEASES.pop(session, None)
        _log_cleanup(reason, session, skipped=True)
        return None
    with _LOCK:
        lease = _LEASES.get(session)
        if lease is None or lease["adopted"]:
            return None
        if reason == "timeout" and time.time() < lease["deadline"]:
            return None
        if reason != "timeout" and not lease.get("drop"):
            return None
        _LEASES.pop(session, None)
        _RELEASED.add(session)
        if _PREP is not None and _PREP.get("session") == session:
            _PREP = None
        return lease


def _session_is_playing_body(session, obj=None):
    """True when playback is using ``session``, False when it is not, None if unknown."""
    try:
        from lib import player
        active = player.PLAYER
        if getattr(active, "sessionID", None) == session:
            return True
        current = getattr(active, "playerObject", None)
        if current is not None and (current is obj or getattr(current, "sessionID", None) == session):
            return True
        meta = getattr(current, "metadata", None)
        for url in getattr(meta, "streamUrls", None) or []:
            if session and str(session) in str(url):
                return True
    except Exception:
        return None
    return False


def _perform_stop_body(server, path):
    from plexnet import plexrequest
    plexrequest.PlexRequest(server, path).getWithTimeout(5)


_session_is_playing = _session_is_playing_body
_perform_stop = _perform_stop_body


def _stop_session(lease, reason):
    session = lease.get("session")
    if not session:
        return
    owned = _session_is_playing(session, lease.get("obj"))
    if owned is None or owned:
        _log_cleanup(reason, session, skipped=True)
        return
    path = _STOP_PATH.format(quote(str(session), safe=""))
    _log_cleanup(reason, session, skipped=False)
    try:
        _perform_stop(lease.get("server"), path)
    except Exception:
        return


def _log_cleanup(reason, session, skipped):
    action = "skip" if skipped else "release"
    try:
        from lib import util
        util.DEBUG_LOG("Playback prep: {0} reason={1}", action, reason)
    except Exception:
        pass
    try:
        from lib import timing
        if timing.timing_enabled():
            timing._emit("TIMING PREP action={0} reason={1} session={2}".format(action, reason, session))
    except Exception:
        pass


def _reset_for_tests():
    global _GEN, _PENDING, _PREP, _stop, _session_is_playing, _perform_stop
    shutdown()
    _stop = False
    with _LOCK:
        _GEN += 1
        _PENDING = None
        _PREP = None
        _LEASES.clear()
        _RELEASED.clear()
    _WAKE.clear()
    _session_is_playing = _session_is_playing_body
    _perform_stop = _perform_stop_body
