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


if __name__ == "__main__":
    unittest.main()
