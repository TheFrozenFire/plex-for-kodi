# coding=utf-8
"""Content skip rules.

Rules live in ``skip_rules.json`` in the add-on profile. An external tool
edits that file. This module only reads it: at startup, before each
playback start, when the file's modification time changes, and when
``PM4K_SKIPS_RELOAD`` asks for a fresh read. A missing file means no rules.
A file that cannot be parsed leaves the last good copy in memory. The
feature is off until ``skip_rules_enabled`` is on.

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

_POLL_S = 0.25
_EOF_SLACK_MS = 500
_INF = 10 ** 18

_LOCK = threading.Lock()
_doc = None
_mtime = None
_watch = None
_override = None
_toast_hook = None
_last_poll = None
_path_override = None
_pending = None
_next_poke = 0.0

_IMDB_RE = re.compile(r"imdb://(tt\d+)", re.IGNORECASE)
_TVDB_RE = re.compile(r"(?:the)?tvdb://(\d+)", re.IGNORECASE)


def reset():
    """Drop in-memory state. Tests and shutdown."""
    global _doc, _mtime, _watch, _override, _toast_hook, _last_poll, _path_override
    with _LOCK:
        _doc = None
        _mtime = None
        _watch = None
        _override = None
        _toast_hook = None
        _last_poll = None
        _path_override = None
        _pending = None
        _next_poke = 0.0


def set_override(settings):
    """Test hook. ``None`` reads the add-on settings again."""
    global _override
    with _LOCK:
        _override = dict(settings) if settings is not None else None


def set_toast_hook(hook):
    global _toast_hook
    with _LOCK:
        _toast_hook = hook


def set_rules_path(path):
    """Test hook. Production reads ``skip_rules.json`` from the add-on profile."""
    global _path_override
    with _LOCK:
        _path_override = path


def arm():
    """Read the rules file once at startup. No extra thread."""
    try:
        load()
    except Exception:
        _log("startup load failed")


def reload_now():
    """NotifyAll handler. Read the file again even if the mtime is unchanged."""
    try:
        load(force=True)
    except Exception:
        _log("reload failed")


def load(force=False):
    """Read ``skip_rules.json``.

    A missing file clears the rules. A parse failure keeps the last good
    copy and does not remember the bad mtime, so a later read tries again.
    The add-on never writes this file.
    """
    global _doc, _mtime
    path = rules_path()
    if not path or not os.path.isfile(path):
        empty = {"version": 1, "updated_at": "", "rules": []}
        with _LOCK:
            _doc = empty
            _mtime = None
        return empty
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _log("rules file could not be read")
        return _current_doc()
    with _LOCK:
        unchanged = (not force) and _doc is not None and _mtime == mtime
        if unchanged:
            return _doc
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        _log("rules file could not be parsed")
        return _current_doc()
    document, errors = parse_document(payload)
    for error in errors:
        _log("ignored rule {0}".format(error))
    if document is None:
        _log("rules file rejected")
        return _current_doc()
    with _LOCK:
        _doc = document
        _mtime = mtime
    return document


def prepare(item, has_next=False, refresh_now=True):
    """Match this item and decide whether it should start.

    Returns ``play``, ``next`` (episode with something queued after it),
    or ``stop``. A missing or unreadable rules file still returns ``play``
    when nothing matches. ``refresh_now`` is false while walking further
    skipped episodes in the same start, so one playback start reads once.
    """
    global _watch
    if not _settings()["enabled"]:
        with _LOCK:
            _watch = None
        return {"action": "play"}
    if refresh_now:
        try:
            load()
        except Exception:
            _log("prepare load failed")
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
    lead_ms = _settings()["lead_ms"]
    with _LOCK:
        _watch = Watch(ranges, lead_ms)
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
            "lead_ms": lead,
            "toast": bool(getSetting("skip_rules_toast", True)),
        }
    except Exception:
        return {"enabled": False, "lead_ms": 1000, "toast": True}


def _current_doc():
    with _LOCK:
        if not _doc:
            return {"version": 1, "updated_at": "", "rules": []}
        return _doc


def rules_path():
    with _LOCK:
        override = _path_override
    if override:
        return override
    try:
        from . import util
        base = util.PROFILE or ""
    except Exception:
        base = ""
    if not base:
        return ""
    return os.path.join(base, "skip_rules.json")


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
