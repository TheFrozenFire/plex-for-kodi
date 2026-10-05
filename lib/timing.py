# coding=utf-8
"""Opt-in timing for screen transitions, HTTP calls, and artwork.

Off unless one of these is set:

- environment variable ``PM4K_TIMING=1`` (``0`` forces it off)
- addon setting ``timing_log`` (hidden; default false)
- a file ``pm4k_timing`` in the addon profile whose contents are ``1``

When off, every entry point returns immediately and does not touch the log,
the texture database, or the network. When on, lines look like::

    TIMING SPAN id=s1 name=open.show phase=begin parent=-
    TIMING SPAN id=s1 name=open.show phase=first ms=400
    TIMING REQ span=s1 method=GET endpoint={server}/library/metadata/{id} status=200 bytes=84211 ttfb_ms=180 total_ms=210 cache=miss thread=MainThread ui_blocked=1
    TIMING SPAN id=s1 name=open.show phase=full ms=5100
    TIMING ART span=s1 cache=miss ms=800 endpoint={server}/photo/:/transcode
    TIMING PLAY id=s2 phase=decision mode=direct ms=220

Hosts other than ``*.plex.tv`` are written as ``{server}``. Numeric path ids
and token-like query values are redacted. That same redaction is applied to
every logged field, including ``thread`` (some request threads are named with
the full URL). Hook failures are logged and swallowed.
"""
from __future__ import absolute_import

import contextvars
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

_TOKEN_RE = re.compile(r"(X-Plex-Token=)[^&\s]+", re.IGNORECASE)
_URL_IN_TEXT = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"((?:[^&\s]*token|identifier|session|auth)[^=&\s]*=)[^&\s]+",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"/\d+")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SENSITIVE_QUERY = re.compile(r"(token|identifier|session|auth)", re.IGNORECASE)
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_ART_TIMEOUT = 20.0
_ART_POLL = 0.25

# None until the setting/file have been read once. The env var is not cached,
# so a test (or a launcher) can flip PM4K_TIMING without restarting.
_cached = None
_span_ids = None
_span_lock = threading.Lock()
_ctx = contextvars.ContextVar("pm4k_timing_stack", default=())

_art_lock = threading.Lock()
_art_pending = []
_art_stop = threading.Event()
_art_thread = None
_art_probe = None


class _NullSpan(object):
    real = False
    id = "-"

    def mark(self, phase="first"):
        return None

    def after_tasks(self, tasks):
        return None


_NULL = _NullSpan()


def reset():
    """Forget cached state. Tests use this."""
    global _cached, _span_ids, _art_thread, _art_probe
    _cached = None
    with _span_lock:
        _span_ids = 0
    try:
        _ctx.set(())
    except Exception:
        pass
    _art_stop.set()
    thread = _art_thread
    _art_thread = None
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    with _art_lock:
        _art_pending[:] = []
    _art_stop.clear()
    _art_probe = None


def redact_token(text):
    return _TOKEN_RE.sub(r"\1****", str(text))


def redact_endpoint(url):
    """Path and query only. Private hosts become ``{server}``."""
    text = redact_token(str(url)).replace(" ", "%20")
    try:
        parts = urlsplit(text)
    except Exception:
        return _DIGITS.sub("/{id}", text.split("?", 1)[0])
    host = (parts.hostname or "").lower()
    if host.endswith(".plex.tv") or host == "plex.tv":
        label = host
    elif host:
        label = "{server}"
    else:
        label = ""
    path = _UUID.sub("{id}", parts.path or "/")
    path = _DIGITS.sub("/{id}", path)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _SENSITIVE_QUERY.search(key):
            value = "****"
        query.append("{0}={1}".format(quote(key, safe=""), quote(value, safe="*")))
    encoded = "&".join(query)
    endpoint = (label + path) if label else path
    if encoded:
        endpoint = endpoint + "?" + encoded
    return endpoint


def redact_text(value):
    """Redact tokens and URLs inside any logged field, not only ``endpoint``."""
    text = str(value)

    def _swap(match):
        try:
            return redact_endpoint(match.group(0))
        except Exception:
            return "{server}"

    text = _URL_IN_TEXT.sub(_swap, text)
    text = _QUERY_SECRET.sub(r"\1****", text)
    return redact_token(text)


def _env_flag():
    raw = os.environ.get("PM4K_TIMING")
    if raw is None or raw == "":
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def _setting_enabled():
    try:
        from .settings_util import getSetting
        return bool(getSetting("timing_log", False))
    except Exception:
        return False


def _file_enabled():
    try:
        from .util import PROFILE
        path = os.path.join(PROFILE, "pm4k_timing")
        if not os.path.exists(path):
            return False
        with open(path, "r") as handle:
            return handle.read().strip().lower() in _TRUE
    except Exception:
        return False


