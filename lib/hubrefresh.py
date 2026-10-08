# coding=utf-8
"""When to reload Continue Watching after playback, and when to hold a row.

The stopped timeline request is the ordering guarantee. Plex applies the
watch position as it handles ``/:/timeline``; the hub list is not part of
that response. The Continue Watching read waits until that request finishes
(a body, an error, or a timeout). A timeout still counts: waiting forever
is worse, and there is no second acknowledgement to wait for.

The rest of the hubs start ``REST_DELAY`` seconds after that read is
allowed, so new movies and episodes show up without another five-minute
wait. Continue Watching is not part of that second pass.

Painting happens on the add-on script thread: the thread that runs the
home window event loop. Kodi's invoker does not use Python's main
thread for that, so the ident is captured from those callbacks.

A later watch always arms another refresh. There is no minimum watch
length: a short play can still change On Deck and Continue Watching.
"""
from __future__ import absolute_import

import threading
import time


REST_DELAY = 2.0

# Clean hub identifiers. ``home.continue`` matches the ``.continue`` suffix.
_EXACT = frozenset((
    "continueWatching",
    "continue",
    "ondeck",
    "home.continue",
    "home.ondeck",
    "watchlist.continueWatching",
))

_LOCK = threading.Lock()
_pending = None
_generation = 0
_cw_started = False
_rest_at = None
# threading.get_ident() of the home window event loop. None until a
# window callback records it. Never filled in from a worker or cron tick.
_script_ident = None


def reset():
    """Drop sequencer state. Tests and shutdown."""
    global _pending, _generation, _cw_started, _rest_at, _script_ident
    with _LOCK:
        _pending = None
        _generation = 0
        _cw_started = False
        _rest_at = None
        _script_ident = None


def note_script_thread():
    """Remember the thread running the home window event loop.

    Call this from window callbacks (init, show, action, focus, click).
    A cron tick or a pool worker must not call it: whichever thread
    calls it becomes the only thread allowed to paint hub updates.
    """
    global _script_ident
    ident = threading.get_ident()
    with _LOCK:
        _script_ident = ident


def on_script_thread():
    """True only after the event loop has been seen on this thread.

    Unknown is not the script thread. A worker must not paint just
    because the ident has not been captured yet, and Python's main
    thread is not a stand-in for the add-on script.
    """
    with _LOCK:
        ident = _script_ident
    return ident is not None and ident == threading.get_ident()


def is_continue_hub(identifier):
    """True for Continue Watching, On Deck, and in-progress rows."""
    if not identifier:
        return False
    if identifier in _EXACT:
        return True
    return (
        identifier.endswith(".inprogress")
        or identifier.endswith(".ondeck")
        or identifier.endswith(".continue")
    )


def item_keys(items):
    """Rating keys in row order. Missing keys are skipped."""
    keys = []
    for obj in items or ():
        rk = getattr(obj, "ratingKey", None)
        if rk:
            keys.append(str(rk))
    return tuple(keys)


def order_changed(old_keys, new_keys):
    return tuple(old_keys) != tuple(new_keys)


def decide(changed, focused, navigated):
    """``defer`` holds the row. ``apply`` may replace it in place.

    A focused row whose order changed is held once the user has moved, so
    the highlight is not swapped under them. Before that first move, the
    row may update, but the caller keeps the selected index: the highlight
    does not follow an item to a new slot.
    """
    if changed and focused and navigated:
        return "defer"
    return "apply"


def note_playback_started():
    """A new watch. The previous stop must not block the next refresh."""
    global _cw_started
    with _LOCK:
        _cw_started = False


def refresh_block():
    """Why a session end would not start a refresh, or None if it would."""
    with _LOCK:
        if _pending is not None:
            return "timeline-inflight"
        if _cw_started:
            return "already-started"
        return None


def note_stop_sent():
    """A video ``state=stopped`` timeline was handed to the network.

    Returns the token the matching response must carry. Any earlier
    Continue Watching or follow-up refresh is no longer current.
    """
    global _pending, _generation, _cw_started, _rest_at
    with _LOCK:
        _generation += 1
        _pending = _generation
        _cw_started = False
        _rest_at = None
        return _generation


def note_stop_acked(token, now=None):
    """The stopped timeline request finished. True if Continue Watching may start."""
    global _pending, _cw_started, _rest_at
    with _LOCK:
        if token != _pending:
            return False
        _pending = None
        if _cw_started:
            return False
        _cw_started = True
        moment = time.monotonic() if now is None else now
        _rest_at = moment + REST_DELAY
        return True


def note_session_end(now=None):
    """Video session ended. Start when this stop did not send a timeline.

    A timeline already in flight owns the refresh (its ack starts the
    read). A stop that never sends one — the server still has this item
    as stopped, or playback state never changed — still has to refresh.
    The previous stop's one-shot flag does not apply after a new watch.
    Starting here bumps the generation so an older read cannot paint.
    """
    global _generation, _cw_started, _rest_at
    with _LOCK:
        if _pending is not None or _cw_started:
            return False
        _generation += 1
        _cw_started = True
        moment = time.monotonic() if now is None else now
        _rest_at = moment + REST_DELAY
        return True


def current_generation():
    with _LOCK:
        return _generation


def same_generation(generation):
    with _LOCK:
        return generation == _generation


def rest_due(now=None):
    """True once, when the follow-up refresh may start."""
    global _rest_at
    with _LOCK:
        if _rest_at is None:
            return False
        moment = time.monotonic() if now is None else now
        if moment < _rest_at:
            return False
        _rest_at = None
        return True
