# coding=utf-8
"""Warm poster and episode thumbs before the next screen needs them.

Downloads run on a few background threads into a small file cache under the
add-on profile. Those threads only write files. A list item is pointed at the
file later, on the add-on event-loop thread, and only while that window
generation and the item are still alive. That thread is recorded by a window
callback. Until then, and on any other thread, nothing touches the list item.
The setting defaults on.

The cache key is a hash of the URL. The URL itself is never used as a
filename and is not logged.
"""
from __future__ import absolute_import

import hashlib
import os
import queue
import threading
import time

_WORKERS = 3
_MAX_FILES = 300
_MAX_BYTES = 80 * 1024 * 1024
_DOWNLOAD_TIMEOUT = (5, 20)

_STOP = object()
_WAIT = 0.5

_queue = queue.Queue()
_seen = set()
_seen_lock = threading.Lock()
_started = False
_stop = False
_threads = []
_start_lock = threading.Lock()
_focus_token = None
_focus_lock = threading.Lock()
_ready = {}
_pending = []
_pending_lock = threading.Lock()


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
        return util.getSetting("prefetch_art", True)
    except Exception:
        return False


def cache_root():
    override = os.environ.get("PM4K_ART_CACHE")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    try:
        from kodi_six import xbmcvfs
        from lib import util
        base = xbmcvfs.translatePath(util.ADDON.getAddonInfo("profile"))
    except Exception:
        base = ""
    if not base:
        return ""
    path = os.path.join(base, "artcache")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return ""
    return path


def path_for(url):
    root = cache_root()
    if not root or not url:
        return ""
    name = hashlib.sha1(str(url).encode("utf-8")).hexdigest() + ".img"
    return os.path.join(root, name)


def resolve(url):
    """Local file if this URL was prefetched, otherwise the URL itself."""
    if not url or not enabled():
        return url
    text = str(url)
    if not (text.startswith("http://") or text.startswith("https://")):
        return url
    path = path_for(text)
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    return url


def token_for(window):
    """Generation for one window. A new token means the previous items are gone."""
    if window is None:
        return None
    token = getattr(window, "_art_token", None)
    if token is None:
        token = object()
        try:
            window._art_token = token
        except Exception:
            return None
    return token


def prefetch(urls, items=None, generation=None):
    """Queue image URLs. List items are recorded for the GUI thread, never for a worker."""
    if not enabled():
        return
    if items and generation is not None:
        bind(items, generation)
    _ensure_workers()
    for url in urls or []:
        if not url:
            continue
        text = str(url)
        if not (text.startswith("http://") or text.startswith("https://")):
            continue
        with _seen_lock:
            if text in _seen:
                continue
            _seen.add(text)
        _queue.put(text)


def prefetch_focused(item):
    """Seasons and the first episode page of the focused show, off the UI thread."""
    if not enabled() or item is None:
        return
    global _focus_token
    token = object()
    with _focus_lock:
        _focus_token = token
    _ensure_workers()
    _queue.put(("__focus__", (token, item)))


def prefetch_episodes(season_or_show, width, height, offset=0, limit=12):
    if not enabled() or season_or_show is None:
        return
    _ensure_workers()
    _queue.put(("__episodes__", (season_or_show, width, height, offset, limit)))


def _ensure_workers():
    global _started
    if _stop:
        return
    with _start_lock:
        if _stop:
            return
        alive = [thread for thread in _threads if thread.is_alive()]
        _threads[:] = alive
        if len(_threads) >= _WORKERS:
            _started = True
            return
        _started = True
        while len(_threads) < _WORKERS:
            index = len(_threads)
            thread = threading.Thread(target=_worker, name="pm4k-art-{0}".format(index), daemon=True)
            thread.start()
            _threads.append(thread)


def bind(items, generation):
    """Remember list items to point at a cached file. GUI thread only."""
    if generation is None or not items or not _on_gui_thread():
        return
    immediate = []
    with _pending_lock:
        for item in items:
            if not _alive(item):
                continue
            url = _http_url(getattr(item, "thumbnailImage", None))
            if not url:
                continue
            path = _ready.get(url)
            if path and os.path.isfile(path):
                immediate.append((item, path))
            else:
                _pending.append((generation, url, item))
    _apply_ready(immediate)


def flush(generation):
    """Point still-living items at files the workers have finished. GUI thread only."""
    if generation is None or not _on_gui_thread():
        return
    immediate = []
    with _pending_lock:
        kept = []
        for gen, url, item in _pending:
            if gen is not generation or not _alive(item):
                continue
            path = _ready.get(url)
            if path and os.path.isfile(path):
                immediate.append((item, path))
            else:
                kept.append((gen, url, item))
        _pending[:] = kept
    _apply_ready(immediate)


def drop_generation(generation):
    """Forget items for a window that is closing. Does not touch the GUI."""
    if generation is None:
        return
    with _pending_lock:
        _pending[:] = [row for row in _pending if row[0] is not generation]


def _on_gui_thread():
    """True only on the recorded window event-loop thread.

    Kodi does not run the add-on script as Python's main thread. Unknown
    is not that thread, so a worker cannot apply a list item by arriving
    before the event loop has been seen.
    """
    from lib import hubrefresh
    return hubrefresh.on_script_thread()


