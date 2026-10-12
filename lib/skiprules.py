# coding=utf-8
"""Content skip rules.

A remote document lists time ranges to jump, or whole items to pass over.
The add-on pulls that document (the TV does not accept inbound connections),
keeps the last good copy, and applies it during playback. A failed fetch
never stops playback. The feature is off until ``skip_rules_enabled`` is on.

The playhead is sampled from the existing player monitor, which already
reads the player clock. That path does not touch list items. A seek or a
stop is the same kind of player call the monitor already makes.
"""
from __future__ import absolute_import

import json
import os
import re
import threading
import time

_INTERVAL_S = 600
_FETCH_TIMEOUT_S = 3.0
_POLL_S = 0.25
_EOF_SLACK_MS = 500
_INF = 10 ** 18

_LOCK = threading.Lock()
_doc = None
_etag = None
_watch = None
_override = None
_fetcher = None
_toast_hook = None
_next_refresh = 0.0
_last_poll = None
_cache_override = None
_pending = None
_next_poke = 0.0

_IMDB_RE = re.compile(r"imdb://(tt\d+)", re.IGNORECASE)
_TVDB_RE = re.compile(r"(?:the)?tvdb://(\d+)", re.IGNORECASE)


class _Receiver(object):
    """Cron tick. Fetches when the interval has elapsed. No GUI work."""

    def tick(self):
        try:
            maybe_refresh()
        except Exception:
            _log("periodic refresh failed")

    def halfHour(self):
        return False

    def day(self):
        return False


_RECEIVER = _Receiver()


def reset():
    """Drop in-memory state. Tests and shutdown."""
    global _doc, _etag, _watch, _override, _fetcher, _toast_hook, _next_refresh, _last_poll, _cache_override
    with _LOCK:
        _doc = None
        _etag = None
        _watch = None
        _override = None
        _fetcher = None
        _toast_hook = None
        _next_refresh = 0.0
        _last_poll = None
        _cache_override = None
        _pending = None
        _next_poke = 0.0


def set_override(settings):
    """Test hook. ``None`` reads the add-on settings again."""
    global _override
    with _LOCK:
        _override = dict(settings) if settings is not None else None


def set_fetcher(fetcher):
    """Test hook. ``fetcher(url, headers, timeout) -> (status, headers, body)``."""
    global _fetcher
    with _LOCK:
        _fetcher = fetcher


def set_toast_hook(hook):
    global _toast_hook
    with _LOCK:
        _toast_hook = hook


def set_cache_path(path):
    global _cache_override
    with _LOCK:
        _cache_override = path


def arm():
    """Register the periodic fetch on the existing cron, if it is running."""
    global _next_refresh
    with _LOCK:
        _next_refresh = 0.0
    try:
        from . import util
        cron = getattr(util, "CRON", None)
        if cron is not None:
            cron.registerReceiver(_RECEIVER)
    except Exception:
        _log("arm failed")


def maybe_refresh(now=None):
    """Fetch when enabled and the interval has elapsed."""
    global _next_refresh
    if not _settings()["enabled"]:
        return
    moment = time.monotonic() if now is None else now
    with _LOCK:
        due = moment >= _next_refresh
        if due:
            _next_refresh = moment + _INTERVAL_S
    if due:
        refresh()


def reload_now():
    """NotifyAll handler. Fetch immediately, still with If-None-Match."""
    global _next_refresh
    with _LOCK:
        _next_refresh = time.monotonic() + _INTERVAL_S
    refresh()


