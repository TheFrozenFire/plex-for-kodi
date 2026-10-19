# coding=utf-8
"""Open Home, or the first TV or movie library, from outside Kodi.

The running add-on listens for ``Other.PM4K_NAV`` (``JSONRPC.NotifyAll``
with sender ``script.plexmod`` and message ``PM4K_NAV``).

The script thread is the window event loop, not Python's main thread. A
request that arrives elsewhere stays queued until the next window action,
which records that thread and then navigates. A video that is already
playing is left alone.
"""
from __future__ import absolute_import

import json
import threading

from .logging import log as LOG

_LOCK = threading.Lock()
_QUEUED = []
_BUSY = False
_APPLYING = False
_MAX_DEFER = 3

# Tests replace entries. None means the real Kodi / window implementation.
_HOOKS = {
    "playing": None,
    "on_gui": None,
    "poke": None,
    "home": None,
    "overlays": None,
    "close_overlays": None,
    "focus_home": None,
    "open_section": None,
}


def _reset_for_tests():
    global _BUSY, _APPLYING
    with _LOCK:
        _BUSY = False
        _APPLYING = False
        del _QUEUED[:]
    for key in _HOOKS:
        _HOOKS[key] = None


def parse_notification(data):
    """A request dict, or None when ``data`` is not a known navigation."""
    payload = _coerce(data)
    if not isinstance(payload, dict):
        return None
    dest = str(payload.get("to") or "").strip().lower()
    if dest == "home":
        return {"to": "home"}
    if dest != "section":
        return None
    kind = str(payload.get("type") or "").strip().lower()
    if kind not in ("show", "movie"):
        return None
    return {"to": "section", "type": kind}


def accept_notification(data):
    """Handle ``Other.PM4K_NAV`` on the running instance."""
    try:
        request = parse_notification(data)
    except Exception:
        LOG("Remote nav: ignored reason=bad_request")
        return None
    if not request:
        LOG("Remote nav: ignored reason=bad_request")
        return None
    try:
        return submit(request)
    except Exception:
        LOG("Remote nav: failed reason=error")
        return None


def submit(request):
    """Navigate for ``request`` on the script thread, or queue it until then."""
    global _BUSY
    if not _valid(request):
        LOG("Remote nav: ignored reason=bad_request")
        return None
    if not _on_gui_thread():
        with _LOCK:
            _QUEUED.append(request)
        _poke_gui()
        return None
    with _LOCK:
        if _BUSY:
            _QUEUED.append(request)
            return None
        _BUSY = True
    try:
        try:
            action = _perform(request)
        except Exception:
            LOG("Remote nav: failed reason=error")
            action = None
    finally:
        with _LOCK:
            _BUSY = False
    return _finish(action, request)


def drain():
    """Run queued requests. Only the script thread may call this."""
    global _BUSY
    if not _on_gui_thread():
        return
    while True:
        with _LOCK:
            if _BUSY or not _QUEUED:
                return
            request = _QUEUED.pop(0)
            _BUSY = True
        try:
            try:
                action = _perform(request)
            except Exception:
                LOG("Remote nav: failed reason=error")
                action = None
        finally:
            with _LOCK:
                _BUSY = False
        _finish(action, request)
        if action and action[0] == "defer":
            return


def nudge():
    """Wake the window loop if a request is waiting to run there."""
    with _LOCK:
        pending = bool(_QUEUED) and not _APPLYING
    if pending:
        _poke_gui()


def _valid(request):
    if not isinstance(request, dict):
        return False
    if request.get("to") == "home":
        return True
    return request.get("to") == "section" and request.get("type") in ("show", "movie")


def _perform(request):
    """Decide the GUI action. Terminal failures are logged here.

    ``defer`` means a PlexMod window is still on top of Home. Closing it
    returns to Home on the next pass; the request stays queued.
    """
    if _is_playing():
        LOG("Remote nav: ignored reason=playing")
        return None
    home = _home()
    if not _ui_ready(home):
        LOG("Remote nav: failed reason=not_ready")
        return None
    if _overlays(home):
        tries = int(request.get("_defer") or 0) + 1
        request["_defer"] = tries
        if tries > _MAX_DEFER:
            LOG("Remote nav: failed reason=not_ready")
            return None
        try:
            home.go_root = True
        except Exception:
            pass
        _close_overlays()
        return ("defer",)
    if request["to"] == "home":
        return ("focus", home)
    return ("open", home, request["type"])


def _finish(action, request):
    global _APPLYING
    if not action:
        return None
    kind = action[0]
    if kind == "defer":
        # Keep this request ahead of anything queued behind it.
        with _LOCK:
            _QUEUED.insert(0, request)
        _poke_gui()
        return None
    if kind == "focus":
        with _LOCK:
            _APPLYING = True
        try:
            try:
                _focus_home(action[1])
            except Exception:
                LOG("Remote nav: failed reason=error")
                return {"ok": False, "reason": "error", "to": "home"}
        finally:
            with _LOCK:
                _APPLYING = False
        LOG("Remote nav: ok to=home")
        return {"ok": True, "to": "home"}
    if kind != "open":
        LOG("Remote nav: ignored reason=bad_request")
        return None
    library_type = action[2]
    with _LOCK:
        _APPLYING = True
    try:
        try:
            opened = _open_section(action[1], library_type)
        except Exception:
            LOG("Remote nav: failed reason=error")
            return {"ok": False, "reason": "error", "to": "section", "type": library_type}
    finally:
        with _LOCK:
            _APPLYING = False
    if opened:
        LOG("Remote nav: ok to=section type={0}", library_type)
        return {"ok": True, "to": "section", "type": library_type}
    LOG("Remote nav: failed reason=no_section type={0}", library_type)
    return {"ok": False, "reason": "no_section", "to": "section", "type": library_type}


