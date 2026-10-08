# coding=utf-8
"""External play-by-ratingKey stays off the GUI thread until the script thread."""
from __future__ import absolute_import

import ast
import json
import os
import threading
import traceback

import xbmcgui
from kodienv import ENV

from lib import hubrefresh, remoteplay

from .base import KodiTestCase, REPO_ROOT

_FORBIDDEN = frozenset((
    "setArt", "setThumbnailImage", "setLabel", "setLabel2", "addItem", "addItems", "getControl",
))


class _Offset(object):
    def __init__(self, value):
        self.value = value

    def asInt(self):
        return self.value


class _Item(object):
    def __init__(self, kind, key, on_deck=None, episodes=None, offset=0, watched=False, available=True,
                 title=""):
        self.TYPE = kind
        self.type = kind
        self.ratingKey = key
        self.onDeck = on_deck or []
        self._episodes = episodes
        self.viewOffset = _Offset(offset)
        self.isWatched = watched
        self._available = available
        self.title = title
        self.reloaded = None

    def reload(self, **kwargs):
        self.reloaded = kwargs
        return self

    def episodes(self, watched=None):
        found = list(self._episodes or [])
        if watched is False:
            return [episode for episode in found if not episode.isWatched]
        return found

    def available(self):
        return self._available


class _Server(object):
    def __init__(self, uuid, name):
        self.uuid = uuid
        self.name = name


class _Manager(object):
    def __init__(self, servers, selected):
        self._servers = list(servers)
        self.selectedServer = selected

    def getServers(self):
        return list(self._servers)


