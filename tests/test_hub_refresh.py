# coding=utf-8
"""Continue Watching reloads after the stopped timeline, and a moved row waits."""
from __future__ import absolute_import

import ast
import os
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


if __name__ == "__main__":
    unittest.main()
