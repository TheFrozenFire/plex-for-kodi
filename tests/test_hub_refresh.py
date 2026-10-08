# coding=utf-8
"""Continue Watching reloads after the stopped timeline, and a moved row waits."""
from __future__ import absolute_import

import ast
import os
import threading
import time
import traceback
import unittest

from lib import hubrefresh


_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_HOME = os.path.join(_REPO, "lib", "windows", "home.py")

# These run on a pool worker. They may store a hub and poke the script thread.
# They must not touch a list item or a control.
_WORKER_METHODS = (
    "ContinueRefreshTask.run",
    "RemainingHubTask.run",
    "HomeWindow._on_continue_reloaded",
    "HomeWindow._on_rest_hub",
    "HomeWindow._queue_rest_draw",
)
_GUI_ATTRS = frozenset((
    "setArt", "setThumbnailImage", "setProperty", "setLabel", "setLabel2",
    "addItem", "addItems", "getControl", "getFocusId", "selectItem", "replaceItems",
))


def _methods(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef):
                found["{0}.{1}".format(node.name, child.name)] = child
    return found


def _gui_calls(node):
    hits = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in _GUI_ATTRS:
            hits.append(child.attr)
        if isinstance(child, ast.Name) and child.id == "xbmcgui":
            hits.append("xbmcgui")
    return hits


def _call_names(node):
    names = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.append(func.attr)
        elif isinstance(func, ast.Name):
            names.append(func.id)
    return names


# How long a queued paint may sit after Home is the active window before
# the harness treats the handoff as stuck. The loop returns as soon as
# the rows are applied.
_APPLY_WAIT_S = 2.0


