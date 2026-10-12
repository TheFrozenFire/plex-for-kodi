# coding=utf-8
"""Workers added for latency work must leave on shutdown.

Kodi 21 joins every Python thread after the script ends. A thread blocked in
queue.get or Event.wait without a timeout cannot be interrupted, so exit hangs.
"""
from __future__ import absolute_import

import os
import threading
import time

from kodienv import ENV

from lib import artprefetch, playbackprep, timing

from .base import KodiTestCase

_NAMES = ("pm4k-art-", "pm4k-playback-prep", "pm4k-art-timing")


def _ours(thread):
    return any(thread.name.startswith(name) or thread.name == name for name in _NAMES)


class WorkerShutdownTest(KodiTestCase):
    def setUp(self):
        super(WorkerShutdownTest, self).setUp()
        self._abort = ENV.abort_requested
        # Workers exit on their own when Kodi is aborting. Hold that off so
        # shutdown is what unblocks them.
        ENV.abort_requested = False
        self._timing = os.environ.pop("PM4K_TIMING", None)
        artprefetch._reset_for_tests()
        playbackprep._reset_for_tests()
        timing.shutdown()
        timing.reset()

    def tearDown(self):
        ENV.abort_requested = True
        artprefetch._reset_for_tests()
        playbackprep._reset_for_tests()
        timing.shutdown()
        with timing._art_lock:
            timing._art_pending[:] = []
        timing.set_art_probe(None)
        timing.reset()
        if self._timing is None:
            os.environ.pop("PM4K_TIMING", None)
        else:
            os.environ["PM4K_TIMING"] = self._timing
        ENV.abort_requested = self._abort
        super(WorkerShutdownTest, self).tearDown()

    def _start(self):
        os.environ["PM4K_TIMING"] = "1"
        timing.reset()
        timing.set_art_probe(lambda url: False)
        with timing._art_lock:
            timing._art_pending.append({
                "url": "https://example.test/photo/pending",
                "t0": time.perf_counter(),
                "span": "-",
            })
        artprefetch._ensure_workers()
        playbackprep._ensure_worker()
        timing._ensure_art_thread()
        threads = list(artprefetch._threads) + [playbackprep._WORKER, timing._art_thread]
        deadline = time.time() + 1
        while time.time() < deadline and not all(thread is not None and thread.is_alive() for thread in threads):
            time.sleep(0.02)
        return threads

    def test_shutdown_joins_every_added_worker(self):
        threads = self._start()
        self.assertTrue(all(thread is not None and thread.is_alive() for thread in threads))
        started = time.time()
        artprefetch.shutdown()
        playbackprep.shutdown()
        timing.shutdown()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), thread.name)
        self.assertLess(time.time() - started, 2.0)

    def test_no_blocked_worker_survives_shutdown(self):
        threads = self._start()
        for thread in threads:
            self.assertTrue(thread.daemon, thread.name)
        artprefetch.shutdown()
        playbackprep.shutdown()
        timing.shutdown()
        deadline = time.time() + 2
        while time.time() < deadline:
            alive = [thread for thread in threading.enumerate() if thread.is_alive() and _ours(thread)]
            if not alive:
                break
            time.sleep(0.05)
        alive = [thread for thread in threading.enumerate() if thread.is_alive() and _ours(thread)]
        self.assertEqual([thread.name for thread in alive], [])
        blocked = [
            thread.name for thread in threads
            if thread.is_alive() and not thread.daemon
        ]
        self.assertEqual(blocked, [])
