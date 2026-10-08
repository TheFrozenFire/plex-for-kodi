# coding=utf-8
"""Start playback of one Plex item from outside Kodi.

The running add-on listens for ``Other.PM4K_PLAY`` (``JSONRPC.NotifyAll``).
A second ``RunScript`` / ``Addons.ExecuteAddon`` invocation only forwards that
announcement and exits, so it does not open another UI.

Workers are not used. The script thread is the window event loop, not
Python's main thread. A request that arrives elsewhere stays queued until
the next window action, which records that thread and then plays.
"""
from __future__ import absolute_import

import json
import re
import threading
import uuid

from .logging import log as LOG

_MESSAGE = "PM4K_PLAY"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STOP_TRIES = 25

_LOCK = threading.Lock()
_QUEUED = []
_BUSY = False

# Tests replace entries. None means the real Kodi / plexnet implementation.
_HOOKS = {
    "manager": None,
    "fetch": None,
    "open": None,
    "playing": None,
    "stop": None,
    "sleep": None,
    "ready": None,
    "wake": None,
    "dismiss": None,
    "poke": None,
    "running": None,
    "notify": None,
    "on_gui": None,
}


def _reset_for_tests():
    global _BUSY
    with _LOCK:
        _BUSY = False
        del _QUEUED[:]
    for key in _HOOKS:
        _HOOKS[key] = None


def argv_is_play(argv):
    if not argv or len(argv) < 2:
        return False
    first = argv[1]
    if first == "play":
        return True
    if first.startswith("{") or first.startswith("ratingKey=") or first.startswith("key="):
        return True
    return False


def parse_argv(argv):
    """Turn ``RunScript`` / ``ExecuteAddon`` args into a request, or None."""
    if not argv or len(argv) < 2:
        return None
    first = argv[1]
    if first == "play":
        return normalize({
            "ratingKey": argv[2] if len(argv) > 2 else "",
            "server": argv[3] if len(argv) > 3 else "",
            "resume": argv[4] if len(argv) > 4 else None,
            "force": argv[5] if len(argv) > 5 else False,
        })
    if first.startswith("{"):
        try:
            payload = json.loads(first)
        except Exception:
            return None
        if isinstance(payload, dict):
            return normalize(payload)
        return None
    data = {}
    for arg in argv[1:]:
        if "=" not in arg:
            continue
        key, value = arg.split("=", 1)
        data[key] = value
    if "ratingKey" in data or "key" in data:
        return normalize(data)
    return None


def normalize(data):
    """A request dict, or None when the ratingKey is missing or not numeric."""
    if not isinstance(data, dict):
        return None
    rating = data.get("ratingKey", None)
    if rating is None or rating == "":
        rating = data.get("ratingkey", None)
    if rating is None or rating == "":
        rating = data.get("key", "")
    rating = str(rating).strip()
    if rating.startswith("/library/metadata/"):
        rating = rating.rsplit("/", 1)[-1]
    if not rating.isdigit():
        return None
    server = data.get("server", None)
    if server is None or server == "":
        server = data.get("serverId", None)
    if server is None or server == "":
        server = data.get("serverName", "")
    server = str(server or "").strip()
    ident = str(data.get("id") or "").strip()
    if not _ID_RE.match(ident):
        ident = uuid.uuid4().hex[:12]
    return {
        "ratingKey": rating,
        "server": server,
        "resume": _parse_resume(data.get("resume", None)),
        "force": _parse_bool(data.get("force", False)),
        "id": ident,
    }


def handoff_argv(argv):
    """Forward a play argv to the running instance, or report that it is down.

    This process must not open a window. The running add-on performs the play.
    """
    request = parse_argv(argv)
    if not request:
        result = _result(False, "bad_request", "", "", "", "")
        _publish(result)
        return result
    if not _instance_running():
        result = _result(False, "not_running", request["id"], request["ratingKey"], "", "")
        _publish(result)
        LOG("Remote play: failed reason=not_running")
        return result
    _store_request(request)
    result = _pending(request)
    _publish(result, notify=False)
    _notify_instance(request)
    LOG("Remote play: handed off")
    return result


def accept_notification(data):
    """Handle ``Other.PM4K_PLAY`` on the running instance."""
    request = _request_from_notification(data)
    if not request or request.get("_missing"):
        ident = ""
        if isinstance(request, dict):
            ident = request.get("id") or ""
        result = _result(False, "bad_request", ident, "", "", "")
        _publish(result)
        LOG("Remote play: failed reason=bad_request")
        return result
    return submit(request)


