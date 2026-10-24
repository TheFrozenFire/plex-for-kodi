# coding=utf-8
"""Open Home, or a TV or movie library, from outside Kodi.

The running add-on listens for ``Other.PM4K_NAV`` (``JSONRPC.NotifyAll``
with sender ``script.plexmod`` and message ``PM4K_NAV``).

The script thread is the window event loop, not Python's main thread. A
request that arrives elsewhere stays queued until the next window action,
which records that thread and then navigates. Home and library navigation
leave a video that is already playing alone.

``skip_marker`` is the playback screen's Skip button. It runs only while an
intro or credits marker is active, including after that button has hidden
and the marker is OSD-only. ``auto_skip`` turns intro and credits auto-skip
on for later episode playback in this Kodi session. It is not saved.
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
# Intro and credits auto-skip for later episodes. Memory only; not a setting.
_SESSION_AUTO_SKIP = False

_ON_TRUE = ("1", "true", "yes", "on")
_ON_FALSE = ("0", "false", "no", "off")

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
    "playback": None,
}


def _reset_for_tests():
    global _BUSY, _APPLYING, _SESSION_AUTO_SKIP
    with _LOCK:
        _BUSY = False
        _APPLYING = False
        _SESSION_AUTO_SKIP = False
        del _QUEUED[:]
    for key in _HOOKS:
        _HOOKS[key] = None


def session_auto_skip_enabled():
    """True when this Kodi session should auto-skip intro and credits.

    Episode playback reads this when it starts. Nothing writes it to the
    per-show playback settings.
    """
    with _LOCK:
        return _SESSION_AUTO_SKIP


def parse_notification(data):
    """A request dict, or None when ``data`` is not a known navigation."""
    payload = _coerce(data)
    if not isinstance(payload, dict):
        return None
    dest = str(payload.get("to") or "").strip().lower()
    if dest == "home":
        return {"to": "home"}
    if dest == "skip_marker":
        return {"to": "skip_marker"}
    if dest == "auto_skip":
        if "on" not in payload:
            return {"to": "auto_skip", "on": True}
        enabled = _parse_on(payload.get("on"))
        if enabled is None:
            return None
        return {"to": "auto_skip", "on": enabled}
    if dest != "section":
        return None
    kind = str(payload.get("type") or "").strip().lower()
    title = _clean_title(payload.get("title", None))
    if title:
        request = {"to": "section", "title": title}
        if kind:
            request["type"] = kind
        return request
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
    dest = request.get("to")
    if dest in ("home", "skip_marker"):
        return True
    if dest == "auto_skip":
        return isinstance(request.get("on", True), bool)
    if dest != "section":
        return False
    if request.get("title"):
        return True
    return request.get("type") in ("show", "movie")


def _perform(request):
    """Decide the GUI action. Terminal failures are logged here.

    ``defer`` means a PlexMod window is still on top of Home. Closing it
    returns to Home on the next pass; the request stays queued.
    """
    if request["to"] == "skip_marker":
        return ("skip_marker",)
    if request["to"] == "auto_skip":
        return ("auto_skip", bool(request.get("on", True)))
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
    return ("open", home, _section_spec(request))


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
    if kind == "skip_marker":
        return _finish_skip_marker()
    if kind == "auto_skip":
        return _finish_auto_skip(action[1])
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
    spec = action[2]
    detail = _section_detail(spec)
    with _LOCK:
        _APPLYING = True
    try:
        try:
            opened = _open_section(action[1], spec)
        except Exception:
            LOG("Remote nav: failed reason=error")
            return _section_result(False, "error", spec)
    finally:
        with _LOCK:
            _APPLYING = False
    if opened:
        LOG("Remote nav: ok to=section {0}", detail)
        return _section_result(True, "opened", spec)
    LOG("Remote nav: failed reason=no_section {0}", detail)
    return _section_result(False, "no_section", spec)


def _finish_skip_marker():
    """Press the playback screen's Skip button, whatever has focus."""
    global _APPLYING
    dialog = _playback_dialog()
    try:
        active = bool(dialog and dialog.markerSkipActive())
    except Exception:
        LOG("Remote nav: failed reason=error")
        return None
    if not active:
        LOG("Remote nav: ignored reason=no_marker")
        return None
    with _LOCK:
        _APPLYING = True
    try:
        try:
            outcome = dialog.skipMarkerButton()
        except Exception:
            LOG("Remote nav: failed reason=error")
            return None
    finally:
        with _LOCK:
            _APPLYING = False
    if outcome is False:
        LOG("Remote nav: ignored reason=busy")
        return None
    if not isinstance(outcome, dict):
        LOG("Remote nav: ignored reason=no_marker")
        return None
    kind = str(outcome.get("type") or "")
    final = bool(outcome.get("final"))
    detail = "type={0}".format(kind) if kind else ""
    if final:
        detail = (detail + " final=1").strip()
    if detail:
        LOG("Remote nav: ok to=skip_marker {0}", detail)
    else:
        LOG("Remote nav: ok to=skip_marker")
    result = {"ok": True, "to": "skip_marker"}
    if kind:
        result["type"] = kind
    if final:
        result["final"] = True
    return result


