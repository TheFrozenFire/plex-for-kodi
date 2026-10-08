# coding=utf-8
"""Timing hooks must not raise, and TIMING lines must not contain a token."""
from __future__ import absolute_import

import ast
import os
import threading

from kodienv import ENV

# Importing lib.player starts its monitor thread, which spins until Kodi says
# abort. Set that before the import so the thread exits instead of hanging CI.
ENV.abort_requested = True

from lib import backgroundthread, player, timing
from lib.windows import episodes, home, kodigui, library, preplay, subitems
from lib.windows.mixins import tasks as tasks_mixin
from plexnet import asyncadapter

from .base import KodiTestCase, REPO_ROOT


_SOURCES = (
    "lib/windows/home.py",
    "lib/player.py",
    "lib/windows/library.py",
    "lib/windows/episodes.py",
    "lib/windows/preplay.py",
    "lib/windows/subitems.py",
    "lib/windows/kodigui.py",
    "lib/backgroundthread.py",
    "lib/windows/mixins/tasks.py",
    "lib/_included_packages/plexnet/asyncadapter.py",
)

# Imported so a missing module fails this test, not only the name audit below.
_IMPORTED = (
    home, player, library, episodes, preplay, subitems, kodigui,
    backgroundthread, tasks_mixin, asyncadapter,
)