def submit(request):
    """Play ``request`` on the script thread, or queue it until then."""
    if not isinstance(request, dict) or not request.get("ratingKey"):
        result = _result(False, "bad_request", request.get("id", "") if isinstance(request, dict) else "", "", "", "")
        _publish(result)
        return result
    if not _on_gui_thread():
        with _LOCK:
            _QUEUED.append(request)
        _poke_gui()
        return None
    return _run(request)


def drain():
    """Play queued requests. Only the script thread may call this."""
    if not _on_gui_thread():
        return
    while True:
        with _LOCK:
            if _BUSY or not _QUEUED:
                return
            request = _QUEUED.pop(0)
        _run(request)


def playback_kwargs(request):
    """Flags for the normal play button.

    Default is to resume when the item has progress and otherwise start at
    the beginning, with no prompt. ``start`` forces the beginning. ``ask``
    is the on-screen play button, including its resume prompt.
    """
    mode = request.get("resume")
    kwargs = {"auto_play": True}
    if mode == "start":
        kwargs["start_over"] = True
    elif mode == "resume":
        kwargs["force_resume"] = True
    return kwargs


def _run(request):
    global _BUSY
    with _LOCK:
        if _BUSY:
            result = _result(False, "busy", request.get("id", ""), request.get("ratingKey", ""), "", "")
            _publish(result)
            LOG("Remote play: failed reason=busy")
            return result
        _BUSY = True
    _publish(_pending(request), notify=False)
    try:
        _take_request(request.get("id") or "")
    except Exception:
        pass
    try:
        return _perform(request)
    finally:
        with _LOCK:
            _BUSY = False


def _perform(request):
    ident = request.get("id", "")
    requested = request.get("ratingKey", "")
    if not _ui_ready():
        return _fail(request, "not_ready")
    if _is_playing():
        if not request.get("force"):
            return _fail(request, "playing")
        _stop_playback()
        if not _wait_until_stopped():
            return _fail(request, "playing")
    try:
        server, reason = _resolve_server(request.get("server") or "")
    except Exception:
        LOG("Remote play: failed reason=error kind=server")
        return _fail(request, "error")
    if server is None:
        return _fail(request, reason or "no_server")
    try:
        item = _fetch(server, requested)
    except Exception:
        LOG("Remote play: failed reason=error kind=lookup")
        return _fail(request, "error")
    if item is None:
        return _fail(request, "not_found")
    try:
        playable, reason = _to_playable(item)
    except Exception:
        LOG("Remote play: failed reason=error kind=resolve")
        return _fail(request, "error")
    if playable is None:
        return _fail(request, reason or "not_found")
    if not _available(playable):
        return _fail(request, "unavailable")
    _wake()
    _dismiss_dialogs()
    try:
        opened = _open(playable, request)
    except Exception as exc:
        if exc.__class__.__name__ == "NoDataException":
            return _fail(request, "not_found")
        LOG("Remote play: failed reason=error kind={0}", exc.__class__.__name__)
        return _fail(request, "error")
    if opened == "NODATA":
        return _fail(request, "not_found")
    kind = _kind(playable)
    played = _rating_of(playable)
    result = _result(True, "started", ident, requested, played, kind)
    _publish(result)
    LOG("Remote play: ok type={0}", kind or "item")
    return result


def _to_playable(item):
    kind = _kind(item)
    if kind in ("movie", "episode"):
        return item, None
    if kind == "show":
        reload = getattr(item, "reload", None)
        if callable(reload):
            reload(includeOnDeck=1)
        deck = list(getattr(item, "onDeck", None) or [])
        if deck:
            return deck[0], None
        episodes = _episodes_of(item, watched=False)
        if episodes is None:
            return None, "error"
        if episodes:
            return episodes[0], None
        return None, "nothing_to_play"
    if kind == "season":
        episodes = _episodes_of(item)
        if episodes is None:
            return None, "error"
        progress = [episode for episode in episodes if _offset(episode) and not _watched(episode)]
        if progress:
            return progress[0], None
        fresh = [episode for episode in episodes if not _watched(episode)]
        if fresh:
            return fresh[0], None
        return None, "nothing_to_play"
    if not kind:
        return None, "not_found"
    return None, "unsupported"


def _episodes_of(item, watched=None):
    fn = getattr(item, "episodes", None)
    if not callable(fn):
        return []
    try:
        if watched is None:
            found = fn()
        else:
            found = fn(watched=watched)
    except Exception:
        return None
    return list(found or [])


