# coding=utf-8
"""Warm poster and episode thumbs before the next screen needs them.

Downloads run on a few background threads into a small file cache under the
add-on profile. List items use the file when it is already there, so Kodi does
not fetch that URL again. Off unless ``prefetch_art`` is turned off; the
setting defaults on.

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

_queue = queue.Queue()
_seen = set()
_seen_lock = threading.Lock()
_started = False
_start_lock = threading.Lock()
_focus_token = None
_focus_lock = threading.Lock()


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


def prefetch(urls, items=None):
    """Queue image URLs. ``items`` may be list items to point at the file when it lands."""
    if not enabled():
        return
    items = items or []
    _ensure_workers()
    for index, url in enumerate(urls or []):
        if not url:
            continue
        text = str(url)
        if not (text.startswith("http://") or text.startswith("https://")):
            continue
        item = items[index] if index < len(items) else None
        with _seen_lock:
            if text in _seen and item is None:
                continue
            _seen.add(text)
        _queue.put((text, item))


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
    with _start_lock:
        if _started:
            return
        _started = True
        for index in range(_WORKERS):
            thread = threading.Thread(target=_worker, name="pm4k-art-{0}".format(index), daemon=True)
            thread.start()


def _worker():
    while True:
        url, extra = _queue.get()
        try:
            if url == "__focus__":
                _run_focus(extra[0], extra[1])
            elif url == "__episodes__":
                season, width, height, offset, limit = extra
                _run_episodes(season, width, height, offset, limit)
            else:
                _download(url, extra)
        except Exception:
            pass
        finally:
            _queue.task_done()


def _still_focused(token):
    with _focus_lock:
        return _focus_token is token


def _run_focus(token, item):
    # Let a fast scroll settle before paying for a season list.
    time.sleep(0.35)
    if not _still_focused(token):
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


def _download(url, item):
    path = path_for(url)
    if not path:
        return
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        _apply(item, path)
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
    _apply(item, path)


def _apply(item, path):
    if item is None:
        return
    try:
        item.setThumbnailImage(path)
    except Exception:
        pass


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
    global _started, _focus_token
    with _seen_lock:
        _seen.clear()
    with _focus_lock:
        _focus_token = None
    # Drain without starting workers. Tests do not enqueue downloads.
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
