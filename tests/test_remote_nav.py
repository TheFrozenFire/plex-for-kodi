# coding=utf-8
"""External navigation stays off the GUI thread until the script thread."""
from __future__ import absolute_import

import ast
import os
import threading

import xbmc
from kodienv import ENV

from lib import hubrefresh, monitor, remotenav
from lib.windows.seekdialog import MARKER_END_JUMP_OFF, SeekDialog

from .base import KodiTestCase, REPO_ROOT


class _Section(object):
    def __init__(self, kind, item_type=None, title=""):
        self.type = kind
        self.title = title
        if item_type:
            self.itemType = item_type

    def __repr__(self):
        return "<{0}>".format(self.type)


class _Row(object):
    def __init__(self, section):
        self.dataSource = section


class _Rows(object):
    def __init__(self, rows):
        self.rows = list(rows)
        self.selected = None

    def __iter__(self):
        return iter(self.rows)

    def setSelectedItem(self, item):
        self.selected = item

    def getSelectedItem(self):
        return self.selected


class _Window(object):
    SECTION_LIST_ID = 300

    def __init__(self, rows=None):
        self.sectionList = rows if rows is not None else _Rows([])
        self.go_root = False
        self._winID = 13001
        self.shown = 0
        self.went_home = 0
        self.focused_control = []
        self.clicks = []
        self.consume_root = True

    def show(self):
        self.shown += 1
        if self.go_root and self.consume_root:
            self.go_root = False

    def goHome(self):
        self.went_home += 1
        self.focused_control.append(self.SECTION_LIST_ID)

    def setFocusId(self, ident):
        self.focused_control.append(ident)

    def sectionClicked(self):
        self.clicks.append(self.sectionList.selected)