def refresh():
    """Pull the document. On any failure, keep the last good copy."""
    global _doc, _etag
    settings = _settings()
    if not settings["enabled"]:
        return _current_doc()
    _ensure_loaded()
    url = (settings.get("url") or "").strip()
    if not url:
        return _current_doc()
    headers = {"Accept": "application/json"}
    token = (settings.get("token") or "").strip()
    if token:
        headers["Authorization"] = "Bearer {0}".format(token)
    with _LOCK:
        etag = _etag
        fetcher = _fetcher
    if etag:
        headers["If-None-Match"] = etag
    try:
        if fetcher is not None:
            status, response_headers, body = fetcher(url, headers, _FETCH_TIMEOUT_S)
        else:
            status, response_headers, body = _http_fetch(url, headers, _FETCH_TIMEOUT_S)
    except Exception:
        _log("fetch failed")
        return _current_doc()
    response_headers = {str(k).lower(): v for k, v in (response_headers or {}).items()}
    new_etag = response_headers.get("etag") or etag
    if status == 304:
        with _LOCK:
            if new_etag:
                _etag = new_etag
        _store()
        return _current_doc()
    if status != 200 or not body:
        _log("fetch status {0}".format(status))
        return _current_doc()
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception:
        _log("document was not json")
        return _current_doc()
    document, errors = parse_document(payload)
    for error in errors:
        _log("ignored rule {0}".format(error))
    if document is None:
        _log("document rejected")
        return _current_doc()
    with _LOCK:
        _doc = document
        _etag = new_etag
    _store()
    return document


def prepare(item, has_next=False, refresh_now=True):
    """Match this item and decide whether it should start.

    Returns ``play``, ``next`` (episode with something queued after it),
    or ``stop``. Network and parse failures still return ``play``.
    ``refresh_now`` is false while walking further skipped episodes in the
    same start, so one playback start causes one fetch.
    """
    global _watch
    if not _settings()["enabled"]:
        with _LOCK:
            _watch = None
        return {"action": "play"}
    if refresh_now:
        try:
            refresh()
        except Exception:
            _log("prepare refresh failed")
    ident = identity(item)
    matched = select(_current_doc().get("rules") or [], ident)
    _publish(ident, matched)
    skipped = [rule for rule in matched if rule["type"] == "skip_item"]
    if skipped:
        with _LOCK:
            _watch = None
        reason = skipped[0].get("reason") or ""
        _toast_item(reason)
        if ident.get("type") == "episode" and has_next:
            return {"action": "next", "reason": reason}
        return {"action": "stop", "reason": reason}
    ranges = merge_ranges([rule for rule in matched if rule["type"] == "skip_range"])
    with _LOCK:
        _watch = Watch(ranges, _settings()["lead_ms"])
    return {"action": "play"}


def poll(position_ms, duration_ms=None, now=None):
    """Sample the playhead. At most one decision per poll interval."""
    global _last_poll
    if not _settings()["enabled"]:
        return None
    moment = time.monotonic() if now is None else now
    with _LOCK:
        watch = _watch
        last = _last_poll
        if last is not None and (moment - last) < _POLL_S:
            return None
        _last_poll = moment
    if watch is None:
        return None
    return watch.consider(position_ms, duration_ms)


def announce(decision):
    """Notice for a range skip. Called on the script thread when the action runs."""
    if decision and decision.get("action") in ("seek", "end"):
        _toast_scene()


def clear_session():
    global _watch, _last_poll, _pending
    with _LOCK:
        _watch = None
        _last_poll = None
        _pending = None


def queue_action(decision):
    """Hold one player action for the script thread. The monitor must not apply it."""
    global _pending
    if not decision:
        return
    with _LOCK:
        _pending = dict(decision)


def take_action():
    """Script-thread drain. Returns the pending action once."""
    global _pending
    with _LOCK:
        decision = _pending
        _pending = None
        return decision


def has_action():
    with _LOCK:
        return _pending is not None


def poke_due(now=None):
    """True when a pending action should wake the window again."""
    global _next_poke
    moment = time.monotonic() if now is None else now
    with _LOCK:
        if _pending is None:
            return False
        if moment < _next_poke:
            return False
        _next_poke = moment + 1.0
        return True


def parse_document(payload):
    """Return ``(document, errors)``. ``document`` is None when the file is unusable."""
    if not isinstance(payload, dict):
        return None, ["document"]
    version = payload.get("version")
    if version != 1 and str(version) != "1":
        return None, ["version"]
    raw_rules = payload.get("rules")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        return None, ["rules"]
    rules = []
    errors = []
    for index, raw in enumerate(raw_rules):
        rule, why = _parse_rule(raw)
        if rule is None:
            ident = ""
            if isinstance(raw, dict) and raw.get("id"):
                ident = str(raw.get("id"))[:80]
            errors.append("{0}:{1}".format(ident or index, why))
            continue
        rules.append(rule)
    return {
        "version": 1,
        "updated_at": payload.get("updated_at") or "",
        "rules": rules,
    }, errors