class HubRefreshDecisionTest(unittest.TestCase):
    def setUp(self):
        hubrefresh.reset()

    def tearDown(self):
        hubrefresh.reset()

    def test_continue_identifiers(self):
        for ident in ("continueWatching", "home.continue", "home.ondeck",
                      "tv.inprogress", "movie.ondeck", "watchlist.continueWatching"):
            self.assertTrue(hubrefresh.is_continue_hub(ident), ident)
        self.assertFalse(hubrefresh.is_continue_hub("movie.recentlyadded"))
        self.assertFalse(hubrefresh.is_continue_hub(""))
        self.assertFalse(hubrefresh.is_continue_hub(None))

    def test_hold_only_a_focused_row_the_user_has_moved_on(self):
        # Same items: apply, including a progress-only change of the same key.
        self.assertEqual("apply", hubrefresh.decide(False, True, True))
        # Focused, but they have not moved since home returned: apply in place.
        self.assertEqual("apply", hubrefresh.decide(True, True, False))
        # They moved, but this is not the focused row.
        self.assertEqual("apply", hubrefresh.decide(True, False, True))
        # Focused row changed after they moved: hold it.
        self.assertEqual("defer", hubrefresh.decide(True, True, True))

    def test_order_is_the_rating_key_sequence(self):
        self.assertFalse(hubrefresh.order_changed(("10", "11"), ("10", "11")))
        self.assertTrue(hubrefresh.order_changed(("10", "11"), ("11", "10")))
        self.assertTrue(hubrefresh.order_changed(("10",), ()))

    def test_stopped_timeline_ack_gates_the_continue_read(self):
        token = hubrefresh.note_stop_sent()
        self.assertFalse(hubrefresh.note_session_end(now=0))
        self.assertFalse(hubrefresh.note_stop_acked(token - 1, now=10))
        self.assertFalse(hubrefresh.rest_due(now=12))
        self.assertTrue(hubrefresh.note_stop_acked(token, now=10))
        self.assertFalse(hubrefresh.note_stop_acked(token, now=10))
        self.assertFalse(hubrefresh.rest_due(now=11))
        self.assertTrue(hubrefresh.rest_due(now=10 + hubrefresh.REST_DELAY))
        self.assertFalse(hubrefresh.rest_due(now=100))

    def test_session_end_starts_immediately_when_no_timeline_was_sent(self):
        self.assertTrue(hubrefresh.note_session_end(now=5))
        self.assertFalse(hubrefresh.note_session_end(now=5))
        self.assertFalse(hubrefresh.rest_due(now=6))
        self.assertTrue(hubrefresh.rest_due(now=5 + hubrefresh.REST_DELAY))

    def test_a_newer_stop_invalidates_the_previous_ack(self):
        first = hubrefresh.note_stop_sent()
        second = hubrefresh.note_stop_sent()
        self.assertNotEqual(first, second)
        self.assertFalse(hubrefresh.note_stop_acked(first, now=1))
        self.assertFalse(hubrefresh.same_generation(first))
        self.assertTrue(hubrefresh.note_stop_acked(second, now=1))
        self.assertTrue(hubrefresh.same_generation(second))

    def test_worker_methods_do_not_touch_list_items(self):
        methods = _methods(_HOME)
        for name in _WORKER_METHODS:
            self.assertIn(name, methods, name)
            hits = _gui_calls(methods[name])
            self.assertFalse(hits, "{0} calls {1}".format(name, hits))

    def test_fast_path_covers_on_deck_and_continue(self):
        displayed = (
            "home.ondeck",
            "home.continue",
            "continueWatching",
            "movie.recentlyadded",
        )
        fast = [ident for ident in displayed if hubrefresh.is_continue_hub(ident)]
        self.assertEqual(fast, ["home.ondeck", "home.continue", "continueWatching"])

    def test_short_watch_still_starts_a_refresh(self):
        # A finished stop leaves the one-shot gate set. There is no minimum
        # watch length: the next session end refreshes at the same instant.
        token = hubrefresh.note_stop_sent()
        self.assertTrue(hubrefresh.note_stop_acked(token, now=0))
        self.assertEqual("already-started", hubrefresh.refresh_block())
        self.assertFalse(hubrefresh.note_session_end(now=0))
        hubrefresh.note_playback_started()
        self.assertIsNone(hubrefresh.refresh_block())
        self.assertTrue(hubrefresh.note_session_end(now=0))
        self.assertTrue(hubrefresh.rest_due(now=hubrefresh.REST_DELAY))

    def test_two_consecutive_play_stop_cycles(self):
        first = hubrefresh.note_stop_sent()
        self.assertTrue(hubrefresh.note_stop_acked(first, now=0))
        hubrefresh.note_playback_started()
        second = hubrefresh.note_stop_sent()
        self.assertNotEqual(first, second)
        self.assertFalse(hubrefresh.note_stop_acked(first, now=1))
        self.assertTrue(hubrefresh.note_stop_acked(second, now=1))
        # The second stop sent no timeline. Session end still refreshes,
        # and it retires the previous generation.
        previous = hubrefresh.current_generation()
        hubrefresh.note_playback_started()
        self.assertTrue(hubrefresh.note_session_end(now=2))
        self.assertNotEqual(previous, hubrefresh.current_generation())
        self.assertFalse(hubrefresh.same_generation(previous))
        self.assertFalse(hubrefresh.note_session_end(now=2))

    def test_script_thread_is_the_event_loop_not_python_main(self):
        # Before any window callback, even Python's main thread is not
        # the add-on script thread.
        self.assertFalse(hubrefresh.on_script_thread())
        seen = {}

        def runner():
            try:
                self.assertIsNot(threading.current_thread(), threading.main_thread())
                self.assertFalse(hubrefresh.on_script_thread())
                hubrefresh.note_script_thread()
                seen["on_runner"] = hubrefresh.on_script_thread()
                seen["ident"] = threading.get_ident()
            except Exception:
                seen["error"] = traceback.format_exc()

        worker = threading.Thread(target=runner)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertTrue(seen["on_runner"])
        self.assertNotEqual(seen["ident"], threading.get_ident())
        # The runner's ident must not leak onto this thread.
        self.assertFalse(hubrefresh.on_script_thread())
        hubrefresh.reset()
        self.assertFalse(hubrefresh.on_script_thread())

    def test_event_loop_records_the_script_thread_and_cron_does_not(self):
        methods = _methods(_HOME)
        for name in ("HomeWindow.onFirstInit", "HomeWindow.show", "HomeWindow.onReInit",
                     "HomeWindow.onAction", "HomeWindow.onFocus", "HomeWindow.onClick"):
            self.assertIn("_note_script_thread", _call_names(methods[name]), name)
        for name in ("HomeWindow.tick", "HomeWindow._poke_if_home_is_visible",
                     "HomeWindow._on_continue_reloaded", "HomeWindow._on_rest_hub",
                     "HomeWindow._drain_hub_refresh"):
            calls = _call_names(methods[name])
            self.assertNotIn("_note_script_thread", calls, name)
            self.assertNotIn("note_script_thread", calls, name)