def _finish_auto_skip(enabled):
    global _SESSION_AUTO_SKIP
    enabled = bool(enabled)
    with _LOCK:
        _SESSION_AUTO_SKIP = enabled
    LOG("Remote nav: ok to=auto_skip on={0}", 1 if enabled else 0)
    return {"ok": True, "to": "auto_skip", "on": enabled}


def _playback_dialog():
    """The open playback screen, or None when nothing is playing there."""
    hook = _HOOKS.get("playback")
    if hook is not None:
        return hook() if callable(hook) else hook
    from . import player
    handler = getattr(player.PLAYER, "handler", None)
    if handler is None:
        return None
    return getattr(handler, "dialog", None)


def _parse_on(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _ON_TRUE:
            return True
        if text in _ON_FALSE:
            return False
    return None


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


def _open_section(home, spec):
    """Open the library ``spec`` names, the way a click on it does.

    With no title, ``spec`` is the first library of that type. A title
    must match on its own; a miss does not open another library.
    """
    hook = _HOOKS.get("open_section")
    if hook is not None:
        return bool(hook(home, spec))
    section_list = getattr(home, "sectionList", None)
    item = _find_section(home, spec)
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


def _find_section(home, spec):
    """The library to open, or None.

    A pinned item-type view reports its library's type, but it is not the
    library. Home, playlists, and the watchlist are not libraries either.
    """
    section_list = getattr(home, "sectionList", None)
    if section_list is None:
        return None
    kind = spec.get("type") or ""
    title = spec.get("title") or ""
    for item in section_list:
        section = getattr(item, "dataSource", None)
        if not section or getattr(section, "itemType", None):
            continue
        if title:
            if not _titles_match(section, title):
                continue
            if kind and _section_type(section) != kind:
                continue
            if not _is_library(section):
                continue
            return item
        if _section_type(section) != kind:
            continue
        return item
    return None


def _section_spec(request):
    spec = {}
    if request.get("type"):
        spec["type"] = request["type"]
    if request.get("title"):
        spec["title"] = request["title"]
    return spec


def _section_detail(spec):
    parts = []
    if spec.get("type"):
        parts.append("type={0}".format(spec["type"]))
    if spec.get("title"):
        parts.append("title={0}".format(spec["title"]))
    return " ".join(parts)


def _section_result(ok, reason, spec):
    result = {"ok": bool(ok), "to": "section"}
    if spec.get("type"):
        result["type"] = spec["type"]
    if spec.get("title"):
        result["title"] = spec["title"]
    if not ok:
        result["reason"] = reason
    return result


def _clean_title(value):
    if value is None:
        return ""
    return str(value).strip()


def _section_type(section):
    return str(getattr(section, "type", "") or "").strip().lower()


def _titles_match(section, title):
    wanted = title.strip().lower()
    found = str(getattr(section, "title", "") or "").strip().lower()
    return bool(wanted) and found == wanted


def _is_library(section):
    return _section_type(section) in ("show", "movie", "artist", "photo")


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