def select(rules, ident):
    """Rating key, then external ids, then title. The first tier that hits wins."""
    by_key = []
    by_guid = []
    by_title = []
    for rule in rules or []:
        kind = _match_kind(rule, ident)
        if kind == "key":
            by_key.append(rule)
        elif kind == "guid":
            by_guid.append(rule)
        elif kind == "title":
            by_title.append(rule)
    if by_key:
        return by_key
    if by_guid:
        return by_guid
    return by_title


def merge_ranges(rules):
    """Overlap or touch becomes one range. An open end stays open."""
    spans = []
    for rule in rules or []:
        end = rule.get("end_ms")
        spans.append({
            "start_ms": int(rule["start_ms"]),
            "end_ms": None if end is None else int(end),
            "ids": [rule["id"]],
            "reason": rule.get("reason") or "",
        })
    spans.sort(key=lambda row: (row["start_ms"], _end_key(row["end_ms"])))
    merged = []
    for span in spans:
        if not merged or span["start_ms"] > _end_key(merged[-1]["end_ms"]):
            merged.append(dict(span))
            continue
        last = merged[-1]
        if last["end_ms"] is None or span["end_ms"] is None:
            last["end_ms"] = None
        else:
            last["end_ms"] = max(last["end_ms"], span["end_ms"])
        for ident in span["ids"]:
            if ident not in last["ids"]:
                last["ids"].append(ident)
        if span["reason"] and span["reason"] not in (last["reason"] or ""):
            last["reason"] = span["reason"] if not last["reason"] else last["reason"]
    return merged


def identity(item):
    """Plain facts used for matching. Episode guids stay on the episode."""
    if isinstance(item, dict) and "rating_key" in item and "type" in item:
        copied = dict(item)
        copied["imdb"] = set(copied.get("imdb") or [])
        copied["tvdb"] = set(str(x) for x in (copied.get("tvdb") or []))
        return copied
    imdb, tvdb = _guid_ids(item)
    kind = str(getattr(item, "type", None) or getattr(item, "TYPE", None) or "")
    server = getattr(item, "server", None)
    return {
        "rating_key": str(getattr(item, "ratingKey", "") or ""),
        "server_id": str(getattr(server, "machineIdentifier", None) or getattr(server, "uuid", "") or ""),
        "server_name": str(getattr(server, "name", "") or ""),
        "type": kind,
        "imdb": imdb,
        "tvdb": tvdb,
        "show_title": str(getattr(item, "grandparentTitle", "") or ""),
        "season": _as_int(getattr(item, "parentIndex", None)),
        "episode": _as_int(getattr(item, "index", None)),
        "movie_title": str(getattr(item, "title", "") or ""),
        "year": _as_int(getattr(item, "year", None)),
        "duration_ms": _as_int(getattr(item, "duration", None)),
    }


class Watch(object):
    """One skip per entry. A later entry is armed only after the playhead leaves."""

    def __init__(self, ranges, lead_ms):
        self.ranges = list(ranges or [])
        self.lead_ms = max(0, int(lead_ms or 0))
        self.held = set()

    def consider(self, position_ms, duration_ms=None):
        try:
            pos = int(position_ms)
        except (TypeError, ValueError):
            return None
        duration = None
        if duration_ms is not None:
            try:
                duration = int(duration_ms)
            except (TypeError, ValueError):
                duration = None
        entered = []
        for index, span in enumerate(self.ranges):
            if _inside(span, pos, self.lead_ms, duration):
                if index not in self.held:
                    entered.append(index)
            else:
                self.held.discard(index)
        if not entered:
            return None
        for index in entered:
            self.held.add(index)
        chosen = max(entered, key=lambda index: _end_key(self.ranges[index]["end_ms"]))
        span = self.ranges[chosen]
        end = span["end_ms"]
        if end is None or (duration is not None and end >= duration - _EOF_SLACK_MS):
            return {"action": "end", "ids": list(span["ids"])}
        return {"action": "seek", "to_ms": int(end), "ids": list(span["ids"])}