class _Item(object):
    def __init__(self, key):
        self.ratingKey = key

    def getProperty(self, name):
        return ""


class _Control(object):
    def __init__(self, source, keys):
        self.dataSource = source
        self._rows = [_Item(key) for key in keys]

    def __iter__(self):
        return iter(self._rows)


class _Hub(object):
    def __init__(self, ident, keys):
        self.ident = ident
        self.hubIdentifier = ident
        self._identifier = ident
        self.items = [_Item(key) for key in keys]

    def getCleanHubIdentifier(self, is_home=False):
        return self.ident


def _load_home():
    from kodienv import ENV
    ENV.abort_requested = True
    from lib.windows import home
    return home


class HomeApplyWindowTest(unittest.TestCase):
    """Paint waits until Home is the open window, including On Deck."""

    def setUp(self):
        hubrefresh.reset()
        home = _load_home()
        from kodienv import ENV
        self.env = ENV
        win = home.HomeWindow.__new__(home.HomeWindow)
        win._shuttingDown = False
        win._closing = False
        win._ignoreReInit = False
        win._goRootHoldUntil = 0
        win._drainingHubs = False
        win._navSinceReturn = False
        win._suspendHubNav = False
        win._deferredHubs = {}
        win._pendingContinue = None
        win._restReady = []
        win._restLeft = 0
        win._restBatch = None
        win._restSectionReady = False
        win._restApplied = False
        win._continueDone = False
        win._preserveContinueRows = False
        win._periodicDraw = False
        win._periodicSection = None
        win._protectFocus = False
        win._periodicSpan = None
        win._hubRefreshSpan = None
        win._applyHeld = False
        win._hubApplyLock = __import__("threading").Lock()
        win.updateHubs = {}
        win.lastSection = type("Section", (), {"key": None})()
        win.shown = []
        win.showHub = lambda *args, **kwargs: win.shown.append((args, kwargs)) or True
        win.getFocusId = lambda: 50
        self.ondeck = _Hub("home.ondeck", ("1", "2"))
        self.continue_hub = _Hub("home.continue", ("3",))
        win.hubControls = [
            _Control(self.ondeck, ("1", "2")),
            _Control(self.continue_hub, ("3",)),
        ]
        win._winID = 13001
        ENV.current_window_id = 14000
        self.win = win
        # These drains run on the pytest thread. That thread is the event
        # loop for this window, even though it is also Python's main thread.
        hubrefresh.note_script_thread()
        self.generation = hubrefresh.note_stop_sent()

    def tearDown(self):
        hubrefresh.reset()

    def _queue(self, hub=None, ident="home.ondeck"):
        hub = hub or self.ondeck
        self.win._pendingContinue = (self.generation, [(ident, hub)])

    def test_stop_off_home_waits_until_home_returns(self):
        fresh = _Hub("home.ondeck", ("2", "1"))
        self._queue(fresh)
        self.win._drain_hub_refresh()
        self.assertEqual(self.win.shown, [])
        self.assertIsNotNone(self.win._pendingContinue)

        self.env.current_window_id = self.win._winID
        self.win._drain_hub_refresh()
        self.assertEqual(len(self.win.shown), 1)
        self.assertTrue(self.win.shown[0][1]["pin_index"])
        self.assertIsNone(self.win._pendingContinue)
        self.assertEqual(self.win.updateHubs["home.ondeck"], fresh)

    def test_ignored_reinit_still_applies_when_home_is_visible(self):
        fresh = _Hub("home.ondeck", ("2", "1"))
        self._queue(fresh)
        self.win._ignoreReInit = True
        self.env.current_window_id = self.win._winID
        self.win.onReInit()
        self.assertEqual(len(self.win.shown), 1)
        self.assertTrue(self.win.shown[0][1]["pin_index"])

    def test_periodic_refresh_holds_focused_on_deck(self):
        # The user has not moved since Home returned. Post-playback may
        # update the focused row in place. The five-minute refresh must not.
        self.env.current_window_id = self.win._winID
        self.win.getFocusId = lambda: 400
        self.win._navSinceReturn = False
        moved = _Hub("home.ondeck", ("2", "1"))
        held = self.win._apply_hub_in_place(
            moved, 0, "periodic", ident="home.ondeck", always_hold=True)
        self.assertEqual(held, "defer")
        self.assertEqual(self.win.shown, [])
        self.assertIs(self.win._deferredHubs[0], moved)

        painted = self.win._apply_hub_in_place(
            _Hub("home.continue", ("9", "3")), 1, "cw", ident="home.continue")
        self.assertEqual(painted, "apply")
        self.assertTrue(self.win.shown[0][1]["pin_index"])
        self.assertEqual(self.win.shown[0][0][0].ident, "home.continue")


