# coding=utf-8
"""Remember a subtitle choice for every episode of a show.

A change the user makes during playback is stored under the show's rating key
and applied to later episodes before the playback decision is built. The
add-on's own first selection is not stored. The store is a small JSON file in
the add-on profile. ``sticky_subtitles`` defaults on.
"""
from __future__ import absolute_import

import json
import os
import threading
import time

_LOCK = threading.Lock()
_SHOWS = None
_APPLIED_EPISODE = None
_ARMED_KEY = None
_SEEN = None
_QUIET_UNTIL = 0.0

# ISO 639-2 to 639-1 for codes Kodi and Plex both use. An unknown code is kept
# as itself so two episodes that share it still match.
_ISO2 = {
    "eng": "en", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr", "spa": "es",
    "ita": "it", "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh", "por": "pt",
    "rus": "ru", "nld": "nl", "dut": "nl", "swe": "sv", "nor": "no", "dan": "da",
    "fin": "fi", "pol": "pl", "ces": "cs", "cze": "cs", "hun": "hu", "tur": "tr",
    "ara": "ar", "heb": "he", "hin": "hi", "tha": "th", "vie": "vi", "ind": "id",
    "ukr": "uk", "ell": "el", "gre": "el", "ron": "ro", "rum": "ro", "bul": "bg",
    "hrv": "hr", "srp": "sr", "slk": "sk", "slo": "sk", "slv": "sl", "cat": "ca",
    "fas": "fa", "per": "fa", "urd": "ur", "fil": "tl", "tgl": "tl",
}

_CLEAR_SHOW = "sticky_subs_clear"
_CLEAR_ALL = "sticky_subs_clear_all"


def enabled():
    try:
        from lib import util
        return util.getSetting("sticky_subtitles", True)
    except Exception:
        return False


def show_key(item):
    """Show rating key. Episodes use ``grandparentRatingKey``."""
    if item is None:
        return ""
    kind = _text(getattr(item, "type", ""))
    if kind == "show":
        return _text(getattr(item, "ratingKey", ""))
    if kind == "season":
        return _text(getattr(item, "parentRatingKey", ""))
    if kind in ("episode", ""):
        return _text(getattr(item, "grandparentRatingKey", ""))
    return ""


def has(key):
    key = _text(key)
    if not key:
        return False
    with _LOCK:
        return key in _ensure()


def any_saved():
    with _LOCK:
        return bool(_ensure())


def get(key):
    key = _text(key)
    if not key:
        return None
    with _LOCK:
        found = _ensure().get(key)
        return dict(found) if found else None


def remember_stream(video, stream):
    """Store a choice the user made. ``None`` and the Plex 'none' stream mean off."""
    if not enabled():
        return False
    key = show_key(video)
    if not key:
        return False
    if _is_none(stream):
        pref = {"mode": "off"}
    else:
        pref = {
            "mode": "track",
            "language": norm_lang(stream_language(stream)),
            "forced": bool(getattr(stream, "forced_subtitle", False)),
            "sdh": bool(getattr(stream, "sdh", False)),
            "codec": _text(getattr(stream, "codec", "")).lower(),
            "stream_id": _text(getattr(stream, "id", "")),
        }
        pref = {name: value for name, value in pref.items() if value or name in ("mode", "forced", "sdh")}
    _put(key, pref)
    _log("remember", pref.get("mode"), pref.get("language") or "")
    _discard_prepared()
    return True


def clear(key):
    key = _text(key)
    if not key:
        return False
    with _LOCK:
        shows = _ensure()
        if key not in shows:
            return False
        del shows[key]
        _write(shows)
    _log("clear", "show", "")
    _discard_prepared()
    return True


def clear_all():
    with _LOCK:
        _write({})
    _log("clear", "all", "")
    _discard_prepared()
    return True


def menu_options(item):
    """Context-menu rows. Empty when the setting is off or nothing is stored."""
    if not enabled() or item is None:
        return []
    kind = _text(getattr(item, "type", ""))
    if kind not in ("show", "episode", "season"):
        return []
    from lib.util import T
    options = []
    key = show_key(item)
    if key and has(key):
        options.append({
            "key": _CLEAR_SHOW,
            "display": T(35066, "Clear saved subtitles for this show"),
        })
    if any_saved():
        options.append({
            "key": _CLEAR_ALL,
            "display": T(35067, "Clear saved subtitles for every show"),
        })
    return options


def handle_menu(choice, item):
    if choice == _CLEAR_SHOW:
        return clear(show_key(item))
    if choice == _CLEAR_ALL:
        return clear_all()
    return False