def _inside(span, pos, lead_ms, duration):
    start = int(span["start_ms"]) - int(lead_ms)
    end = span["end_ms"]
    if end is None:
        if duration is None:
            return pos >= start
        return start <= pos < duration
    return start <= pos < int(end)


def _end_key(end):
    return _INF if end is None else int(end)


def _parse_rule(raw):
    if not isinstance(raw, dict):
        return None, "shape"
    ident = raw.get("id")
    if not isinstance(ident, str) or not ident.strip():
        return None, "id"
    kind = raw.get("type")
    if kind not in ("skip_range", "skip_item"):
        return None, "type"
    rule = {
        "id": ident.strip()[:80],
        "type": kind,
        "reason": _short_text(raw.get("reason")),
    }
    for key in ("plex_rating_key", "server", "show_title", "movie_title", "imdb", "tvdb", "source"):
        value = raw.get(key)
        if value is None or value == "":
            continue
        rule[key] = str(value).strip()
    for key in ("season", "episode", "year"):
        number = _as_int(raw.get(key))
        if number is not None:
            rule[key] = number
    if kind == "skip_range":
        start = _as_int(raw.get("start_ms"))
        if start is None or start < 0:
            return None, "start"
        end = raw.get("end_ms")
        if end is None:
            rule["end_ms"] = None
        else:
            end = _as_int(end)
            if end is None or end <= start:
                return None, "end"
            rule["end_ms"] = end
        rule["start_ms"] = start
    if not _has_matcher(rule):
        return None, "matcher"
    return rule, ""


def _has_matcher(rule):
    if rule.get("plex_rating_key"):
        return True
    if rule.get("imdb") or rule.get("tvdb"):
        return True
    if rule.get("show_title") and rule.get("season") is not None and rule.get("episode") is not None:
        return True
    if rule.get("movie_title") and rule.get("year") is not None:
        return True
    return False


def _match_kind(rule, ident):
    if rule.get("server") and not _server_ok(rule.get("server"), ident):
        return None
    if rule.get("plex_rating_key"):
        if str(ident.get("rating_key") or "") == str(rule["plex_rating_key"]):
            return "key"
        return None
    imdb = str(rule.get("imdb") or "").lower()
    tvdb = str(rule.get("tvdb") or "")
    if imdb or tvdb:
        ids = ident.get("imdb") or set()
        shows = ident.get("tvdb") or set()
        if imdb and imdb in ids:
            return "guid"
        if tvdb and tvdb in shows:
            return "guid"
        return None
    if _title_match(rule, ident):
        return "title"
    return None


def _title_match(rule, ident):
    if rule.get("show_title") and rule.get("season") is not None and rule.get("episode") is not None:
        if ident.get("season") is None or ident.get("episode") is None:
            return False
        return (
            _norm(rule["show_title"]) == _norm(ident.get("show_title"))
            and int(rule["season"]) == int(ident["season"])
            and int(rule["episode"]) == int(ident["episode"])
        )
    if rule.get("movie_title") and rule.get("year") is not None:
        if ident.get("year") is None:
            return False
        return _norm(rule["movie_title"]) == _norm(ident.get("movie_title")) and int(rule["year"]) == int(ident["year"])
    return False


def _server_ok(wanted, ident):
    text = str(wanted or "").strip()
    if not text:
        return True
    if text == str(ident.get("server_id") or ""):
        return True
    return _norm(text) == _norm(ident.get("server_name"))


def _guid_ids(item):
    texts = []
    guid = getattr(item, "guid", None)
    if guid:
        texts.append(str(guid))
    for entry in getattr(item, "guids", None) or []:
        if isinstance(entry, dict):
            gid = entry.get("id")
        else:
            gid = getattr(entry, "id", None)
        if gid:
            texts.append(str(gid))
    imdb = set()
    tvdb = set()
    for text in texts:
        for match in _IMDB_RE.finditer(text):
            imdb.add(match.group(1).lower())
        for match in _TVDB_RE.finditer(text):
            tvdb.add(match.group(1))
    return imdb, tvdb