class RemotePlayTest(KodiTestCase):
    def setUp(self):
        super(RemotePlayTest, self).setUp()
        remoteplay._reset_for_tests()
        # These cases call submit on the pytest thread. That thread is the
        # event loop for them. A background request still waits for drain.
        hubrefresh.reset()
        hubrefresh.note_script_thread()
        self.opened = []
        self.stopped = []
        self.woken = []
        self.poked = []
        self.playing = False
        remoteplay._HOOKS["ready"] = lambda: True
        remoteplay._HOOKS["wake"] = lambda: self.woken.append(1)
        remoteplay._HOOKS["dismiss"] = lambda: None
        remoteplay._HOOKS["poke"] = lambda: self.poked.append(1)
        remoteplay._HOOKS["playing"] = lambda: self.playing
        remoteplay._HOOKS["stop"] = self._stop
        remoteplay._HOOKS["sleep"] = lambda: True
        remoteplay._HOOKS["open"] = lambda item, request: self.opened.append((item, request))
        self.server = _Server("server-1", "Library")
        self.other = _Server("server-2", "Other")
        remoteplay._HOOKS["manager"] = lambda: _Manager([self.server, self.other], self.server)
        self.fetched = {}
        remoteplay._HOOKS["fetch"] = self._fetch

    def tearDown(self):
        remoteplay._reset_for_tests()
        hubrefresh.reset()
        super(RemotePlayTest, self).tearDown()

    def _stop(self):
        self.stopped.append(1)
        self.playing = False

    def _fetch(self, server, rating_key):
        self.fetched["server"] = server
        return self.fetched.get("item")

    def _play(self, kind="episode", key="12345", **kwargs):
        self.fetched["item"] = _Item(kind, key, **kwargs)
        return remoteplay.submit(remoteplay.normalize({"ratingKey": key, "id": "req1"}))

    def _result(self):
        raw = xbmcgui.Window(10000).getProperty("script.plex.remoteplay")
        return json.loads(raw) if raw else None

    def test_episode_uses_selected_server_and_resumes_without_a_prompt(self):
        result = self._play()
        self.assertTrue(result["ok"])
        self.assertEqual("episode", result["type"])
        self.assertEqual("12345", result["ratingKey"])
        self.assertIs(self.fetched["server"], self.server)
        self.assertEqual("resume", self.opened[0][1]["resume"])
        self.assertEqual({"auto_play": True, "force_resume": True}, remoteplay.playback_kwargs(self.opened[0][1]))
        self.assertTrue(self.woken)
        self.assertEqual("ok", self._result()["state"])
        self.assertTrue(ENV.logged("Remote play: ok"))
        self.assertTrue(any(call[0] == "notification" for call in ENV.dialog_calls))

    def test_start_flag_and_named_server(self):
        self.fetched["item"] = _Item("movie", "55")
        request = remoteplay.normalize({
            "ratingKey": "55",
            "server": "other",
            "resume": 0,
            "force": False,
            "id": "req2",
        })
        result = remoteplay.submit(request)
        self.assertTrue(result["ok"])
        self.assertEqual("movie", result["type"])
        self.assertIs(self.fetched["server"], self.other)
        self.assertEqual({"auto_play": True, "start_over": True}, remoteplay.playback_kwargs(request))

    def test_ask_leaves_the_prompt_to_the_play_button(self):
        request = remoteplay.normalize({"ratingKey": "55", "resume": "ask", "id": "req3"})
        self.assertEqual({"auto_play": True}, remoteplay.playback_kwargs(request))

    def test_show_prefers_on_deck_then_unwatched(self):
        deck = _Item("episode", "77")
        self.fetched["item"] = _Item("show", "10", on_deck=[deck], title="SENTINEL")
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "10", "id": "show1"}))
        self.assertEqual("77", result["ratingKey"])
        self.assertEqual("10", result["requested"])
        self.assertEqual({"includeOnDeck": 1}, self.fetched["item"].reloaded)
        self.assertFalse(ENV.logged("SENTINEL"))

        later = _Item("episode", "88", watched=False)
        watched = _Item("episode", "66", watched=True)
        self.fetched["item"] = _Item("show", "10", on_deck=[], episodes=[watched, later])
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "10", "id": "show2"}))
        self.assertEqual("88", result["ratingKey"])

        self.fetched["item"] = _Item("show", "10", on_deck=[], episodes=[watched])
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "10", "id": "show3"}))
        self.assertFalse(result["ok"])
        self.assertEqual("nothing_to_play", result["reason"])

    def test_season_prefers_in_progress_then_unwatched(self):
        progress = _Item("episode", "31", offset=1000, watched=False)
        fresh = _Item("episode", "32", watched=False)
        done = _Item("episode", "30", watched=True)
        self.fetched["item"] = _Item("season", "3", episodes=[done, fresh, progress])
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "3", "id": "sea1"}))
        self.assertEqual("31", self.opened[-1][0].ratingKey)
        self.assertTrue(result["ok"])

        self.opened[:] = []
        self.fetched["item"] = _Item("season", "3", episodes=[done, fresh])
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "3", "id": "sea2"}))
        self.assertEqual("32", result["ratingKey"])

    def test_does_not_interrupt_playback_unless_forced(self):
        self.playing = True
        self.fetched["item"] = _Item("movie", "55")
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "id": "busy1"}))
        self.assertEqual("playing", result["reason"])
        self.assertFalse(self.opened)
        self.assertFalse(self.stopped)
        self.assertEqual("error", self._result()["state"])

        self.playing = True
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "force": True, "id": "busy2"}))
        self.assertTrue(result["ok"])
        self.assertTrue(self.stopped)
        self.assertTrue(self.opened)

    def test_force_reports_playing_when_stop_does_not_finish(self):
        self.playing = True
        remoteplay._HOOKS["stop"] = lambda: self.stopped.append(1)
        self.fetched["item"] = _Item("movie", "55")
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "force": "1", "id": "busy3"}))
        self.assertEqual("playing", result["reason"])
        self.assertFalse(self.opened)

    def test_missing_server_ambiguous_server_and_unsupported(self):
        remoteplay._HOOKS["manager"] = lambda: _Manager([], None)
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "id": "s1"}))
        self.assertEqual("no_server", result["reason"])

        twin = _Server("server-9", "Library")
        remoteplay._HOOKS["manager"] = lambda: _Manager([self.server, twin], self.server)
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "server": "library", "id": "s2"}))
        self.assertEqual("ambiguous_server", result["reason"])

        remoteplay._HOOKS["manager"] = lambda: _Manager([self.server], self.server)
        self.fetched["item"] = _Item("artist", "55")
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "id": "s3"}))
        self.assertEqual("unsupported", result["reason"])

        self.fetched["item"] = None
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "id": "s4"}))
        self.assertEqual("not_found", result["reason"])

        self.fetched["item"] = _Item("movie", "55", available=False)
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "id": "s5"}))
        self.assertEqual("unavailable", result["reason"])
        self.assertFalse(self.opened)

    def test_not_ready_and_bad_rating_key(self):
        remoteplay._HOOKS["ready"] = lambda: False
        self.fetched["item"] = _Item("movie", "55")
        result = remoteplay.submit(remoteplay.normalize({"ratingKey": "55", "id": "n1"}))
        self.assertEqual("not_ready", result["reason"])
        self.assertIsNone(remoteplay.normalize({"ratingKey": "../55"}))
        self.assertIsNone(remoteplay.normalize({"key": "abc"}))
        self.assertEqual("99", remoteplay.normalize({"key": "/library/metadata/99"})["ratingKey"])

    def test_background_thread_does_not_open_until_drain(self):
        self.fetched["item"] = _Item("episode", "12345")
        seen = []
        remoteplay._HOOKS["open"] = lambda item, request: seen.append(threading.current_thread())
        errors = []

        def worker():
            try:
                remoteplay.accept_notification('{"ratingKey":"12345","id":"bg1"}')
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.assertFalse(seen)
        self.assertTrue(self.poked)
        remoteplay.drain()
        self.assertEqual([threading.current_thread()], seen)
        self.assertTrue(self._result()["ok"])

    def test_non_main_script_thread_drains_on_the_next_action(self):
        errors = []

        def runner():
            try:
                self._drain_on_script_thread()
            except Exception:
                errors.append(traceback.format_exc())

        worker = threading.Thread(target=runner)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "script runner did not finish")
        self.assertFalse(errors, errors)

    def _drain_on_script_thread(self):
        hubrefresh.reset()
        remoteplay._reset_for_tests()
        self._install_hooks()
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        self.assertFalse(hubrefresh.on_script_thread())
        self.fetched["item"] = _Item("episode", "12345")
        seen = []
        remoteplay._HOOKS["open"] = lambda item, request: seen.append(threading.current_thread())
        worker_errors = []

        def pool():
            try:
                remoteplay.accept_notification('{"ratingKey":"12345","id":"bg-loop"}')
                if hubrefresh.on_script_thread():
                    worker_errors.append("worker claimed the event loop")
            except Exception:
                worker_errors.append(traceback.format_exc())

        pool_thread = threading.Thread(target=pool)
        pool_thread.start()
        pool_thread.join(2)
        self.assertFalse(pool_thread.is_alive())
        self.assertFalse(worker_errors, worker_errors)
        self.assertFalse(seen)
        self.assertFalse(hubrefresh.on_script_thread())
        self.assertTrue(self.poked)
        # Calling drain off the recorded thread must not open playback.
        remoteplay.drain()
        self.assertFalse(seen)

        from lib.windows import kodigui
        window = type("Win", (), {"_art_token": None})()
        kodigui._service_script_thread(window)
        self.assertTrue(hubrefresh.on_script_thread())
        self.assertEqual([threading.current_thread()], seen)
        self.assertIsNot(seen[0], threading.main_thread())
        self.assertTrue(self._result()["ok"])

    def _install_hooks(self):
        remoteplay._HOOKS["ready"] = lambda: True
        remoteplay._HOOKS["wake"] = lambda: self.woken.append(1)
        remoteplay._HOOKS["dismiss"] = lambda: None
        remoteplay._HOOKS["poke"] = lambda: self.poked.append(1)
        remoteplay._HOOKS["playing"] = lambda: self.playing
        remoteplay._HOOKS["stop"] = self._stop
        remoteplay._HOOKS["sleep"] = lambda: True
        remoteplay._HOOKS["open"] = lambda item, request: self.opened.append((item, request))
        remoteplay._HOOKS["manager"] = lambda: _Manager([self.server, self.other], self.server)
        remoteplay._HOOKS["fetch"] = self._fetch

    def test_window_action_records_the_event_loop_before_gui_work(self):
        source = self._source("lib", "windows", "kodigui.py")
        tree = ast.parse(source, filename="lib/windows/kodigui.py")
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_service_script_thread":
                found["_service_script_thread"] = node
            if isinstance(node, ast.ClassDef) and node.name in ("BaseWindow", "BaseDialog"):
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "onAction":
                        found["{0}.onAction".format(node.name)] = child

        def calls(node):
            names = []
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    names.append(child.func.attr)
                elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    names.append(child.func.id)
            return names

        service = calls(found["_service_script_thread"])
        self.assertIn("note_script_thread", service)
        self.assertIn("_flush_art_prefetch", service)
        self.assertIn("_drain_remote_play", service)
        for name in ("BaseWindow.onAction", "BaseDialog.onAction"):
            self.assertIn("_service_script_thread", calls(found[name]), name)
        home = ast.parse(self._source("lib", "windows", "home.py"), filename="lib/windows/home.py")
        home_actions = []
        for node in ast.walk(home):
            if isinstance(node, ast.ClassDef) and node.name == "HomeWindow":
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name in ("onAction", "onFocus", "onClick"):
                        home_actions.append(calls(child))
        self.assertEqual(len(home_actions), 3)
        for names in home_actions:
            self.assertIn("_service_script_thread", names)

    def test_handoff_forwards_when_running_and_reports_when_not(self):
        remoteplay._HOOKS["running"] = lambda: True
        result = remoteplay.handoff_argv(["addon", "play", "12345", "server-1", "0", "1"])
        self.assertEqual("pending", result["state"])
        self.assertFalse(self.opened)
        builtin = ENV.builtins[-1]
        self.assertIn("NotifyAll(script.plexmod,PM4K_PLAY,", builtin)
        marker = 'PM4K_PLAY,"'
        inner = builtin[builtin.find(marker) + len(marker):builtin.rfind('")')]
        loaded = json.loads(inner.replace('\\"', '"'))
        self.assertEqual("12345", loaded["ratingKey"])
        self.assertEqual("start", loaded["resume"])
        self.assertTrue(loaded["force"])
        self.assertEqual("server-1", loaded["server"])

        remoteplay._HOOKS["running"] = lambda: False
        result = remoteplay.handoff_argv(["addon", "play", "12345"])
        self.assertEqual("not_running", result["reason"])
        self.assertEqual("error", self._result()["state"])
        self.assertTrue(ENV.logged("reason=not_running"))

    def test_notification_json_and_argv_shapes(self):
        self.fetched["item"] = _Item("movie", "55")
        result = remoteplay.accept_notification('{"ratingKey":"55","resume":false,"server":"server-2"}')
        self.assertTrue(result["ok"])
        self.assertIs(self.fetched["server"], self.other)
        self.assertEqual("start", self.opened[-1][1]["resume"])

        parsed = remoteplay.parse_argv(["addon", "play", "55"])
        self.assertEqual("55", parsed["ratingKey"])
        self.assertEqual("resume", parsed["resume"])
        parsed = remoteplay.parse_argv(["addon", '{"ratingKey":"55","resume":"ask"}'])
        self.assertEqual("ask", parsed["resume"])
        parsed = remoteplay.parse_argv(["addon", "ratingKey=55", "force=1"])
        self.assertEqual("55", parsed["ratingKey"])
        self.assertTrue(parsed["force"])
        self.assertIsNone(remoteplay.parse_argv(["addon", "fromplugin"]))
        self.assertTrue(remoteplay.argv_is_play(["addon", "play", "55"]))
        self.assertFalse(remoteplay.argv_is_play(["addon", "1", "0"]))

    def _source(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_module_does_not_touch_list_items(self):
        source = self._source("lib", "remoteplay.py")
        tree = ast.parse(source, filename="lib/remoteplay.py")
        found = [node.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN]
        self.assertEqual([], found)
        self.assertNotIn("ListItem", source)
        self.assertIn("forceResume", self._source("lib", "windows", "episodes.py"))
        self.assertIn("forceResume", self._source("lib", "windows", "preplay.py"))

    def test_player_windows_apply_force_resume_before_the_prompt(self):
        # The flag is consumed by the play button, not by a parallel player.
        episodes = self._source("lib", "windows", "episodes.py")
        preplay = self._source("lib", "windows", "preplay.py")
        self.assertIn("if force_resume and not force_resume_menu:", episodes)
        self.assertIn("if force_resume and not force_resume_menu:", preplay)