def apply(video):
    """Select the saved track, or turn subtitles off, before a decision is built.

    Returns True when a saved choice was applied. Does not record a choice.
    """
    if video is None or not enabled():
        return False
    pref = get(show_key(video))
    if not pref:
        return False
    streams = list(getattr(video, "subtitleStreams", None) or [])
    if pref.get("mode") == "off":
        _select_off(video, streams)
        _log("apply", "off", "")
        return True
    stream = _match(streams, pref)
    if stream is None:
        _log("apply", "skip", pref.get("language") or "")
        return False
    _select(video, streams, stream)
    _log("apply", "track", pref.get("language") or "")
    return True


def on_subtitle_pass(video):
    """First subtitle setup for an episode applies the saved choice.

    Later passes are the add-on or the user changing tracks. Those must not
    re-apply an older choice over a change that just happened, and they must
    not be recorded as a Kodi change caused by this add-on.
    """
    global _APPLIED_EPISODE
    episode = _episode_id(video)
    if episode and episode == _APPLIED_EPISODE:
        quiet()
        return
    if episode:
        _APPLIED_EPISODE = episode
    apply(video)


def arm(video, kodi_state=None):
    """Snapshot Kodi's subtitle state after the initial selection. Not a choice."""
    global _ARMED_KEY, _SEEN, _QUIET_UNTIL
    if not enabled():
        _ARMED_KEY = None
        _SEEN = None
        return
    key = show_key(video)
    if not key:
        _ARMED_KEY = None
        _SEEN = None
        return
    _ARMED_KEY = key
    _SEEN = _signature(kodi_state if kodi_state is not None else _read_kodi())
    # Kodi can still report the track this add-on just selected. That is not a choice.
    _QUIET_UNTIL = time.monotonic() + 1.0


def quiet(seconds=1.0):
    """Ignore Kodi subtitle changes caused by this add-on setting the track."""
    global _QUIET_UNTIL
    if _ARMED_KEY:
        _QUIET_UNTIL = time.monotonic() + seconds


def poll(video, kodi_state=None):
    """Record a Kodi subtitle change that happened after the initial selection."""
    global _SEEN
    if not enabled() or video is None or not _ARMED_KEY:
        return False
    if show_key(video) != _ARMED_KEY:
        return False
    state = kodi_state if kodi_state is not None else _read_kodi()
    signature = _signature(state)
    if signature is None:
        return False
    if time.monotonic() < _QUIET_UNTIL:
        _SEEN = signature
        return False
    if signature == _SEEN:
        return False
    _SEEN = signature
    return _remember_signature(video, signature)


def playback_ended(video=None):
    """Last look, then stop watching. The next episode applies its own choice."""
    global _APPLIED_EPISODE, _ARMED_KEY, _SEEN, _QUIET_UNTIL
    if video is not None:
        try:
            poll(video)
        except Exception:
            pass
    _APPLIED_EPISODE = None
    _ARMED_KEY = None
    _SEEN = None
    _QUIET_UNTIL = 0.0


def norm_lang(code):
    text = _text(code).lower().replace("_", "-")
    if not text or text in ("unknown", "none", "und"):
        return ""
    primary = text.split("-", 1)[0]
    if len(primary) == 2:
        return primary
    return _ISO2.get(primary, primary)


def stream_language(stream):
    if stream is None:
        return ""
    for attr in ("languageCode", "languageTag", "language"):
        text = _text(getattr(stream, attr, ""))
        if text and text.lower() not in ("unknown", "none"):
            return text
    return ""


def _remember_signature(video, signature):
    if signature[0] == "off":
        return remember_stream(video, None)
    language, forced, sdh = signature[1], signature[2], signature[3]
    stream = _FakeTrack(language, forced, sdh)
    return remember_stream(video, stream)


class _FakeTrack(object):
    def __init__(self, language, forced, sdh):
        self.id = ""
        self.languageCode = language
        self.forced_subtitle = forced
        self.sdh = sdh
        self.codec = ""


def _signature(state):
    if not isinstance(state, dict):
        return None
    enabled_flag = state.get("subtitleenabled")
    current = state.get("currentsubtitle") or {}
    if not isinstance(current, dict):
        current = {}
    if enabled_flag is False or enabled_flag == 0:
        return ("off",)
    if not current:
        return ("off",)
    return (
        "track",
        norm_lang(current.get("language") or ""),
        bool(current.get("isforced")),
        bool(current.get("isimpaired")),
        current.get("index"),
    )