def _resolve_server(hint):
    manager = _manager()
    if manager is None:
        return None, "no_server"
    if not hint:
        selected = getattr(manager, "selectedServer", None)
        if selected is None:
            return None, "no_server"
        return selected, None
    hint_l = hint.lower()
    servers = _iter_servers(manager)
    by_uuid = [server for server in servers if str(getattr(server, "uuid", "") or "").lower() == hint_l]
    if len(by_uuid) == 1:
        return by_uuid[0], None
    if len(by_uuid) > 1:
        return None, "ambiguous_server"
    by_name = [server for server in servers if str(getattr(server, "name", "") or "").lower() == hint_l]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, "ambiguous_server"
    return None, "no_server"


def _iter_servers(manager):
    get_servers = getattr(manager, "getServers", None)
    if callable(get_servers):
        try:
            return [server for server in (get_servers() or []) if getattr(server, "uuid", None) != "myplex"]
        except Exception:
            pass
    found = getattr(manager, "serversByUuid", None)
    if isinstance(found, dict):
        return [server for server in found.values() if getattr(server, "uuid", None) not in ("myplex", "plexdiscover")]
    return list(getattr(manager, "servers", None) or [])


def _manager():
    hook = _HOOKS.get("manager")
    if hook is not None:
        return hook() if callable(hook) else hook
    from plexnet import plexapp
    return plexapp.SERVERMANAGER


def _fetch(server, rating_key):
    hook = _HOOKS.get("fetch")
    if hook is not None:
        return hook(server, rating_key)
    return server.getObject("/library/metadata/{0}".format(rating_key))


def _open(item, request):
    hook = _HOOKS.get("open")
    if hook is not None:
        return hook(item, request)
    from .windows import opener
    return opener.open(item, **playback_kwargs(request))


def _ui_ready():
    hook = _HOOKS.get("ready")
    if hook is not None:
        return bool(hook() if callable(hook) else hook)
    from .windows import windowutils
    return windowutils.HOME is not None


def _is_playing():
    hook = _HOOKS.get("playing")
    if hook is not None:
        return bool(hook()) if callable(hook) else bool(hook)
    import xbmc
    try:
        return bool(xbmc.Player().isPlayingVideo())
    except Exception:
        return False


def _stop_playback():
    hook = _HOOKS.get("stop")
    if hook is not None:
        return hook()
    import xbmc
    xbmc.Player().stop()


def _sleep_once():
    hook = _HOOKS.get("sleep")
    if hook is not None:
        return bool(hook())
    import xbmc
    xbmc.sleep(100)
    try:
        from . import util
        if util.MONITOR.abortRequested():
            return True
    except Exception:
        pass
    return False


def _wait_until_stopped():
    tries = 0
    while _is_playing() and tries < _STOP_TRIES:
        tries += 1
        if _sleep_once() or not _is_playing():
            break
    return not _is_playing()


def _wake():
    hook = _HOOKS.get("wake")
    if hook is not None:
        return hook()
    import xbmc
    try:
        active = xbmc.getCondVisibility("System.ScreenSaverActive")
    except Exception:
        active = False
    if not active:
        return
    try:
        xbmc.executebuiltin("InhibitScreensaver(true)")
        xbmc.executeJSONRPC(
            '{"jsonrpc":"2.0","method":"Input.ExecuteAction","params":{"action":"noop"},"id":1}'
        )
    except Exception:
        LOG("Remote play: wake failed")
    try:
        xbmc.executebuiltin("InhibitScreensaver(false)")
    except Exception:
        pass


def _dismiss_dialogs():
    hook = _HOOKS.get("dismiss")
    if hook is not None:
        return hook()
    try:
        from plexnet import plexapp
        plexapp.util.APP.trigger("close.dialogs")
    except Exception:
        LOG("Remote play: dialog dismiss failed")


def _poke_gui():
    hook = _HOOKS.get("poke")
    if hook is not None:
        return hook()
    import xbmc
    try:
        xbmc.executebuiltin("Action(noop)")
    except Exception:
        pass


def _instance_running():
    hook = _HOOKS.get("running")
    if hook is not None:
        return bool(hook()) if callable(hook) else bool(hook)
    from .properties import getGlobalProperty
    return bool(getGlobalProperty("running"))


def _on_gui_thread():
    """True only on the recorded window event-loop thread.

    Unknown is not that thread. A test may replace this with ``_HOOKS``.
    """
    hook = _HOOKS.get("on_gui")
    if hook is not None:
        return bool(hook()) if callable(hook) else bool(hook)
    from lib import hubrefresh
    return hubrefresh.on_script_thread()


def _notify_instance(request):
    import xbmc
    body = json.dumps(request, sort_keys=True, separators=(",", ":"))
    escaped = body.replace("\\", "\\\\").replace('"', '\\"')
    xbmc.executebuiltin('NotifyAll(script.plexmod,{0},"{1}")'.format(_MESSAGE, escaped))