def timing_enabled():
    flag = _env_flag()
    if flag is not None:
        return flag
    global _cached
    if _cached is None:
        _cached = _setting_enabled() or _file_enabled()
    return _cached


def _emit(message):
    try:
        from .logging import log
        text = redact_text(message).replace("\n", " ").replace("\r", " ")
        log(text)
    except Exception:
        pass


def _log_hook_failure(where, exc):
    """A timing bug must not escape into the add-on. The record is redacted."""
    try:
        detail = redact_text("{0}: {1}".format(type(exc).__name__, exc))
        _emit("TIMING HOOK name={0} error={1}".format(where, detail))
    except Exception:
        pass


def _ms(started):
    return (time.perf_counter() - started) * 1000.0


def _next_span_id():
    global _span_ids
    with _span_lock:
        if _span_ids is None:
            _span_ids = 0
        _span_ids += 1
        return "s{0}".format(_span_ids)


class Span(object):
    real = True

    def __init__(self, name):
        self.name = name
        self.id = _next_span_id()
        stack = _ctx.get()
        self.parent = stack[-1] if stack else None
        self.started = time.perf_counter()
        self._marks = set()
        self._waiting = 0
        self._defer = False
        self._finished = False
        self._exit_done = False
        self._lock = threading.Lock()

    def mark(self, phase="first"):
        try:
            with self._lock:
                if phase in self._marks or self._finished:
                    return
                self._marks.add(phase)
            _emit("TIMING SPAN id={0} name={1} phase={2} ms={3:.0f}".format(
                self.id, redact_text(self.name), redact_text(phase), _ms(self.started)))
        except Exception as exc:
            _log_hook_failure("mark", exc)

    def after_tasks(self, tasks):
        """Hold ``phase=full`` until each of ``tasks`` has finished."""
        try:
            with self._lock:
                self._defer = True
                for task in tasks:
                    if getattr(task, "_timing_release", None) is self:
                        continue
                    task._timing_release = self
                    self._waiting += 1
        except Exception as exc:
            _log_hook_failure("after_tasks", exc)

    def _drop_from_thread(self):
        stack = _ctx.get()
        if self in stack:
            _ctx.set(tuple(item for item in stack if item is not self))

    def _maybe_full(self):
        with self._lock:
            if self._finished:
                return False
            if not self._exit_done:
                return False
            if self._defer and self._waiting > 0:
                return False
            self._finished = True
            return True


def _emit_full(span):
    _emit("TIMING SPAN id={0} name={1} phase=full ms={2:.0f}".format(
        span.id, redact_text(span.name), _ms(span.started)))


def _push(span):
    _ctx.set(_ctx.get() + (span,))


def begin(name):
    """Open a span, or return a no-op object when timing is off."""
    try:
        if not timing_enabled():
            return _NULL
        opened = Span(name)
        _push(opened)
        parent = opened.parent.id if opened.parent is not None else "-"
        _emit("TIMING SPAN id={0} name={1} phase=begin parent={2}".format(
            opened.id, redact_text(opened.name), redact_text(parent)))
        return opened
    except Exception as exc:
        _log_hook_failure("begin", exc)
        return _NULL


def mark(span, phase="first"):
    """Log ``phase`` on ``span``.

    None and disabled spans (timing off, or a no-op span) do nothing.
    Never raises.
    """
    try:
        if not timing_enabled():
            return
        if span is None or not getattr(span, "real", False):
            return
        span.mark(phase)
    except Exception as exc:
        _log_hook_failure("mark", exc)


def finish(span):
    """Close ``span``. If it is waiting on tasks, ``phase=full`` is logged when they finish."""
    try:
        if not getattr(span, "real", False):
            return
        span._drop_from_thread()
        with span._lock:
            span._exit_done = True
        if span._maybe_full():
            _emit_full(span)
    except Exception as exc:
        _log_hook_failure("finish", exc)


def pause_span(span):
    """Drop ``span`` from this thread without logging ``phase=full``."""
    try:
        if not getattr(span, "real", False):
            return
        span._drop_from_thread()
    except Exception as exc:
        _log_hook_failure("pause_span", exc)


def current():
    try:
        if not timing_enabled():
            return _NULL
        stack = _ctx.get()
        kept = tuple(item for item in stack if getattr(item, "real", False) and not item._finished)
        if len(kept) != len(stack):
            _ctx.set(kept)
        if kept:
            return kept[-1]
        return _NULL
    except Exception as exc:
        _log_hook_failure("current", exc)
        return _NULL


@contextmanager
def span(name):
    try:
        opened = begin(name)
    except Exception as exc:
        _log_hook_failure("span", exc)
        opened = _NULL
    try:
        yield opened
    finally:
        try:
            finish(opened)
        except Exception as exc:
            _log_hook_failure("span", exc)