def _match(streams, pref):
    stream_id = _text(pref.get("stream_id") or "")
    language = norm_lang(pref.get("language") or "")
    forced = bool(pref.get("forced"))
    sdh = bool(pref.get("sdh"))
    codec = _text(pref.get("codec") or "").lower()
    real = [stream for stream in streams if not _is_none(stream)]
    if stream_id:
        for stream in real:
            if _text(getattr(stream, "id", "")) == stream_id:
                return stream
    if not language:
        return None
    candidates = [stream for stream in real if norm_lang(stream_language(stream)) == language]
    if not candidates:
        return None

    def score(stream):
        same_codec = 0
        if codec:
            same_codec = int(_text(getattr(stream, "codec", "")).lower() == codec)
        return (
            int(bool(getattr(stream, "forced_subtitle", False)) == forced),
            int(bool(getattr(stream, "sdh", False)) == sdh),
            same_codec,
        )

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _select(video, streams, stream):
    wanted = _text(getattr(stream, "id", ""))
    for candidate in streams:
        same = candidate is stream or (wanted and _text(getattr(candidate, "id", "")) == wanted)
        _set_selected(candidate, same)
    video._current_subtitle_idx = getattr(stream, "typeIndex", None)
    video.manually_selected_sub_stream = getattr(stream, "id", None)
    video.current_subtitle_is_embedded = bool(getattr(stream, "embedded", False))


def _select_off(video, streams):
    for candidate in streams:
        _set_selected(candidate, False)
    video._current_subtitle_idx = None
    video.manually_selected_sub_stream = False
    video.current_subtitle_is_embedded = False


def _set_selected(stream, selected):
    try:
        stream.setSelected(bool(selected))
    except Exception:
        pass


def _is_none(stream):
    if stream is None:
        return True
    if stream.__class__.__name__ == "NoneStream":
        return True
    # An empty id is a language-only choice (Kodi does not know Plex stream ids).
    # Plex's own "no subtitle" stream is id 0.
    return _text(getattr(stream, "id", "")) == "0"


def _episode_id(video):
    if video is None:
        return ""
    return _text(getattr(video, "ratingKey", ""))


def _text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("", "none"):
        return ""
    return text


def _put(key, pref):
    with _LOCK:
        shows = _ensure()
        shows[key] = pref
        _write(shows)


def _ensure():
    global _SHOWS
    if _SHOWS is None:
        _SHOWS = _read()
    return _SHOWS


def _path():
    override = os.environ.get("PM4K_STICKY_SUBS")
    if override:
        return override
    try:
        from lib import util
        base = util.translatePath(util.ADDON.getAddonInfo("profile"))
    except Exception:
        base = ""
    if not base:
        return ""
    return os.path.join(base, "sticky_subs.json")


def _read():
    path = _path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            obj = json.load(handle)
    except Exception:
        _log("read", "skip", "")
        return {}
    shows = obj.get("shows") if isinstance(obj, dict) else None
    if not isinstance(shows, dict):
        return {}
    clean = {}
    for key, pref in shows.items():
        if _text(key) and isinstance(pref, dict) and pref.get("mode") in ("off", "track"):
            clean[_text(key)] = pref
    return clean


def _write(shows):
    global _SHOWS
    _SHOWS = shows
    path = _path()
    if not path:
        return
    folder = os.path.dirname(path)
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            return
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "shows": shows}, handle, separators=(",", ":"), sort_keys=True)
        os.replace(temporary, path)
    except Exception:
        _log("write", "skip", "")


def _discard_prepared():
    try:
        from lib import playbackprep
        playbackprep.discard_prepared()
    except Exception:
        pass


def _read_kodi():
    try:
        from lib import kodijsonrpc
        players = kodijsonrpc.rpc.Player.GetActivePlayers() or []
        playerid = None
        for player in players:
            if player.get("type") == "video":
                playerid = player.get("playerid")
                break
        if playerid is None:
            return None
        return kodijsonrpc.rpc.Player.GetProperties(
            playerid=playerid,
            properties=["subtitleenabled", "currentsubtitle"],
        )
    except Exception:
        return None


def _log(action, mode, language):
    language = language or "-"
    try:
        from lib import util
        util.DEBUG_LOG(
            "Sticky subs: {} mode={} language={} show=redacted",
            action, mode, language,
        )
    except Exception:
        pass
    try:
        from lib import timing
        if timing.timing_enabled():
            timing._emit("TIMING SUBS action={0} mode={1} language={2} show=redacted".format(
                action, mode, language))
    except Exception:
        pass


def _reset_for_tests():
    global _SHOWS, _APPLIED_EPISODE, _ARMED_KEY, _SEEN, _QUIET_UNTIL
    _SHOWS = None
    _APPLIED_EPISODE = None
    _ARMED_KEY = None
    _SEEN = None
    _QUIET_UNTIL = 0.0