class RemoteNavTest(KodiTestCase):
    def setUp(self):
        super(RemoteNavTest, self).setUp()
        remotenav._reset_for_tests()
        hubrefresh.reset()
        hubrefresh.note_script_thread()
        self.home = _Window()
        self.opened = []
        self.focused = []
        self.closed = []
        self.poked = []
        self.playing = False
        self.overlay = False
        remotenav._HOOKS["home"] = lambda: self.home
        remotenav._HOOKS["playing"] = lambda: self.playing
        remotenav._HOOKS["poke"] = lambda: self.poked.append(1)
        remotenav._HOOKS["overlays"] = lambda home: self.overlay
        remotenav._HOOKS["close_overlays"] = lambda: self.closed.append(1)
        remotenav._HOOKS["focus_home"] = lambda home: self.focused.append(home)
        remotenav._HOOKS["open_section"] = self._open

    def tearDown(self):
        remotenav._reset_for_tests()
        hubrefresh.reset()
        super(RemoteNavTest, self).tearDown()

    def _open(self, home, spec):
        self.opened.append(spec.get("type"))
        return True

    def test_home_selects_the_home_section_on_the_script_thread(self):
        result = remotenav.accept_notification('{"to":"home"}')
        self.assertEqual({"ok": True, "to": "home"}, result)
        self.assertEqual([self.home], self.focused)
        self.assertFalse(self.opened)
        self.assertFalse(self.closed)
        self.assertTrue(ENV.logged("Remote nav: ok to=home"))

    def test_section_opens_the_matching_library(self):
        show = remotenav.accept_notification('{"to": "section", "type": "show"}')
        movie = remotenav.accept_notification({"to": "section", "type": "Movie"})
        self.assertEqual({"ok": True, "to": "section", "type": "show"}, show)
        self.assertEqual("movie", movie["type"])
        self.assertEqual(["show", "movie"], self.opened)
        self.assertTrue(ENV.logged("Remote nav: ok to=section type=show"))
        self.assertTrue(ENV.logged("Remote nav: ok to=section type=movie"))

    def test_playing_video_is_ignored(self):
        self.playing = True
        result = remotenav.accept_notification('{"to":"home"}')
        self.assertIsNone(result)
        self.assertFalse(self.focused)
        self.assertFalse(self.closed)
        self.assertTrue(ENV.logged("Remote nav: ignored reason=playing"))

        self.playing = False
        remotenav._HOOKS["playing"] = None
        xbmc.Player.playing_video = True
        remotenav.accept_notification('{"to":"section","type":"movie"}')
        self.assertFalse(self.opened)
        self.assertTrue(ENV.logged("Remote nav: ignored reason=playing"))

    def test_bad_data_is_ignored_and_does_not_raise(self):
        samples = (
            None,
            "",
            "nope",
            "[]",
            "{}",
            '{"to":"library"}',
            '{"to":"section"}',
            '{"to":"section","type":"artist"}',
            '{"to":"section","type":"movies"}',
            b"\xff",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNone(remotenav.accept_notification(sample))
        self.assertFalse(self.focused)
        self.assertFalse(self.opened)
        self.assertFalse(self.poked)
        self.assertTrue(ENV.logged("Remote nav: ignored reason=bad_request"))

    def test_double_encoded_json_is_accepted(self):
        result = remotenav.accept_notification('"{\\"to\\":\\"home\\"}"')
        self.assertEqual("home", result["to"])
        self.assertTrue(result["ok"])

    def test_not_ready_when_home_is_missing(self):
        remotenav._HOOKS["home"] = lambda: None
        result = remotenav.accept_notification('{"to":"section","type":"show"}')
        self.assertIsNone(result)
        self.assertFalse(self.opened)
        self.assertTrue(ENV.logged("Remote nav: failed reason=not_ready"))

    def test_missing_library_is_logged(self):
        remotenav._HOOKS["open_section"] = lambda home, spec: False
        result = remotenav.accept_notification('{"to":"section","type":"show"}')
        self.assertEqual("no_section", result["reason"])
        self.assertFalse(result["ok"])
        self.assertTrue(ENV.logged("Remote nav: failed reason=no_section type=show"))

    def test_a_window_above_home_is_closed_before_navigation(self):
        self.overlay = True

        def close():
            self.closed.append(1)
            self.overlay = False

        remotenav._HOOKS["close_overlays"] = close
        first = remotenav.accept_notification('{"to":"section","type":"movie"}')
        self.assertIsNone(first)
        self.assertTrue(self.home.go_root)
        self.assertEqual([1], self.closed)
        self.assertFalse(self.opened)
        remotenav.drain()
        self.assertEqual(["movie"], self.opened)
        self.assertTrue(ENV.logged("Remote nav: ok to=section type=movie"))

    def test_deferred_request_stays_ahead_of_a_later_one(self):
        self.overlay = True
        remotenav._HOOKS["on_gui"] = lambda: False
        remotenav.accept_notification('{"to":"section","type":"show"}')
        remotenav.accept_notification('{"to":"home"}')
        remotenav._HOOKS["on_gui"] = None
        self.poked[:] = []
        remotenav.drain()
        self.assertFalse(self.opened)
        self.assertFalse(self.focused)
        self.overlay = False
        remotenav.drain()
        self.assertEqual(["show"], self.opened)
        self.assertEqual([self.home], self.focused)

    def test_background_thread_does_not_navigate_until_drain(self):
        errors = []

        def worker():
            try:
                remotenav.accept_notification('{"to":"home"}')
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.focused)
        self.assertTrue(self.poked)
        remotenav.drain()
        self.assertEqual([self.home], self.focused)

    def test_drain_off_the_script_thread_waits(self):
        hubrefresh.reset()
        remotenav.accept_notification('{"to":"home"}')
        remotenav.drain()
        self.assertFalse(self.focused)
        hubrefresh.note_script_thread()
        from lib.windows import kodigui
        kodigui._service_script_thread(type("Win", (), {"_art_token": None})())
        self.assertEqual([self.home], self.focused)

    def test_nudge_wakes_a_queued_request_and_not_an_empty_queue(self):
        remotenav.nudge()
        self.assertFalse(self.poked)
        remotenav._HOOKS["on_gui"] = lambda: False
        remotenav.accept_notification('{"to":"home"}')
        self.poked[:] = []
        remotenav.nudge()
        self.assertEqual([1], self.poked)

    def test_errors_do_not_escape(self):
        def boom(home):
            raise RuntimeError("nope")

        remotenav._HOOKS["focus_home"] = boom
        result = remotenav.accept_notification('{"to":"home"}')
        self.assertFalse(result["ok"])
        self.assertTrue(ENV.logged("Remote nav: failed reason=error"))

        def broken():
            raise RuntimeError("home")

        remotenav._HOOKS["home"] = broken
        self.assertIsNone(remotenav.accept_notification('{"to":"section","type":"movie"}'))

    def test_monitor_dispatches_the_notification(self):
        monitor.MONITOR.onNotification("script.plexmod", "Other.PM4K_NAV", '{"to":"home"}')
        self.assertEqual([self.home], self.focused)
        self.focused[:] = []
        monitor.MONITOR.onNotification("xbmc", "Other.PM4K_NAV", '{"to":"home"}')
        monitor.MONITOR.onNotification("script.plexmod", "Other.PM4K_PLAY", '{"to":"home"}')
        self.assertFalse(self.focused)

    def test_real_focus_uses_go_root_then_go_home(self):
        remotenav._HOOKS["focus_home"] = None
        remotenav._focus_home(self.home)
        self.assertEqual(1, self.home.shown)
        self.assertEqual(0, self.home.went_home)
        self.assertFalse(self.home.go_root)

        self.home.consume_root = False
        remotenav._focus_home(self.home)
        self.assertEqual(1, self.home.went_home)
        self.assertFalse(self.home.go_root)
        self.assertIn(self.home.SECTION_LIST_ID, self.home.focused_control)

    def test_real_open_picks_the_first_library_and_clicks_it(self):
        remotenav._HOOKS["open_section"] = None
        pinned = _Section("movie", item_type="collection")
        first_movie = _Section("movie")
        show = _Section("show")
        later_movie = _Section("movie")
        window = _Window(_Rows([
            _Row(_Section("home")),
            _Row(_Section("playlists")),
            _Row(_Section("mixed")),
            _Row(pinned),
            _Row(first_movie),
            _Row(show),
            _Row(later_movie),
            _Row(None),
        ]))
        window.go_root = True
        self.assertTrue(remotenav._open_section(window, {"type": "movie"}))
        self.assertIs(first_movie, window.clicks[0].dataSource)
        self.assertEqual([window.SECTION_LIST_ID], window.focused_control)
        self.assertFalse(window.go_root)

        photo = _Window(_Rows([_Row(_Section("photo"))]))
        photo.go_root = True
        self.assertFalse(remotenav._open_section(photo, {"type": "show"}))
        self.assertFalse(photo.clicks)
        self.assertTrue(photo.go_root)

    def test_title_opens_that_library_only(self):
        remotenav._HOOKS["open_section"] = None
        movies = _Section("movie", title="Movies")
        shows = _Section("show", title="TV Shows")
        fourk = _Section("movie", title="4K Movies")
        music = _Section("artist", title="Music")
        window = _Window(_Rows([
            _Row(_Section("home", title="Home")),
            _Row(_Section("playlists", title="Playlists")),
            _Row(_Section("movie", title="Movies", item_type="collection")),
            _Row(movies),
            _Row(shows),
            _Row(fourk),
            _Row(music),
        ]))
        remotenav._HOOKS["home"] = lambda: window

        titled = remotenav.accept_notification('{"to":"section","title":"  tv shows "}')
        self.assertEqual({"ok": True, "to": "section", "title": "tv shows"}, titled)
        self.assertIs(shows, window.clicks[-1].dataSource)
        self.assertTrue(ENV.logged("Remote nav: ok to=section title=tv shows"))

        both = remotenav.accept_notification('{"to":"section","type":"movie","title":"4K Movies"}')
        self.assertTrue(both["ok"])
        self.assertEqual("movie", both["type"])
        self.assertIs(fourk, window.clicks[-1].dataSource)

        music_hit = remotenav.accept_notification('{"to":"section","title":"MUSIC"}')
        self.assertTrue(music_hit["ok"])
        self.assertIs(music, window.clicks[-1].dataSource)

        missed = remotenav.accept_notification('{"to":"section","type":"movie","title":"TV Shows"}')
        self.assertEqual("no_section", missed["reason"])
        self.assertFalse(missed["ok"])
        self.assertEqual(3, len(window.clicks))
        self.assertTrue(ENV.logged("Remote nav: failed reason=no_section type=movie title=TV Shows"))

        absent = remotenav.accept_notification('{"to":"section","title":"Missing"}')
        self.assertEqual("no_section", absent["reason"])
        self.assertEqual(3, len(window.clicks))

        first = remotenav.accept_notification('{"to":"section","type":"movie"}')
        self.assertTrue(first["ok"])
        self.assertIs(movies, window.clicks[-1].dataSource)
        self.assertNotIn("title", first)

        blank = remotenav.accept_notification('{"to":"section","title":"   ","type":"show"}')
        self.assertTrue(blank["ok"])
        self.assertIs(shows, window.clicks[-1].dataSource)
        self.assertIsNone(remotenav.accept_notification('{"to":"section","title":"   "}'))

    def test_overlay_detection_uses_the_home_window_id(self):
        remotenav._HOOKS["overlays"] = None
        ENV.current_dialog_id = 9999
        ENV.current_window_id = 14000
        self.assertTrue(remotenav._overlays(self.home))
        ENV.current_window_id = self.home._winID
        self.assertFalse(remotenav._overlays(self.home))
        ENV.current_dialog_id = 12000
        self.assertTrue(remotenav._overlays(self.home))
        bare = _Window()
        bare._winID = None
        ENV.current_dialog_id = 9999
        ENV.current_window_id = 14000
        self.assertFalse(remotenav._overlays(bare))

    def test_wiring(self):
        kodigui = ast.parse(self._source("lib", "windows", "kodigui.py"), filename="lib/windows/kodigui.py")
        service = None
        for node in ast.walk(kodigui):
            if isinstance(node, ast.FunctionDef) and node.name == "_service_script_thread":
                service = node
        names = []
        for child in ast.walk(service):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                names.append(child.func.id)
        self.assertIn("_drain_remote_nav", names)

        home = ast.parse(self._source("lib", "windows", "home.py"), filename="lib/windows/home.py")
        reinit = None
        for node in ast.walk(home):
            if isinstance(node, ast.FunctionDef) and node.name == "onReInit":
                reinit = node
        called = []
        for child in ast.walk(reinit):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                called.append(child.func.attr)
        self.assertIn("_nudge_remote_nav", called)

        source = self._source("lib", "monitor.py")
        self.assertIn("PM4K_NAV", source)
        self.assertIn("remotenav", source)
        doc = self._source("docs", "remote-play.md")
        self.assertIn("PM4K_NAV", doc)
        self.assertIn('"to":"skip_marker"', doc)
        self.assertIn('"to":"auto_skip"', doc)
        self.assertIn("show.markerSkip_OSDOnly", doc)

    def _source(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()


class _Marker(object):
    def __init__(self, kind, end, final=False):
        self.type = kind
        self.endTimeOffset = end
        self.final = final

    def __str__(self):
        return "{0}@{1}".format(self.type, self.endTimeOffset)


class _Playback(object):
    """Stand-in playback screen. Skip uses SeekDialog's own methods."""

    def __init__(self):
        self._applyingSeek = False
        self._currentMarker = None
        self.props = {}
        self.sought = []
        self.hidden = []
        self.finals = []
        self.resets = []
        self.skip_calls = []
        self._introSkipShownStarted = "shown"
        self._creditsSkipShownStarted = "shown"

    def getProperty(self, key):
        return self.props.get(key, "")

    def setProperty(self, key, value):
        self.props[key] = value

    def getFocusId(self):
        raise AssertionError("skip must not ask who has focus")

    def doSeek(self, offset=None, settings_changed=False, skip_alt_seek_fix=False):
        self.sought.append(offset)

    def hideOSD(self, skipMarkerFocus=False, closing=False):
        self.hidden.append(skipMarkerFocus)

    def handleFinalMarker(self, marker_def, immediate=False, context="MarkerAutoSkip"):
        self.finals.append((immediate, context))
        return True

    def resetAutoSeekTimer(self, value="not_set"):
        self.resets.append(value)

    def markerSkipActive(self):
        return SeekDialog.markerSkipActive(self)

    def skipMarkerButton(self):
        self.skip_calls.append(1)
        return SeekDialog.skipMarkerButton(self)

    def show_marker(self, kind, end, final=False, visible=True, osd_only=False):
        self._currentMarker = {
            "marker": _Marker(kind, end, final=final),
            "marker_type": kind,
            "skipped": False,
            "markerAutoSkipShownTimer": "_creditsSkipShownStarted" if kind == "credits" else "_introSkipShownStarted",
        }
        self.props["show.markerSkip"] = "1" if visible else ""
        self.props["show.markerSkip_OSDOnly"] = "1" if osd_only else ""


class _ShowSettings(object):
    def __init__(self, intro=False, credits=False):
        self.auto_skip_intro = intro
        self.auto_skip_credits = credits


class MarkerSkipNavTest(KodiTestCase):
    def _source(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def setUp(self):
        super(MarkerSkipNavTest, self).setUp()
        remotenav._reset_for_tests()
        hubrefresh.reset()
        hubrefresh.note_script_thread()
        self.screen = _Playback()
        self.playing = False
        remotenav._HOOKS["playing"] = lambda: self.playing
        remotenav._HOOKS["playback"] = lambda: self.screen
        remotenav._HOOKS["poke"] = lambda: None
        remotenav._HOOKS["home"] = lambda: None

    def tearDown(self):
        remotenav._reset_for_tests()
        hubrefresh.reset()
        super(MarkerSkipNavTest, self).tearDown()

    def test_skip_marker_uses_the_skip_button_path_while_playing(self):
        self.playing = True
        self.screen.show_marker("intro", 90000)
        result = remotenav.accept_notification('{"to": "Skip_Marker"}')
        self.assertEqual({"ok": True, "to": "skip_marker", "type": "intro"}, result)
        self.assertEqual([1], self.screen.skip_calls)
        self.assertEqual([90000 + MARKER_END_JUMP_OFF], self.screen.sought)
        self.assertEqual([True], self.screen.hidden)
        self.assertFalse(self.screen.finals)
        self.assertFalse(self.screen.resets)
        self.assertTrue(self.screen._currentMarker["skipped"])
        self.assertEqual("", self.screen.props["show.markerSkip"])
        self.assertEqual("", self.screen.props["show.markerSkip_OSDOnly"])
        self.assertTrue(ENV.logged("Remote nav: ok to=skip_marker type=intro"))

    def test_osd_only_marker_still_skips(self):
        self.playing = True
        self.screen.show_marker("credits", 500000, visible=False, osd_only=True)
        result = remotenav.accept_notification({"to": "skip_marker"})
        self.assertEqual("credits", result["type"])
        self.assertNotIn("final", result)
        self.assertEqual([500000 + MARKER_END_JUMP_OFF], self.screen.sought)
        self.assertEqual([None], self.screen.resets)
        self.assertIsNone(self.screen._creditsSkipShownStarted)
        self.assertTrue(ENV.logged("Remote nav: ok to=skip_marker type=credits"))

    def test_visible_and_osd_only_together_still_skips(self):
        self.screen.show_marker("intro", 1000, visible=True, osd_only=True)
        result = remotenav.accept_notification('{"to":"skip_marker"}')
        self.assertTrue(result["ok"])
        self.assertEqual([1000 + MARKER_END_JUMP_OFF], self.screen.sought)

    def test_final_credits_follow_the_skip_button(self):
        self.playing = True
        self.screen.show_marker("credits", 1_200_000, final=True, osd_only=True, visible=False)
        result = remotenav.accept_notification('{"to":"skip_marker"}')
        self.assertEqual(
            {"ok": True, "to": "skip_marker", "type": "credits", "final": True},
            result,
        )
        self.assertEqual([(False, "MarkerSkip")], self.screen.finals)
        self.assertFalse(self.screen.sought)
        self.assertFalse(self.screen.hidden)
        self.assertFalse(self.screen._currentMarker["skipped"])
        self.assertEqual("1", self.screen.props["show.markerSkip_OSDOnly"])
        self.assertTrue(ENV.logged("Remote nav: ok to=skip_marker type=credits final=1"))

    def test_no_marker_does_nothing(self):
        self.playing = True
        self.assertIsNone(remotenav.accept_notification('{"to":"skip_marker"}'))
        self.screen._currentMarker = {"marker": _Marker("intro", 10), "marker_type": "intro", "skipped": False}
        self.assertIsNone(remotenav.accept_notification('{"to":"skip_marker"}'))
        self.screen.props["show.markerSkip"] = "1"
        self.screen._currentMarker["marker_type"] = "chapter"
        self.screen._currentMarker["marker"].type = "chapter"
        self.assertIsNone(remotenav.accept_notification('{"to":"skip_marker"}'))
        self.assertFalse(self.screen.skip_calls)
        self.assertFalse(self.screen.sought)
        self.assertFalse(self.screen.finals)
        self.assertTrue(ENV.logged("Remote nav: ignored reason=no_marker"))

    def test_missing_playback_screen_is_no_marker(self):
        remotenav._HOOKS["playback"] = lambda: None
        self.assertIsNone(remotenav.accept_notification('{"to":"skip_marker"}'))
        self.assertTrue(ENV.logged("Remote nav: ignored reason=no_marker"))

    def test_busy_seek_does_not_skip(self):
        self.screen.show_marker("intro", 40000)
        self.screen._applyingSeek = True
        self.assertIsNone(remotenav.accept_notification('{"to":"skip_marker"}'))
        self.assertEqual([1], self.screen.skip_calls)
        self.assertFalse(self.screen.sought)
        self.assertTrue(ENV.logged("Remote nav: ignored reason=busy"))

    def test_skip_errors_do_not_escape(self):
        def boom():
            raise RuntimeError("marker")

        self.screen.markerSkipActive = boom
        self.assertIsNone(remotenav.accept_notification('{"to":"skip_marker"}'))
        self.assertTrue(ENV.logged("Remote nav: failed reason=error"))

        self.screen.markerSkipActive = lambda: True
        self.screen.skipMarkerButton = boom
        self.assertIsNone(remotenav.accept_notification('{"to":"skip_marker"}'))

    def test_skip_waits_for_the_script_thread(self):
        self.screen.show_marker("intro", 25000)
        remotenav._HOOKS["on_gui"] = lambda: False
        remotenav.accept_notification('{"to":"skip_marker"}')
        self.assertFalse(self.screen.skip_calls)
        remotenav._HOOKS["on_gui"] = None
        remotenav.drain()
        self.assertEqual([1], self.screen.skip_calls)
        self.assertEqual([25000 + MARKER_END_JUMP_OFF], self.screen.sought)

    def test_home_is_still_ignored_while_playing(self):
        self.playing = True
        self.screen.show_marker("intro", 1000)
        focused = []
        remotenav._HOOKS["focus_home"] = lambda home: focused.append(home)
        self.assertIsNone(remotenav.accept_notification('{"to":"home"}'))
        self.assertFalse(focused)
        self.assertFalse(self.screen.sought)
        self.assertTrue(ENV.logged("Remote nav: ignored reason=playing"))
        skipped = remotenav.accept_notification('{"to":"skip_marker"}')
        self.assertEqual("intro", skipped["type"])

    def test_monitor_dispatches_skip_marker(self):
        self.screen.show_marker("intro", 10)
        monitor.MONITOR.onNotification("script.plexmod", "Other.PM4K_NAV", '{"to":"skip_marker"}')
        self.assertEqual([1], self.screen.skip_calls)
        self.screen.skip_calls[:] = []
        monitor.MONITOR.onNotification("xbmc", "Other.PM4K_NAV", '{"to":"skip_marker"}')
        self.assertFalse(self.screen.skip_calls)

    def test_playback_dialog_comes_from_the_player(self):
        import sys
        import types

        import lib

        remotenav._HOOKS["playback"] = None
        sentinel = object()
        fake = types.ModuleType("lib.player")
        fake.PLAYER = type("Player", (), {"handler": type("Handler", (), {"dialog": sentinel})()})()
        previous_mod = sys.modules.get("lib.player")
        previous_attr = getattr(lib, "player", None)
        sys.modules["lib.player"] = fake
        lib.player = fake
        try:
            self.assertIs(sentinel, remotenav._playback_dialog())
            fake.PLAYER.handler = type("Handler", (), {})()
            self.assertIsNone(remotenav._playback_dialog())
            fake.PLAYER.handler = None
            self.assertIsNone(remotenav._playback_dialog())
        finally:
            if previous_mod is None:
                sys.modules.pop("lib.player", None)
            else:
                sys.modules["lib.player"] = previous_mod
            if previous_attr is None:
                if hasattr(lib, "player"):
                    delattr(lib, "player")
            else:
                lib.player = previous_attr

    def test_skip_button_handler_calls_the_shared_method(self):
        tree = ast.parse(self._source("lib", "windows", "seekdialog.py"), filename="lib/windows/seekdialog.py")
        seek = None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "SeekDialog":
                seek = node
        on_action = None
        skip = None
        for node in seek.body:
            if isinstance(node, ast.FunctionDef) and node.name == "onAction":
                on_action = node
            elif isinstance(node, ast.FunctionDef) and node.name == "skipMarkerButton":
                skip = node
        button_calls = [
            child for child in ast.walk(on_action)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            and child.func.attr == "skipMarkerButton"
        ]
        self.assertEqual(1, len(button_calls))
        select_body = None
        for child in ast.walk(on_action):
            if not isinstance(child, ast.If) or not child.body:
                continue
            first = child.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                    and isinstance(first.value.func, ast.Attribute)
                    and first.value.func.attr == "skipMarkerButton"):
                select_body = child.body
        self.assertIsNotNone(select_body)
        self.assertIsInstance(select_body[1], ast.Return)
        direct_final = [
            child for child in ast.walk(on_action)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            and child.func.attr == "handleFinalMarker"
        ]
        self.assertFalse(direct_final)
        called = [
            child.func.attr for child in ast.walk(skip)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        ]
        self.assertIn("handleFinalMarker", called)
        self.assertIn("doSeek", called)
        self.assertIn("hideOSD", called)


class SessionAutoSkipTest(KodiTestCase):
    def _source(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def setUp(self):
        super(SessionAutoSkipTest, self).setUp()
        remotenav._reset_for_tests()
        hubrefresh.reset()
        hubrefresh.note_script_thread()
        remotenav._HOOKS["poke"] = lambda: None
        self.saved = []
        self._set_setting = None

    def tearDown(self):
        if self._set_setting is not None:
            from lib.windows import seekdialog
            seekdialog.util.setSetting = self._set_setting
        remotenav._reset_for_tests()
        hubrefresh.reset()
        super(SessionAutoSkipTest, self).tearDown()

    def _watch_settings(self):
        from lib.windows import seekdialog
        self._set_setting = seekdialog.util.setSetting

        def remember(key, value, addon=None):
            self.saved.append((key, value))
            return self._set_setting(key, value, addon)

        seekdialog.util.setSetting = remember

    def _episode(self, direct=True, binge=False):
        screen = type("Screen", (), {})()
        screen.isDirectPlay = direct
        screen.bingeMode = binge
        screen.autoSkipIntro = False
        screen.autoSkipCredits = False
        return screen

    def test_auto_skip_turns_on_for_later_episodes_only_in_memory(self):
        self._watch_settings()
        screen = self._episode()
        settings = _ShowSettings()
        SeekDialog._applyMarkerAutoSkip(screen, settings, False)
        self.assertFalse(screen.autoSkipIntro)
        self.assertFalse(screen.autoSkipCredits)

        enabled = remotenav.accept_notification('{"to":"auto_skip"}')
        self.assertEqual({"ok": True, "to": "auto_skip", "on": True}, enabled)
        self.assertTrue(remotenav.session_auto_skip_enabled())
        self.assertTrue(ENV.logged("Remote nav: ok to=auto_skip on=1"))
        # The screen already playing keeps the choice it started with.
        self.assertFalse(screen.autoSkipIntro)

        later = self._episode()
        SeekDialog._applyMarkerAutoSkip(later, settings, False)
        self.assertTrue(later.autoSkipIntro)
        self.assertTrue(later.autoSkipCredits)
        self.assertFalse(settings.auto_skip_intro)
        self.assertFalse(settings.auto_skip_credits)
        self.assertFalse(self.saved)

        again = remotenav.accept_notification('{"to":"auto_skip","on":true}')
        self.assertTrue(again["on"])
        disabled = remotenav.accept_notification('{"to":"auto_skip","on":false}')
        self.assertEqual({"ok": True, "to": "auto_skip", "on": False}, disabled)
        self.assertFalse(remotenav.session_auto_skip_enabled())
        self.assertTrue(ENV.logged("Remote nav: ok to=auto_skip on=0"))
        after = self._episode()
        SeekDialog._applyMarkerAutoSkip(after, _ShowSettings(intro=True), False)
        self.assertTrue(after.autoSkipIntro)
        self.assertFalse(after.autoSkipCredits)

    def test_session_flag_overrides_the_first_episode_exception(self):
        screen = self._episode(binge=True)
        SeekDialog._applyMarkerAutoSkip(screen, _ShowSettings(), True)
        self.assertFalse(screen.autoSkipIntro)
        self.assertTrue(screen.autoSkipCredits)

        remotenav.accept_notification('{"to":"auto_skip"}')
        later = self._episode(binge=True)
        SeekDialog._applyMarkerAutoSkip(later, _ShowSettings(), True)
        self.assertTrue(later.autoSkipIntro)
        self.assertTrue(later.autoSkipCredits)

    def test_transcode_still_obeys_the_existing_gate(self):
        from lib.windows import seekdialog
        original = seekdialog.util.getUserSetting

        def setting(key, default=None):
            if key == "auto_skip_in_transcode":
                return False
            return default

        seekdialog.util.getUserSetting = setting
        try:
            remotenav.accept_notification('{"to":"auto_skip"}')
            screen = self._episode(direct=False)
            SeekDialog._applyMarkerAutoSkip(screen, _ShowSettings(intro=True, credits=True), False)
            self.assertFalse(screen.autoSkipIntro)
            self.assertFalse(screen.autoSkipCredits)
        finally:
            seekdialog.util.getUserSetting = original

    def test_bad_auto_skip_value_is_ignored(self):
        self.assertIsNone(remotenav.accept_notification('{"to":"auto_skip","on":"maybe"}'))
        self.assertFalse(remotenav.session_auto_skip_enabled())
        self.assertTrue(ENV.logged("Remote nav: ignored reason=bad_request"))
        remotenav.accept_notification('{"to":"auto_skip","on":0}')
        self.assertFalse(remotenav.session_auto_skip_enabled())
        remotenav.accept_notification('{"to":"auto_skip","on":"off"}')
        self.assertFalse(remotenav.session_auto_skip_enabled())
        remotenav.accept_notification('{"to":"auto_skip","on":1}')
        self.assertTrue(remotenav.session_auto_skip_enabled())
        remotenav.accept_notification('{"to":"auto_skip","on":"no"}')
        self.assertFalse(remotenav.session_auto_skip_enabled())

    def test_auto_skip_waits_for_the_script_thread(self):
        remotenav._HOOKS["on_gui"] = lambda: False
        remotenav.accept_notification('{"to":"auto_skip"}')
        self.assertFalse(remotenav.session_auto_skip_enabled())
        remotenav._HOOKS["on_gui"] = None
        remotenav.drain()
        self.assertTrue(remotenav.session_auto_skip_enabled())

    def test_setup_applies_marker_auto_skip_for_episodes(self):
        tree = ast.parse(self._source("lib", "windows", "seekdialog.py"), filename="lib/windows/seekdialog.py")
        setup = None
        apply = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "setup":
                setup = node
            elif node.name == "_applyMarkerAutoSkip":
                apply = node
        called = [
            child.func.attr for child in ast.walk(setup)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        ]
        self.assertIn("_applyMarkerAutoSkip", called)
        attrs = [child.attr for child in ast.walk(apply) if isinstance(child, ast.Attribute)]
        self.assertIn("session_auto_skip_enabled", attrs)
        self.assertNotIn("setSetting", attrs)
        self.assertNotIn("save", attrs)