def _store_request(request):
    from .properties import setGlobalProperty
    setGlobalProperty(
        "remoteplay.request.{0}".format(request["id"]),
        json.dumps(request, sort_keys=True, separators=(",", ":")),
    )


def _take_request(ident):
    if not _ID_RE.match(ident or ""):
        return ""
    from .properties import getGlobalProperty
    return getGlobalProperty("remoteplay.request.{0}".format(ident), consume=True) or ""


def _request_from_notification(data):
    payload = _coerce(data)
    if isinstance(payload, str):
        stored = _take_request(payload)
        if not stored:
            return {"_missing": True, "id": payload}
        try:
            payload = json.loads(stored)
        except Exception:
            return {"_missing": True, "id": payload}
    if not isinstance(payload, dict):
        return None
    if payload.get("ratingKey") or payload.get("ratingkey") or payload.get("key"):
        return normalize(payload)
    ident = str(payload.get("id") or "").strip()
    if not ident:
        return None
    stored = _take_request(ident)
    if not stored:
        return {"_missing": True, "id": ident}
    try:
        loaded = json.loads(stored)
    except Exception:
        return {"_missing": True, "id": ident}
    if isinstance(loaded, dict):
        return normalize(loaded)
    return None


def _coerce(data):
    if isinstance(data, dict):
        return data
    text = "" if data is None else str(data).strip()
    if not text:
        return None
    try:
        loaded = json.loads(text)
    except Exception:
        return text
    if isinstance(loaded, str):
        try:
            again = json.loads(loaded)
        except Exception:
            return loaded
        return again
    return loaded


def _publish(result, notify=True):
    from .properties import setGlobalProperty
    body = json.dumps(result, sort_keys=True, separators=(",", ":"))
    setGlobalProperty("remoteplay", body)
    ident = result.get("id") or ""
    if ident:
        setGlobalProperty("remoteplay.{0}".format(ident), body)
    if notify and result.get("state") != "pending":
        _toast(result)


def _toast(result):
    hook = _HOOKS.get("notify")
    if hook is not None:
        return hook(result)
    try:
        import xbmcgui
        from .i18n import T
        heading = T(35068, "Remote playback")
        if result.get("ok"):
            message = T(35069, "Playback started")
        else:
            message = T(35070, "Playback failed ({0})").format(result.get("reason") or "error")
        xbmcgui.Dialog().notification(heading, message, xbmcgui.NOTIFICATION_INFO, 4000, False)
    except Exception:
        LOG("Remote play: notification failed")


def _pending(request):
    return {
        "state": "pending",
        "id": request.get("id", ""),
        "requested": request.get("ratingKey", ""),
    }


def _fail(request, reason):
    result = _result(
        False,
        reason,
        request.get("id", ""),
        request.get("ratingKey", ""),
        "",
        "",
    )
    _publish(result)
    LOG("Remote play: failed reason={0}", reason)
    return result


def _result(ok, reason, ident, requested, played, kind):
    result = {
        "state": "ok" if ok else "error",
        "ok": bool(ok),
        "reason": reason,
        "id": ident or "",
        "requested": requested or "",
    }
    if ok:
        result["ratingKey"] = played or requested or ""
        result["type"] = kind or ""
    return result


def _kind(item):
    kind = getattr(item, "TYPE", None) or getattr(item, "type", None) or ""
    return str(kind).lower()


def _rating_of(item):
    return str(getattr(item, "ratingKey", "") or "")


def _offset(item):
    view = getattr(item, "viewOffset", None)
    if view is None:
        return 0
    as_int = getattr(view, "asInt", None)
    try:
        return int(as_int() if callable(as_int) else (view or 0))
    except Exception:
        return 0


def _watched(item):
    watched = getattr(item, "isWatched", None)
    if isinstance(watched, bool):
        return watched
    if callable(watched):
        try:
            return bool(watched())
        except Exception:
            pass
    count = getattr(item, "viewCount", None)
    as_int = getattr(count, "asInt", None)
    try:
        return int(as_int() if callable(as_int) else (count or 0)) > 0
    except Exception:
        return False


def _available(item):
    fn = getattr(item, "available", None)
    if not callable(fn):
        return True
    try:
        return bool(fn())
    except Exception:
        return True


def _parse_resume(value):
    if value is None or value == "":
        return "resume"
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("ask", "prompt"):
            return "ask"
        if text in ("0", "false", "no", "start", "beginning"):
            return "start"
        return "resume"
    if value is False or value == 0:
        return "start"
    if value is True or value == 1:
        return "resume"
    return "resume"


def _parse_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "force")
    return bool(value)
