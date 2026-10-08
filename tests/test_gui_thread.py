# coding=utf-8
"""Background workers must not touch Kodi list items.

A list item method from a worker thread is what crashed Kodi while a window
was closing. Workers may write a file or a dict. Applying that to a list item
stays on the add-on event-loop thread, and only for the generation that is
still current. That thread is not Python's main thread under Kodi.
"""
from __future__ import absolute_import

import ast
import os
import tempfile
import threading
import traceback

from lib import artprefetch, hubrefresh, playbackprep, stickysubs, timing

from .base import KodiTestCase, REPO_ROOT

_GUI_ATTRS = frozenset((
    "setArt", "setThumbnailImage", "setProperty", "setLabel", "setLabel2",
    "addItem", "addItems", "getControl",
))

# The threads this branch added. Timing may ask Kodi for a thumb hash; it must
# not touch a list item or a control. Playback prep and sticky subs have no GUI.
_WORKER_FUNCTIONS = {
    "lib/artprefetch.py": ("_worker", "_download", "_run_focus", "_run_episodes", "_store_ready", "_trim"),
    "lib/playbackprep.py": ("_worker", "_prepare", "_stop_session"),
    "lib/stickysubs.py": ("apply", "poll", "remember_stream", "on_subtitle_pass"),
    "lib/timing.py": ("_art_loop", "poll_art", "_thumb_file_exists"),
}


def _function_sources(path, names):
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
        text = handle.read()
    tree = ast.parse(text, filename=path)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = node
    return found


def _gui_calls(node):
    hits = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in _GUI_ATTRS:
            hits.append(child.attr)
        if isinstance(child, ast.Name) and child.id == "xbmcgui":
            hits.append("xbmcgui")
    return hits


class _Item(object):
    def __init__(self, url):
        self._valid = True
        self.thumbnailImage = url
        self.updated = []

    def setThumbnailImage(self, path):
        if not hubrefresh.on_script_thread():
            raise RuntimeError("list item updated off the GUI thread")
        self.thumbnailImage = path
        self.updated.append(path)


def _guard_listitem(method):
    def wrapped(*args, **kwargs):
        if not hubrefresh.on_script_thread():
            raise RuntimeError("xbmcgui.{0} off the GUI thread".format(method))
        return None
    return wrapped