class _Action(object):
    def getId(self):
        return 0


class ScriptThreadPaintTest(unittest.TestCase):
    """The home event loop is not Python's main thread under Kodi."""

    def setUp(self):
        hubrefresh.reset()
        self._timing = os.environ.get("PM4K_TIMING")
        os.environ["PM4K_TIMING"] = "1"
        from lib import timing
        timing.reset()
        home = _load_home()
        from kodienv import ENV
        self.env = ENV
        self.home = home
        self._log_at = len(ENV.log_lines)

    def tearDown(self):
        hubrefresh.reset()
        from lib import timing
        timing.reset()
        if self._timing is None:
            os.environ.pop("PM4K_TIMING", None)
        else:
            os.environ["PM4K_TIMING"] = self._timing

    def _logged(self, needle):
        return any(needle in msg for msg, _lvl in self.env.log_lines[self._log_at:])

    def _window(self):
        win = self.home.HomeWindow.__new__(self.home.HomeWindow)
        win._shuttingDown = False
        win._closing = False
        win._ignoreReInit = False
        win._ignoreInput = True
        win._ignoreTick = True
        win._goRootHoldUntil = 0
        win._drainingHubs = False
        win._navSinceReturn = False
        win._suspendHubNav = False
        win._deferredHubs = {}
        win._pendingContinue = None
        win._restReady = []
        win._restLeft = 0
        win._restBatch = None
        win._restSectionReady = False
        win._restApplied = False
        win._continueDone = False
        win._preserveContinueRows = False
        win._periodicDraw = False
        win._periodicSection = None
        win._protectFocus = False
        win._periodicSpan = None
        win._hubRefreshSpan = None
        win._applyHeld = False
        win._hubApplyLock = threading.Lock()
        win._updateSourceChanged = False
        win.movingSection = False
        win.lastFocusID = None
        win.changingServer = False
        win._checkingForExit = False
        win.updateHubs = {}
        win.lastSection = type("Section", (), {"key": None})()
        win.shown = []
        win.showHub = lambda *args, **kwargs: win.shown.append((args, kwargs)) or True
        win.getFocusId = lambda: 50
        self.ondeck = _Hub("home.ondeck", ("1", "2"))
        self.continue_hub = _Hub("home.continue", ("3",))
        self.recent = _Hub("movie.recentlyadded", ("4", "5"))
        win.hubControls = [
            _Control(self.ondeck, ("1", "2")),
            _Control(self.continue_hub, ("3",)),
            _Control(self.recent, ("4", "5")),
        ]
        win._winID = 13001
        self.env.current_window_id = 14000
        return win

    def test_non_main_script_runner_applies_continue_and_rest(self):
        errors = []

        def runner():
            try:
                self._run_on_script_thread()
            except Exception:
                errors.append(traceback.format_exc())

        worker = threading.Thread(target=runner)
        worker.start()
        worker.join(_APPLY_WAIT_S + 3)
        self.assertFalse(worker.is_alive(), "script runner did not finish")
        self.assertFalse(errors, errors)

    def _run_on_script_thread(self):
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        self.assertFalse(hubrefresh.on_script_thread())
        win = self._window()
        generation = hubrefresh.note_stop_sent()
        fresh_od = _Hub("home.ondeck", ("2", "1"))
        fresh_cw = _Hub("home.continue", ("6", "3"))
        fresh_recent = _Hub("movie.recentlyadded", ("5", "4"))
        win._pendingContinue = (generation, [
            ("home.ondeck", fresh_od),
            ("home.continue", fresh_cw),
        ])
        win._restBatch = generation
        win._restLeft = 1
        win._restReady = []

        pool_errors = []

        def pool_worker():
            try:
                # The fetch callback stores the row. It must not paint, and
                # it must not become the script thread by touching apply.
                win._on_rest_hub(fresh_recent, generation)
                win._drain_hub_refresh()
                if hubrefresh.on_script_thread():
                    pool_errors.append("pool worker claimed the script thread")
            except Exception:
                pool_errors.append(traceback.format_exc())

        pool = threading.Thread(target=pool_worker)
        pool.start()
        pool.join(2)
        self.assertFalse(pool.is_alive())
        self.assertFalse(pool_errors, pool_errors)
        self.assertFalse(hubrefresh.on_script_thread())
        self.assertEqual(win.shown, [])
        self.assertIsNotNone(win._pendingContinue)
        self.assertTrue(win._restReady)
        self.assertTrue(win._hub_apply_waiting())

        # Home is back on screen but is not the active window yet. The
        # event loop records itself here; the rows stay queued.
        win.onAction(_Action())
        self.assertTrue(hubrefresh.on_script_thread())
        self.assertEqual(win._hub_apply_block(), "not-home")
        self.assertEqual(win.shown, [])
        self.assertTrue(win._hub_apply_waiting())

        self.env.current_window_id = win._winID
        self.assertIsNone(win._hub_apply_block())
        # The cron tick asks again once Home is visible. It runs on its own
        # thread, so it must not paint and must not take the script ident.
        deadline = time.monotonic() + _APPLY_WAIT_S
        while time.monotonic() < deadline:
            if not win._hub_apply_waiting():
                break
            cron_errors = []

            def cron_tick():
                try:
                    painted = len(win.shown)
                    win.tick()
                    if len(win.shown) != painted:
                        cron_errors.append("cron painted")
                    if hubrefresh.on_script_thread():
                        cron_errors.append("cron claimed the script thread")
                except Exception:
                    cron_errors.append(traceback.format_exc())

            cron = threading.Thread(target=cron_tick)
            cron.start()
            cron.join(1)
            self.assertFalse(cron.is_alive())
            self.assertFalse(cron_errors, cron_errors)
            self.assertTrue(hubrefresh.on_script_thread())
            win.onAction(_Action())
        else:
            self.fail("queued hub refresh still pending after Home was the active window")

        self.assertFalse(win._hub_apply_waiting())
        idents = [call[0][0].ident for call in win.shown]
        self.assertEqual(idents, ["home.ondeck", "home.continue", "movie.recentlyadded"])
        self.assertTrue(all(call[1]["pin_index"] for call in win.shown))
        self.assertIsNone(win._pendingContinue)
        self.assertFalse(win._restReady)
        self.assertTrue(self._logged("phase=apply.skipped.off-thread"))
        self.assertTrue(self._logged("phase=apply.skipped.not-home"))
        self.assertTrue(self._logged("phase=apply.retry"))
        self.assertTrue(self._logged("phase=cw.applied"))
        self.assertTrue(self._logged("phase=rest.applied"))
        self.assertTrue(self._logged("phase=rest.end"))

        # Focus leaving a control is another chance to paint, on the same thread.
        win.shown = []
        again = _Hub("movie.recentlyadded", ("7",))
        win._restReady = [again]
        win._restLeft = 0
        win._restBatch = generation
        win._restApplied = False
        win._applyHeld = True
        win.movingSection = True
        win.onFocus(50)
        self.assertEqual([call[0][0].ident for call in win.shown], ["movie.recentlyadded"])
        self.assertTrue(win.shown[0][1]["pin_index"])
        self.assertFalse(win._hub_apply_waiting())

        # A click retries as well, and still does not follow an item.
        win.shown = []
        clicked = _Hub("home.ondeck", ("8", "2"))
        win._pendingContinue = (generation, [("home.ondeck", clicked)])
        win._continueDone = False
        win._applyHeld = True
        win.onClick(50)
        self.assertEqual([call[0][0].ident for call in win.shown], ["home.ondeck"])
        self.assertTrue(win.shown[0][1]["pin_index"])
        self.assertFalse(win._hub_apply_waiting())


if __name__ == "__main__":
    unittest.main()