def span_func(name):
    """Decorator: one span for the whole call. ``phase=first`` is logged as it returns."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            with span(name) as opened:
                try:
                    return func(*args, **kwargs)
                finally:
                    mark(opened, "first")
        wrapper.__name__ = getattr(func, "__name__", "span_func")
        wrapper.__doc__ = getattr(func, "__doc__", None)
        return wrapper
    return decorator


def note_task(task):
    """Remember the current span on a background task. No-op when timing is off."""
    try:
        if not timing_enabled():
            return
        opened = current()
        if getattr(opened, "real", False):
            task._timing_span = opened
    except Exception as exc:
        _log_hook_failure("note_task", exc)


def release_task(task):
    try:
        opened = getattr(task, "_timing_release", None)
        if not getattr(opened, "real", False):
            return
        task._timing_release = None
        with opened._lock:
            opened._waiting -= 1
        if opened._maybe_full():
            _emit_full(opened)
    except Exception as exc:
        _log_hook_failure("release_task", exc)


@contextmanager
def adopt(opened):
    """Run ``opened`` as the current span on this thread. No-op for a null span."""
    try:
        real = getattr(opened, "real", False)
    except Exception as exc:
        _log_hook_failure("adopt", exc)
        yield
        return
    if not real:
        yield
        return
    try:
        _push(opened)
    except Exception as exc:
        _log_hook_failure("adopt", exc)
        yield
        return
    try:
        yield
    finally:
        try:
            opened._drop_from_thread()
        except Exception as exc:
            _log_hook_failure("adopt", exc)


def play_phase(span, phase, mode=""):
    try:
        if not getattr(span, "real", False):
            return
        mode_bit = " mode={0}".format(redact_text(mode)) if mode else ""
        _emit("TIMING PLAY id={0} phase={1}{2} ms={3:.0f}".format(
            span.id, redact_text(phase), mode_bit, _ms(span.started)))
    except Exception as exc:
        _log_hook_failure("play_phase", exc)


def _thread_name():
    try:
        name = threading.current_thread().name or "?"
        return redact_text(name).replace(" ", "_").replace("\n", "_").replace("\r", "_")
    except Exception:
        return "?"


def _ui_blocked():
    try:
        return 1 if threading.current_thread() is threading.main_thread() else 0
    except Exception:
        return 0


def _response_bytes(response):
    if response is None:
        return -1
    raw = getattr(response, "_content", False)
    if raw not in (False, None):
        try:
            return len(raw)
        except Exception:
            pass
    try:
        header = response.headers.get("Content-Length")
        if header is not None and str(header).isdigit():
            return int(header)
    except Exception:
        pass
    return -1


def _response_ttfb_ms(response, total_ms):
    elapsed = getattr(response, "elapsed", None)
    if elapsed is None:
        return total_ms
    try:
        return elapsed.total_seconds() * 1000.0
    except Exception:
        return total_ms


def observe_http(method, url, func):
    """Time one HTTP call. ``func`` performs the request and returns the response."""
    try:
        enabled = timing_enabled()
    except Exception as exc:
        _log_hook_failure("observe_http", exc)
        enabled = False
    if not enabled:
        return func()
    started = time.perf_counter()
    response = None
    try:
        response = func()
        return response
    finally:
        try:
            _log_http(method, url, response, started)
        except Exception as exc:
            _log_hook_failure("observe_http", exc)


def _log_http(method, url, response, started):
    total_ms = _ms(started)
    active = current()
    span_id = active.id if getattr(active, "real", False) else "-"
    if response is None:
        status = 0
        cache = "error"
        nbytes = -1
        ttfb = total_ms
    else:
        status = getattr(response, "status_code", 0) or 0
        cache = "hit" if getattr(response, "from_cache", False) else "miss"
        nbytes = _response_bytes(response)
        ttfb = _response_ttfb_ms(response, total_ms)
    _emit(
        "TIMING REQ span={0} method={1} endpoint={2} status={3} bytes={4} "
        "ttfb_ms={5:.0f} total_ms={6:.0f} cache={7} thread={8} ui_blocked={9}".format(
            redact_text(span_id),
            redact_text(str(method).upper()),
            redact_text(redact_endpoint(url)),
            status,
            nbytes,
            ttfb,
            total_ms,
            redact_text(cache),
            _thread_name(),
            _ui_blocked(),
        )
    )


def _emit_line(label, elapsed):
    _emit("TIMING {0} {1:.0f}ms".format(redact_text(label), elapsed * 1000.0))


@contextmanager
def timed(label):
    """Log the wall time of the wrapped block when timing is enabled."""
    try:
        enabled = timing_enabled()
    except Exception as exc:
        _log_hook_failure("timed", exc)
        enabled = False
    if not enabled:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        try:
            _emit_line(label, time.perf_counter() - started)
        except Exception as exc:
            _log_hook_failure("timed", exc)


def timed_call(label):
    """Decorator form of :func:`timed`. ``label`` may be a string or a callable
    ``(args, kwargs) -> str`` so wrappers can name the specific request.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            if callable(label):
                try:
                    text = label(args, kwargs)
                except Exception:
                    text = func.__name__
            else:
                text = label
            with timed(text):
                return func(*args, **kwargs)
        wrapper.__name__ = getattr(func, "__name__", "timed_call")
        wrapper.__doc__ = getattr(func, "__doc__", None)
        return wrapper
    return decorator