def _publish(ident, matched):
    payload = {
        "rating_key": ident.get("rating_key") or "",
        "type": ident.get("type") or "",
        "matched": [{
            "id": rule["id"],
            "type": rule["type"],
            "reason": rule.get("reason") or "",
            "start_ms": rule.get("start_ms"),
            "end_ms": rule.get("end_ms"),
        } for rule in matched],
    }
    try:
        from .properties import setGlobalProperty
        setGlobalProperty("skiprules", json.dumps(payload, sort_keys=True))
    except Exception:
        _log("status publish failed")


def _toast_item(reason):
    if reason:
        _toast(_text(35082, "Skipped: {reason}").replace("{reason}", reason))
    else:
        _toast(_text(35083, "Skipped"))


def _toast_scene():
    _toast(_text(35081, "Skipped scene"))


def _toast(message):
    if not _settings()["toast"]:
        return
    try:
        with _LOCK:
            hook = _toast_hook
        if hook is not None:
            hook(message)
            return
        from .logging import showNotification
        showNotification(message, time_ms=2500)
    except Exception:
        _log("toast failed")


def _text(string_id, fallback):
    try:
        from .i18n import T
        return T(string_id, fallback)
    except Exception:
        return fallback


def _settings():
    with _LOCK:
        override = dict(_override) if _override is not None else None
    if override is not None:
        override.setdefault("enabled", False)
        override.setdefault("url", "")
        override.setdefault("token", "")
        override.setdefault("lead_ms", 1000)
        override.setdefault("toast", True)
        return override
    try:
        from .settings_util import getSetting
        lead = getSetting("skip_lead_in_ms", 1000)
        try:
            lead = int(lead)
        except (TypeError, ValueError):
            lead = 1000
        return {
            "enabled": bool(getSetting("skip_rules_enabled", False)),
            "url": getSetting("skip_rules_url", "") or "",
            "token": getSetting("skip_rules_token", "") or "",
            "lead_ms": lead,
            "toast": bool(getSetting("skip_rules_toast", True)),
        }
    except Exception:
        return {"enabled": False, "url": "", "token": "", "lead_ms": 1000, "toast": True}


def _current_doc():
    _ensure_loaded()
    with _LOCK:
        return _doc or {"version": 1, "updated_at": "", "rules": []}


def _ensure_loaded():
    global _doc, _etag
    with _LOCK:
        if _doc is not None:
            return
    loaded = _read_cache()
    if not loaded:
        with _LOCK:
            if _doc is None:
                _doc = {"version": 1, "updated_at": "", "rules": []}
        return
    with _LOCK:
        if _doc is None:
            _doc = loaded.get("document") or {"version": 1, "updated_at": "", "rules": []}
            _etag = loaded.get("etag") or None


def cache_path():
    with _LOCK:
        override = _cache_override
    if override:
        return override
    env = os.environ.get("PM4K_SKIP_RULES_CACHE")
    if env:
        return env
    try:
        from . import util
        base = util.PROFILE or ""
    except Exception:
        base = ""
    if not base:
        return ""
    return os.path.join(base, "skip_rules.json")


def _read_cache():
    path = cache_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        _log("cache unreadable")
        return None
    document = payload.get("document") if isinstance(payload, dict) else None
    if not isinstance(document, dict):
        return None
    return payload


def _store():
    path = cache_path()
    if not path:
        return
    with _LOCK:
        payload = {"etag": _etag or "", "document": _doc}
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        _log("cache write failed")


def _http_fetch(url, headers, timeout):
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read()
            hdrs = {k.lower(): v for k, v in response.headers.items()}
            return status, hdrs, body
    except urllib.error.HTTPError as exc:
        hdrs = {}
        if exc.headers:
            hdrs = {k.lower(): v for k, v in exc.headers.items()}
        body = b""
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, hdrs, body


def _as_int(value):
    if value is None or value == "":
        return None
    if hasattr(value, "asInt"):
        try:
            return int(value.asInt())
        except (TypeError, ValueError):
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return int(number)


def _short_text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:120]


def _norm(value):
    return str(value or "").strip().casefold()


def _log(message):
    try:
        from .logging import log
        log("Skip rules: {0}".format(message))
    except Exception:
        pass
