# coding=utf-8
"""External navigation stays off the GUI thread until the script thread."""
from __future__ import absolute_import

import ast
import os
import threading

import xbmc
from kodienv import ENV

from lib import hubrefresh, monitor, remotenav

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
        self.assertIn("PM4K_NAV", self._source("docs", "remote-play.md"))

    def _source(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()