def _focus_home(home):
    """Select the Home section and focus the section list.

    ``show`` applies the same ``go_root`` path as backing out to Home.
    When that path does not run, ``goHome`` selects the section directly.
    """
    hook = _HOOKS.get("focus_home")
    if hook is not None:
        return hook(home)
    try:
        home.go_root = True
    except Exception:
        pass
    show = getattr(home, "show", None)
    if callable(show):
        show()
    if not getattr(home, "go_root", False):
        return
    go = getattr(home, "goHome", None)
    if callable(go):
        go()
    else:
        section_list = getattr(home, "sectionList", None)
        focus = getattr(home, "SECTION_LIST_ID", None)
        if section_list is not None and focus is not None:
            home.setFocusId(focus)
            section_list.setSelectedItemByPos(0)
    try:
        home.go_root = False
    except Exception:
        pass


def _open_section(home, kind):
    """Open the first library of ``kind``, the way a click on it does."""
    hook = _HOOKS.get("open_section")
    if hook is not None:
        return bool(hook(home, kind))
    section_list = getattr(home, "sectionList", None)
    item = _first_section(home, kind)
    if item is None or section_list is None:
        return False
    try:
        section_list.setSelectedItem(item)
    except Exception:
        return False
    try:
        current = section_list.getSelectedItem()
    except Exception:
        current = None
    if current is not item and getattr(current, "dataSource", None) is not getattr(item, "dataSource", None):
        return False
    focus = getattr(home, "SECTION_LIST_ID", None)
    if focus is not None:
        try:
            home.setFocusId(focus)
        except Exception:
            pass
    click = getattr(home, "sectionClicked", None)
    if not callable(click):
        return False
    # A pending go-home must not fire after the library closes, or backing
    # out of it would jump to the Home section instead of the library's hubs.
    try:
        home.go_root = False
    except Exception:
        pass
    _poke_if_queued()
    click()
    return True


def _first_section(home, kind):
    """First home-screen library whose type is ``kind``.

    A pinned item-type view reports its library's type, but it is not the
    library. Home, playlists, and the watchlist do not match ``show`` or
    ``movie``.
    """
    section_list = getattr(home, "sectionList", None)
    if section_list is None:
        return None
    for item in section_list:
        section = getattr(item, "dataSource", None)
        if not section:
            continue
        if getattr(section, "itemType", None):
            continue
        if str(getattr(section, "type", "") or "").strip().lower() != kind:
            continue
        return item
    return None


def _home():
    hook = _HOOKS.get("home")
    if hook is not None:
        return hook() if callable(hook) else hook
    from .windows import windowutils
    return windowutils.HOME


def _ui_ready(home):
    if home is None:
        return False
    return getattr(home, "sectionList", None) is not None


def _overlays(home):
    """True when a PlexMod window or dialog is open above Home."""
    hook = _HOOKS.get("overlays")
    if hook is not None:
        try:
            return bool(hook(home)) if callable(hook) else bool(hook)
        except Exception:
            return False
    try:
        import xbmcgui
        current = int(xbmcgui.getCurrentWindowId() or 0)
        dialog = int(xbmcgui.getCurrentWindowDialogId() or 0)
    except Exception:
        return False
    if dialog > 9999:
        return True
    home_id = getattr(home, "_winID", None)
    try:
        home_id = int(home_id)
    except (TypeError, ValueError):
        return False
    return bool(home_id) and current > 0 and current != home_id


def _close_overlays():
    hook = _HOOKS.get("close_overlays")
    if hook is not None:
        return hook()
    try:
        from plexnet import plexapp
        plexapp.util.APP.trigger("close.dialogs")
        plexapp.util.APP.trigger("close.windows")
    except Exception:
        LOG("Remote nav: close failed")


def _is_playing():
    hook = _HOOKS.get("playing")
    if hook is not None:
        try:
            return bool(hook()) if callable(hook) else bool(hook)
        except Exception:
            return False
    try:
        import xbmc
        return bool(xbmc.Player().isPlayingVideo())
    except Exception:
        return False


def _poke_gui():
    hook = _HOOKS.get("poke")
    if hook is not None:
        return hook()
    import xbmc
    try:
        xbmc.executebuiltin("Action(noop)")
    except Exception:
        pass


def _poke_if_queued():
    with _LOCK:
        pending = bool(_QUEUED)
    if pending:
        _poke_gui()


def _on_gui_thread():
    """True only on the recorded window event-loop thread.

    Unknown is not that thread. A test may replace this with ``_HOOKS``.
    """
    hook = _HOOKS.get("on_gui")
    if hook is not None:
        return bool(hook()) if callable(hook) else bool(hook)
    from lib import hubrefresh
    return hubrefresh.on_script_thread()


def _coerce(data):
    if isinstance(data, dict):
        return data
    if data is None:
        return None
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except Exception:
            return None
    text = str(data).strip()
    if not text:
        return None
    try:
        loaded = json.loads(text)
    except Exception:
        return None
    if isinstance(loaded, str):
        try:
            loaded = json.loads(loaded)
        except Exception:
            return None
    return loaded