class GuiThreadTest(KodiTestCase):
    def setUp(self):
        super(GuiThreadTest, self).setUp()
        self._dir = tempfile.mkdtemp()
        os.environ["PM4K_ART_CACHE"] = self._dir
        artprefetch._reset_for_tests()
        # Existing cases call bind and flush on the pytest thread. That
        # thread is the event loop for those cases.
        hubrefresh.reset()
        hubrefresh.note_script_thread()

    def tearDown(self):
        artprefetch._reset_for_tests()
        hubrefresh.reset()
        os.environ.pop("PM4K_ART_CACHE", None)
        super(GuiThreadTest, self).tearDown()

    def test_worker_functions_do_not_reference_gui_methods(self):
        for path, names in _WORKER_FUNCTIONS.items():
            found = _function_sources(path, names)
            self.assertEqual(set(names), set(found), path)
            for name, node in found.items():
                self.assertEqual(_gui_calls(node), [], "{0}:{1}".format(path, name))

    def test_a_background_download_does_not_touch_the_list_item(self):
        url = "https://example.test/photo/poster"
        path = artprefetch.path_for(url)
        with open(path, "wb") as handle:
            handle.write(b"img")
        item = _Item(url)
        generation = object()
        errors = []

        def work():
            try:
                artprefetch._download(url)
                artprefetch.bind([item], generation)
                artprefetch.flush(generation)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=work, name="pm4k-art-test")
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(item.updated, [])
        self.assertEqual(artprefetch._ready.get(url), path)

        artprefetch.bind([item], generation)
        self.assertEqual(item.updated, [path])
        self.assertFalse(artprefetch._pending)

    def test_flush_skips_a_closed_generation_and_a_dead_item(self):
        url = "https://example.test/photo/other"
        path = artprefetch.path_for(url)
        with open(path, "wb") as handle:
            handle.write(b"img")
        live = _Item(url)
        dead = _Item(url)
        dead._valid = False
        current = object()
        closed = object()
        artprefetch.bind([live], closed)
        artprefetch.bind([dead], current)
        artprefetch._store_ready(url, path)
        artprefetch.drop_generation(closed)
        artprefetch.flush(current)
        self.assertEqual(live.updated, [])
        self.assertEqual(dead.updated, [])

    def test_fake_listitem_rejects_a_background_caller(self):
        from kodi_six import xbmcgui
        original_art = xbmcgui.ListItem.setArt
        original_prop = xbmcgui.ListItem.setProperty
        xbmcgui.ListItem.setArt = _guard_listitem("setArt")
        xbmcgui.ListItem.setProperty = _guard_listitem("setProperty")
        try:
            item = xbmcgui.ListItem()
            item.setArt({"thumb": "ok"})
            caught = []

            def work():
                try:
                    xbmcgui.ListItem().setArt({"thumb": "nope"})
                except RuntimeError as exc:
                    caught.append(str(exc))

            thread = threading.Thread(target=work, name="pm4k-art-1")
            thread.start()
            thread.join(timeout=2)
            self.assertEqual(len(caught), 1)
            self.assertIn("setArt", caught[0])
        finally:
            xbmcgui.ListItem.setArt = original_art
            xbmcgui.ListItem.setProperty = original_prop

    def test_playback_prep_and_sticky_subs_are_imported(self):
        self.assertTrue(callable(playbackprep._worker))
        self.assertTrue(callable(stickysubs.apply))
        self.assertTrue(callable(timing._art_loop))

    def test_non_main_script_thread_applies_cached_art(self):
        errors = []

        def runner():
            try:
                self._apply_on_script_thread()
            except Exception:
                errors.append(traceback.format_exc())

        worker = threading.Thread(target=runner)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "script runner did not finish")
        self.assertFalse(errors, errors)

    def _apply_on_script_thread(self):
        hubrefresh.reset()
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        self.assertFalse(hubrefresh.on_script_thread())
        url = "https://example.test/photo/event-loop"
        path = artprefetch.path_for(url)
        with open(path, "wb") as handle:
            handle.write(b"img")
        item = _Item(url)
        generation = object()
        # Unknown is not the event loop, so this thread cannot paint yet.
        artprefetch.bind([item], generation)
        artprefetch.flush(generation)
        self.assertEqual(item.updated, [])
        self.assertFalse(artprefetch._pending)

        worker_errors = []

        def pool():
            try:
                artprefetch._store_ready(url, path)
                artprefetch.bind([item], generation)
                artprefetch.flush(generation)
                if hubrefresh.on_script_thread():
                    worker_errors.append("worker claimed the event loop")
            except Exception:
                worker_errors.append(traceback.format_exc())

        pool_thread = threading.Thread(target=pool, name="pm4k-art-test")
        pool_thread.start()
        pool_thread.join(2)
        self.assertFalse(pool_thread.is_alive())
        self.assertFalse(worker_errors, worker_errors)
        self.assertEqual(item.updated, [])
        self.assertFalse(hubrefresh.on_script_thread())

        from lib.windows import kodigui
        window = type("Win", (), {"_art_token": generation})()
        kodigui._service_script_thread(window)
        self.assertTrue(hubrefresh.on_script_thread())
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        # The file is ready, so the event loop can point the item at it.
        artprefetch.bind([item], generation)
        self.assertEqual(item.updated, [path])

        # A row recorded here waits for the next tick. The worker must not apply it.
        waiting = _Item(url)
        artprefetch._ready.pop(url, None)
        artprefetch.bind([waiting], generation)
        self.assertEqual(waiting.updated, [])
        self.assertTrue(artprefetch._pending)

        def pool_again():
            try:
                artprefetch._store_ready(url, path)
                artprefetch.flush(generation)
                if hubrefresh.on_script_thread():
                    worker_errors.append("worker claimed the event loop")
            except Exception:
                worker_errors.append(traceback.format_exc())

        again = threading.Thread(target=pool_again, name="pm4k-art-test")
        again.start()
        again.join(2)
        self.assertFalse(again.is_alive())
        self.assertFalse(worker_errors, worker_errors)
        self.assertEqual(waiting.updated, [])
        kodigui._service_script_thread(window)
        self.assertEqual(waiting.updated, [path])
        self.assertFalse(artprefetch._pending)