def set_art_probe(probe):
    """Tests inject ``probe(url) -> True/False/None`` and poll with :func:`poll_art`."""
    global _art_probe
    _art_probe = probe


def watch_art(url):
    """Start the clock for one image URL. No-op when timing is off or the URL is local."""
    try:
        if not timing_enabled():
            return
        if not url:
            return
        text = str(url)
        if not (text.startswith("http://") or text.startswith("https://")):
            return
        active = current()
        item = {
            "url": text,
            "t0": time.perf_counter(),
            "span": active.id if getattr(active, "real", False) else "-",
        }
        if _art_ready(text) is True:
            _emit_art(item, "hit", 0.0)
            return
        with _art_lock:
            if any(pending["url"] == text for pending in _art_pending):
                return
            _art_pending.append(item)
        if _art_probe is None:
            _ensure_art_thread()
    except Exception as exc:
        _log_hook_failure("watch_art", exc)


def poll_art():
    """Check pending image URLs once. The background thread and tests both call this."""
    try:
        if not timing_enabled():
            with _art_lock:
                _art_pending[:] = []
            return
        now = time.perf_counter()
        with _art_lock:
            pending = list(_art_pending)
        done = []
        for item in pending:
            ready = _art_ready(item["url"])
            elapsed = (now - item["t0"]) * 1000.0
            if ready is True:
                _emit_art(item, "miss", elapsed)
                done.append(item["url"])
            elif now - item["t0"] >= _ART_TIMEOUT:
                _emit_art(item, "timeout", elapsed)
                done.append(item["url"])
        if done:
            with _art_lock:
                _art_pending[:] = [item for item in _art_pending if item["url"] not in done]
    except Exception as exc:
        _log_hook_failure("poll_art", exc)


def _emit_art(item, cache, elapsed):
    _emit("TIMING ART span={0} cache={1} ms={2:.0f} endpoint={3}".format(
        redact_text(item["span"]), redact_text(cache), elapsed, redact_text(redact_endpoint(item["url"]))))


def _ensure_art_thread():
    global _art_thread
    with _art_lock:
        if _art_thread is not None and _art_thread.is_alive():
            return
        _art_stop.clear()
        _art_thread = threading.Thread(target=_art_loop, name="pm4k-art-timing", daemon=True)
        _art_thread.start()


def _art_loop():
    while not _art_stop.is_set():
        try:
            poll_art()
        except Exception:
            pass
        if _art_stop.wait(_ART_POLL):
            return
        with _art_lock:
            if not _art_pending:
                return


def _translate_special(path):
    try:
        from kodi_six import xbmcvfs
        translated = xbmcvfs.translatePath(path)
        if translated:
            return translated
    except Exception:
        pass
    try:
        from kodi_six import xbmc
        return xbmc.translatePath(path)
    except Exception:
        return ""


def _art_ready(url):
    if _art_probe is not None:
        try:
            return _art_probe(url)
        except Exception:
            return None
    db_hit = _textures_db_has(url)
    if db_hit is True:
        return True
    file_hit = _thumb_file_exists(url)
    if file_hit is True:
        return True
    if db_hit is False and file_hit is False:
        return False
    return None


def _textures_db_has(url):
    """Read-only lookup in Kodi's texture database. Never writes."""
    try:
        path = _translate_special("special://database/Textures13.db")
        if not path or not os.path.exists(path):
            return None
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.05)
        try:
            row = conn.execute(
                "SELECT cachedurl FROM texture WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return True
        return False
    except Exception:
        return None


def _thumb_file_exists(url):
    try:
        from kodi_six import xbmc
        name = xbmc.getCacheThumbName(url)
    except Exception:
        return None
    if not name:
        return None
    candidates = [name]
    if not str(name).endswith(".png") and not str(name).endswith(".jpg"):
        candidates = [name + ".jpg", name + ".png"]
    for candidate in candidates:
        path = candidate
        if str(path).startswith("special://"):
            path = _translate_special(path)
        if path and os.path.exists(path):
            return True
    return False