def _timing_names(path):
    """Names used as ``timing.<name>`` or imported from the timing module."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("timing"):
            for alias in node.names:
                if alias.name != "*":
                    found.add(alias.name)
        value = getattr(node, "value", None)
        if isinstance(node, ast.Attribute) and isinstance(value, ast.Name) and value.id == "timing":
            if not node.attr.startswith("_"):
                found.add(node.attr)
    return found


class _Task(object):
    pass


class _Response(object):
    status_code = 200
    from_cache = False
    headers = {"Content-Length": "4"}
    _content = b"body"
    elapsed = None


class _Handler(object):
    def __init__(self):
        self.av_started = 0
        self.playback_started = 0

    def onAVStarted(self):
        self.av_started += 1

    def onPlayBackStarted(self):
        self.playback_started += 1


class _Player(object):
    def __init__(self):
        self.sessionID = "session"
        self.handler = _Handler()
        self.pauseAfterPlaybackStarted = False
        self._pendingStaleStop = False
        self._pb_span = None
        self.started = False

    def isPlayingVideo(self):
        return False

    def isExternalPlayer(self):
        return False

    def trigger(self, event):
        return None


def _call_sites():
    """Every timing entry point the add-on actually calls."""
    task = _Task()

    def use_span():
        with timing.span("library.open") as opened:
            opened.mark("first")
            opened.after_tasks([task])
        timing.release_task(task)

    def use_span_func():
        @timing.span_func("return.home")
        def wrapped():
            return "ok"

        return wrapped()

    def use_mark():
        timing.mark(None, "first")
        timing.mark(timing.current(), "first")
        opened = timing.begin("home.hubs")
        timing.mark(opened, "first")
        timing.finish(opened)

    def use_finish():
        timing.finish(None)
        timing.finish(timing.begin("open.detail"))

    def use_begin():
        timing.finish(timing.begin("playback.start"))

    def use_pause():
        timing.pause_span(None)
        timing.pause_span(timing.begin("playback.start"))

    def use_play():
        timing.play_phase(None, "decision", mode="direct")
        opened = timing.begin("playback.start")
        timing.play_phase(opened, "decision", mode="direct")
        timing.play_phase(opened, "markers")
        timing.play_phase(opened, "stream_open")
        timing.play_phase(opened, "playing")
        timing.play_phase(opened, "subtitles")
        timing.play_phase(opened, "first_frame")
        timing.mark(opened, "first")
        timing.finish(opened)

    def use_current():
        timing.current().mark("first")
        timing.current().after_tasks([_Task()])

    def use_watch():
        timing.watch_art("http://pms.example/photo/poster")
        kodigui._watch_art("http://pms.example/photo/thumb")

    def use_adopt():
        with timing.adopt(None):
            pass
        opened = timing.begin("open.show")
        with timing.adopt(opened):
            pass
        timing.finish(opened)

    def use_release():
        timing.release_task(_Task())

    def use_note():
        timing.note_task(_Task())
        with timing.span("open.season"):
            timing.note_task(_Task())

    def use_note_playback():
        opened = timing.begin("playback.start")
        timing.note_playback(opened)
        timing.note_playback(None)
        timing.finish(opened)

    def use_http():
        timing.observe_http("GET", "http://pms.example/library/sections", lambda: _Response())

    def use_idle():
        timing.hold_until_tasks(None, None)
        timing.hold_until_tasks(timing.begin_if_idle("home.hubs"), [_Task()])
        with timing.span("return.home"):
            nested = timing.begin_if_idle("home.hubs")
        timing.hold_until_tasks(nested, None)

    return {
        "span": use_span,
        "span_func": use_span_func,
        "mark": use_mark,
        "finish": use_finish,
        "begin": use_begin,
        "pause_span": use_pause,
        "play_phase": use_play,
        "current": use_current,
        "watch_art": use_watch,
        "adopt": use_adopt,
        "release_task": use_release,
        "note_task": use_note,
        "note_playback": use_note_playback,
        "observe_http": use_http,
        "begin_if_idle": use_idle,
        "hold_until_tasks": use_idle,
    }


class TimingSafetyTest(KodiTestCase):
    def setUp(self):
        super(TimingSafetyTest, self).setUp()
        self._previous = os.environ.pop("PM4K_TIMING", None)
        self._thread = threading.current_thread().name
        timing.reset()

    def tearDown(self):
        threading.current_thread().name = self._thread
        if self._previous is None:
            os.environ.pop("PM4K_TIMING", None)
        else:
            os.environ["PM4K_TIMING"] = self._previous
        timing.reset()
        try:
            thread = getattr(player.PLAYER, "thread", None)
            player.PLAYER.close()
            if thread is not None and thread.is_alive():
                thread.join(timeout=1)
        except Exception:
            pass
        super(TimingSafetyTest, self).tearDown()

    def _drive_home_and_player(self):
        window = type("HomeStub", (), {})()
        window._hubs_span = timing.begin("home.hubs")
        home.HomeWindow._noteHubsTiming(window, False)
        home.HomeWindow._noteHubsTiming(window, True)
        self.assertIsNone(window._hubs_span)

        fake = _Player()
        fake._pb_span = timing.begin("playback.start")
        player.PlexPlayer.onPlayBackStarted(fake)
        player.PlexPlayer.onAVStarted(fake)
        self.assertEqual(fake.handler.playback_started, 1)
        self.assertEqual(fake.handler.av_started, 1)
        self.assertIsNone(fake._pb_span)
        self.assertTrue(fake.started)

    def test_instrumented_modules_call_sites_off_and_on(self):
        for module in _IMPORTED:
            self.assertTrue(module.__name__)

        discovered = set()
        for relative in _SOURCES:
            discovered.update(_timing_names(os.path.join(REPO_ROOT, relative)))
        calls = _call_sites()
        missing = discovered - set(calls)
        self.assertFalse(missing, "timing call without a safety invocation: {0}".format(sorted(missing)))
        for name in discovered:
            self.assertTrue(callable(getattr(timing, name)), name)

        for flag in ("0", "1"):
            os.environ["PM4K_TIMING"] = flag
            timing.reset()
            timing.set_art_probe(lambda url: False)
            for name, func in sorted(calls.items()):
                func()
            self._drive_home_and_player()
            if flag == "0":
                self.assertFalse(ENV.logged("TIMING"))

    def test_no_token_in_any_timing_line(self):
        from tools.parse_timings import parse_lines

        token = "sekret-thread"
        url = (
            "https://pms.example:32400/library/metadata/77"
            "?X-Plex-Token={0}&X-Plex-Container-Size=10"
        ).format(token)
        os.environ["PM4K_TIMING"] = "1"
        timing.reset()
        timing.set_art_probe(lambda url: True)
        threading.current_thread().name = "HTTP-ASYNC:{0}".format(url)
        try:
            with timing.span("open.show?X-Plex-Token={0}".format(token)) as opened:
                timing.play_phase(opened, "decision", mode="direct?X-Plex-Token={0}".format(token))
                timing.observe_http("GET", url, lambda: _Response())
                timing.watch_art(url)
                timing.mark(opened, "first")
        finally:
            threading.current_thread().name = self._thread

        timing_lines = [message for message, _level in ENV.log_lines if "TIMING" in message]
        self.assertTrue(timing_lines)
        for message in timing_lines:
            self.assertNotIn(token, message)
            self.assertNotIn("pms.example", message)
        self.assertFalse(ENV.logged(token))
        self.assertTrue(ENV.logged("thread=HTTP-ASYNC:{server}/library/metadata/{id}"))
        self.assertTrue(ENV.logged("X-Plex-Token=****"))
        self.assertTrue(ENV.logged("X-Plex-Container-Size=10"))
        self.assertTrue(ENV.logged("endpoint={server}/library/metadata/{id}"))
        parsed = parse_lines(timing_lines)
        self.assertEqual(len(parsed["requests"]), 1)
        self.assertEqual(parsed["requests"][0]["cache"], "miss")

    def test_broken_hook_is_swallowed_and_playback_still_starts(self):
        os.environ["PM4K_TIMING"] = "1"
        timing.reset()
        opened = timing.begin("playback.start")

        def boom(*args, **kwargs):
            raise RuntimeError("broke X-Plex-Token=sekret-hook")

        original = timing.Span.mark
        timing.Span.mark = boom
        try:
            timing.mark(opened, "first")
            timing.mark(None, "first")
        finally:
            timing.Span.mark = original
        self.assertFalse(ENV.logged("sekret-hook"))
        self.assertTrue(ENV.logged("TIMING HOOK"))

        original_mark = timing.mark
        timing.mark = boom
        try:
            fake = _Player()
            fake._pb_span = opened
            player.PlexPlayer.onAVStarted(fake)
        finally:
            timing.mark = original_mark
        self.assertEqual(fake.handler.av_started, 1)
        self.assertIsNone(fake._pb_span)
        self.assertFalse(ENV.logged("sekret-hook"))

    def test_playlist_playback_span_attributes_other_threads(self):
        os.environ["PM4K_TIMING"] = "1"
        timing.reset()
        real_open = player.PlexPlayer.open
        real_play = player.PlexPlayer._playVideo
        player.PlexPlayer.open = lambda self: None
        player.PlexPlayer._playVideo = lambda *args, **kwargs: None
        try:
            fake = object.__new__(player.PlexPlayer)
            fake.sessionID = "session"
            fake.handler = _Handler()
            fake._pb_span = None
            fake.pauseAfterPlaybackStarted = False
            fake._pendingStaleStop = False
            fake.bgmPlaying = False
            fake.trigger = lambda *args, **kwargs: None
            fake.isPlayingVideo = lambda: True
            fake.isExternalPlayer = lambda: False

            class Offset(object):
                def asInt(self):
                    return 0

            class Video(object):
                viewOffset = Offset()

                def softReload(self, *args, **kwargs):
                    return None

            class Playlist(object):
                isRemote = False

                def current(self):
                    return Video()

            player.PlexPlayer.playVideoPlaylist(fake, Playlist(), resume=False)
        finally:
            player.PlexPlayer.open = real_open
            player.PlexPlayer._playVideo = real_play

        self.assertTrue(ENV.logged("name=playback.start phase=begin"))
        span = fake._pb_span
        self.assertTrue(getattr(span, "real", False))

        url = "https://pms.example:32400/video/:/transcode/universal/decision?X-Plex-Token=sekret-playback"

        def on_thread():
            timing.observe_http("GET", url, lambda: _Response())

        worker = threading.Thread(target=on_thread, name="HTTP-ASYNC:{0}".format(url))
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(ENV.logged("span={0}".format(span.id)))
        self.assertFalse(ENV.logged("sekret-playback"))
        self.assertFalse(ENV.logged("pms.example"))

        fake.handler = _Handler()
        player.PlexPlayer.onPlayBackStarted(fake)
        player.PlexPlayer.onAVStarted(fake)
        self.assertTrue(ENV.logged("phase=playing"))
        self.assertTrue(ENV.logged("phase=first_frame"))
        self.assertTrue(ENV.logged("name=playback.start phase=first"))
        self.assertTrue(ENV.logged("name=playback.start phase=full"))
        self.assertIsNone(fake._pb_span)
        self.assertEqual(fake.handler.av_started, 1)
        self.assertEqual(fake.handler.playback_started, 1)

        timing.observe_http("GET", "http://pms.example/library/sections", lambda: _Response())
        lines = [message for message, _level in ENV.log_lines if "library/sections" in message]
        self.assertTrue(lines)
        self.assertTrue(all("span=-" in message or "span={0}".format(span.id) not in message for message in lines))
        self.assertTrue(any("span=-" in message for message in lines))