def _alive(item):
    return bool(getattr(item, "_valid", True))


def _http_url(url):
    if not url:
        return ""
    text = str(url)
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return ""


def _apply_ready(pairs):
    for item, path in pairs:
        if not _alive(item):
            continue
        try:
            item.setThumbnailImage(path)
        except Exception:
            pass


def _store_ready(url, path):
    if not url or not path:
        return
    with _pending_lock:
        _ready[url] = path


def shutdown():
    """Unblock the art workers and wait for them. Safe to call more than once."""
    global _stop
    _stop = True
    with _start_lock:
        threads = list(_threads)
    for _thread in threads:
        _queue.put(_STOP)
    for thread in threads:
        thread.join(timeout=2)


def _worker():
    while not _stopping():
        try:
            item = _queue.get(timeout=_WAIT)
        except queue.Empty:
            continue
        try:
            if item is _STOP:
                return
            if isinstance(item, tuple) and item and item[0] == "__focus__":
                _run_focus(item[1][0], item[1][1])
            elif isinstance(item, tuple) and item and item[0] == "__episodes__":
                season, width, height, offset, limit = item[1]
                _run_episodes(season, width, height, offset, limit)
            else:
                _download(item)
        except Exception:
            pass
        finally:
            _queue.task_done()


def _still_focused(token):
    with _focus_lock:
        return _focus_token is token


def _run_focus(token, item):
    # Let a fast scroll settle before paying for a season list.
    deadline = time.time() + 0.35
    while time.time() < deadline:
        if _stopping() or not _still_focused(token):
            return
        time.sleep(0.05)
    if not _still_focused(token) or _stopping():
        return
    try:
        from lib import util
        poster = util.scaleResolution(174, 260)
        episode = util.scaleResolution(657, 393)
    except Exception:
        poster = (174, 260)
        episode = (657, 393)
    kind = getattr(item, "type", None)
    show = item if kind == "show" else None
    if kind == "episode":
        show = getattr(item, "_show", None)
        if show is None and hasattr(item, "show"):
            try:
                show = item.show()
            except Exception:
                show = None
        thumb = getattr(item, "thumb", None)
        if thumb is not None:
            try:
                prefetch([thumb.asTranscodedImageURL(*episode)])
            except Exception:
                pass
    if show is None or not _still_focused(token):
        return
    try:
        seasons = list(show.seasons() or [])
    except Exception:
        return
    if not _still_focused(token):
        return
    posters = []
    for season in seasons:
        thumb = getattr(season, "defaultThumb", None) or getattr(season, "thumb", None)
        if thumb is None:
            continue
        try:
            posters.append(thumb.asTranscodedImageURL(*poster))
        except Exception:
            continue
    prefetch(posters)
    target = None
    for season in seasons:
        try:
            watched = season.isWatched
        except Exception:
            watched = False
        if not watched:
            target = season
            break
    if target is None and seasons:
        target = seasons[0]
    if target is not None and _still_focused(token):
        _run_episodes(target, episode[0], episode[1], 0, 12)


def _run_episodes(season_or_show, width, height, offset, limit):
    try:
        episodes = season_or_show.episodes(offset=offset, limit=limit)
    except Exception:
        return
    urls = []
    for episode in episodes or []:
        thumb = getattr(episode, "thumb", None)
        if thumb is None:
            continue
        try:
            urls.append(thumb.asTranscodedImageURL(width, height))
        except Exception:
            continue
    prefetch(urls)


def _download(url):
    path = path_for(url)
    if not path:
        return
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        _store_ready(url, path)
        return
    try:
        import requests
        response = requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True)
        if getattr(response, "status_code", 0) != 200:
            return
        temporary = path + ".part"
        size = 0
        with open(temporary, "wb") as handle:
            for chunk in response.iter_content(64 * 1024):
                if _stopping():
                    return
                if not chunk:
                    continue
                handle.write(chunk)
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    break
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(path + ".part")
        except Exception:
            pass
        return
    _trim()
    _store_ready(url, path)


def _trim():
    root = cache_root()
    if not root:
        return
    try:
        names = [os.path.join(root, name) for name in os.listdir(root) if name.endswith(".img")]
    except Exception:
        return
    names.sort(key=lambda path: os.path.getmtime(path))
    total = 0
    sized = []
    for path in names:
        try:
            total += os.path.getsize(path)
        except Exception:
            continue
        sized.append(path)
    while sized and (len(sized) > _MAX_FILES or total > _MAX_BYTES):
        oldest = sized.pop(0)
        try:
            total -= os.path.getsize(oldest)
            os.remove(oldest)
        except Exception:
            pass


def _reset_for_tests():
    global _started, _stop, _focus_token
    shutdown()
    _stop = False
    _started = False
    with _start_lock:
        _threads[:] = []
    with _seen_lock:
        _seen.clear()
    with _focus_lock:
        _focus_token = None
    with _pending_lock:
        _ready.clear()
        _pending[:] = []
    while True:
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            break
        except ValueError:
            break
