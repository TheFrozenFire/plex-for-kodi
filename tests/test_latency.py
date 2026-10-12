# coding=utf-8
"""The first latency cuts, each behind a setting that defaults on."""
from __future__ import absolute_import

from kodienv import ENV

# Importing the episode window imports the player, which starts a monitor thread.
ENV.abort_requested = True

import os
import tempfile

import time

from lib import artprefetch, playbackprep, timing
from lib.windows import episodes, pagination
from plexnet import plexconnection
from plexnet import util as pnutil

from .base import KodiTestCase


class _Page(list):
    def __init__(self, count, total):
        super(_Page, self).__init__([object()] * count)
        self.totalSize = total


class _Hub(pagination.BaseRelatedPaginator):
    thumbFallback = None

    def __init__(self, *args, **kwargs):
        self.asked = []
        super(_Hub, self).__init__(*args, **kwargs)

    def getData(self, offset, amount):
        self.asked.append((offset, amount))
        return _Page(min(amount, 3), 20)

    def createListItem(self, rel):
        class Item(object):
            def setProperty(self, *args, **kwargs):
                return None

            def setBoolProperty(self, *args, **kwargs):
                return None

        return Item()

    def prepareListItem(self, data, mli):
        return None


class _Control(object):
    def replaceItems(self, items):
        self.items = items

    def selectItem(self, index):
        return None

    def getSelectedItem(self):
        return None


class RelatedCountTest(KodiTestCase):
    def test_default_does_not_ask_for_a_count(self):
        class Item(object):
            @property
            def relatedCount(self):
                raise AssertionError("count-only request")

        self.assertIsNone(pagination.related_leaf_count(Item()))

    def test_setting_off_reads_the_count(self):
        ENV.settings["defer_related_count"] = "false"

        class Item(object):
            relatedCount = 7

        self.assertEqual(pagination.related_leaf_count(Item()), 7)

    def test_first_page_learns_the_total_without_a_count_request(self):
        hub = _Hub(_Control(), object(), leaf_count=None)
        hub.paginate()
        self.assertEqual(hub.asked, [(0, hub.initialPageSize)])
        self.assertEqual(hub.leafCount, 20)


class SeasonPaintTest(KodiTestCase):
    def test_show_already_in_hand_is_not_reloaded(self):
        show = object()
        self.assertIs(episodes.show_for_season_open(show, None, None), show)
        self.assertNotIn("checkFiles", episodes.metadata_reload_kwargs())

    def test_missing_show_is_fetched_once(self):
        fetched = object()

        class Season(object):
            calls = 0

            def show(self):
                Season.calls += 1
                return fetched

        self.assertIs(episodes.show_for_season_open(None, None, Season()), fetched)
        self.assertEqual(Season.calls, 1)

    def test_setting_off_reloads_and_stats_files(self):
        ENV.settings["fast_season_paint"] = "false"

        class Show(object):
            def reload(self, **kwargs):
                self.kwargs = kwargs
                return "reloaded"

        show = Show()
        self.assertEqual(episodes.show_for_season_open(show, None, None), "reloaded")
        self.assertEqual(show.kwargs["includeOnDeck"], 1)
        self.assertEqual(episodes.metadata_reload_kwargs()["checkFiles"], 1)
        self.assertEqual(episodes.metadata_reload_kwargs()["includeChapters"], 1)


class _Pref(object):
    def __init__(self, skip):
        self.skip = skip

    def getPreference(self, pref, default=None, **kwargs):
        if pref == "skip_dead_connections":
            return self.skip
        return default

    def DEBUG_LOG(self, *args, **kwargs):
        return None

    def LOG(self, *args, **kwargs):
        return None


class ReachabilityTest(KodiTestCase):
    def setUp(self):
        super(ReachabilityTest, self).setUp()
        self._interface = pnutil.INTERFACE

    def tearDown(self):
        pnutil.INTERFACE = self._interface
        super(ReachabilityTest, self).tearDown()

    def _connection(self):
        # Documentation range. Not an address from any real setup.
        return plexconnection.PlexConnection(1, "http://203.0.113.10:32400", False, "token")

    def test_known_unreachable_is_not_probed(self):
        pnutil.INTERFACE = _Pref(True)
        conn = self._connection()
        conn.state = conn.STATE_UNREACHABLE
        self.assertFalse(conn.testReachability(server=None))
        self.assertFalse(conn.hasPendingRequest)

    def test_unknown_address_is_still_tested(self):
        pnutil.INTERFACE = _Pref(True)
        conn = self._connection()
        with self.assertRaises(AttributeError):
            conn.testReachability(server=None)

    def test_setting_off_retests_an_unreachable_address(self):
        pnutil.INTERFACE = _Pref(False)
        conn = self._connection()
        conn.state = conn.STATE_UNREACHABLE
        with self.assertRaises(AttributeError):
            conn.testReachability(server=None)


class ArtPrefetchTest(KodiTestCase):
    def test_cache_name_does_not_contain_the_url(self):
        root = tempfile.mkdtemp()
        os.environ["PM4K_ART_CACHE"] = root
        try:
            url = "https://pms.example:32400/photo/:/transcode?width=244&X-Plex-Token=sekret-art"
            path = artprefetch.path_for(url)
            self.assertTrue(path.endswith(".img"))
            self.assertNotIn("sekret-art", path)
            self.assertNotIn("pms.example", path)
            with open(path, "wb") as handle:
                handle.write(b"img")
            self.assertEqual(artprefetch.resolve(url), path)
        finally:
            os.environ.pop("PM4K_ART_CACHE", None)
            artprefetch._reset_for_tests()

    def test_disabled_prefetch_leaves_the_url_alone(self):
        ENV.settings["prefetch_art"] = "false"
        url = "https://pms.example/photo/:/transcode?width=10"
        self.assertEqual(artprefetch.resolve(url), url)
        artprefetch.prefetch([url])
        self.assertTrue(artprefetch._queue.empty())

    def test_thumb_lookup_uses_the_thumbnails_directory(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "c"))
        cached = os.path.join(root, "c", "c0ffee.jpg")
        with open(cached, "wb") as handle:
            handle.write(b"x")
        found = timing._thumb_candidates("c/c0ffee.jpg", root)
        self.assertEqual(found[0], cached)
        self.assertTrue(os.path.exists(found[0]))


class PlaybackPrepTest(KodiTestCase):
    def tearDown(self):
        playbackprep._reset_for_tests()
        super(PlaybackPrepTest, self).tearDown()

    def test_a_fresh_decision_is_used_once(self):
        playbackprep._reset_for_tests()

        class Video(object):
            ratingKey = "55"

        video = Video()
        playbackprep._PREP = {"key": ("55", 1000), "obj": "decided", "at": time.time()}
        self.assertEqual(playbackprep.take(video, 1000), "decided")
        self.assertIsNone(playbackprep.take(video, 1000))

    def test_a_different_offset_is_not_reused(self):
        playbackprep._reset_for_tests()

        class Video(object):
            ratingKey = "55"

        playbackprep._PREP = {"key": ("55", 1000), "obj": "decided", "at": time.time()}
        self.assertIsNone(playbackprep.take(Video(), 0))

    def test_recent_metadata_skips_another_reload(self):
        class Video(object):
            _pm4k_soft_at = time.time()

        self.assertTrue(playbackprep.metadata_is_fresh(Video()))
        ENV.settings["fast_playback_start"] = "false"
        self.assertFalse(playbackprep.metadata_is_fresh(Video()))

    def _lease(self, session, rating="55", offset=1000, adopted=False, drop=None, deadline=None):
        lease = {
            "session": session,
            "server": object(),
            "key": (rating, offset),
            "obj": "decided",
            "adopted": adopted,
            "drop": drop,
            "deadline": time.time() + 300 if deadline is None else deadline,
        }
        playbackprep._LEASES[session] = lease
        return lease

    def _capture_stops(self):
        stops = []
        playbackprep._perform_stop = lambda server, path: stops.append(path)
        playbackprep._session_is_playing = lambda session, obj=None: False
        return stops

    def test_hold_defaults_to_five_minutes(self):
        self.assertEqual(playbackprep.hold_seconds(), 300)
        ENV.settings["playback_prep_hold"] = "15"
        self.assertEqual(playbackprep.hold_seconds(), 15)

    def test_an_unused_session_is_stopped_after_the_hold(self):
        stops = self._capture_stops()
        session = "sess-secret-value"
        self._lease(session, deadline=time.time() + 300)
        playbackprep.release_expired()
        self.assertEqual(stops, [])
        playbackprep._LEASES[session]["deadline"] = time.time() - 1
        playbackprep.release_expired()
        self.assertEqual(len(stops), 1)
        self.assertIn("/video/:/transcode/universal/stop?session=", stops[0])
        self.assertIn("sess-secret-value", stops[0])
        self.assertNotIn("X-Plex-Token", stops[0])

    def test_focus_change_stops_only_the_previous_item(self):
        stops = self._capture_stops()
        self._lease("previous-session", rating="1")
        self._lease("current-session", rating="2")
        with playbackprep._LOCK:
            playbackprep._mark_drop_locked(("2", 1000), "focus")
        playbackprep.flush_drops()
        self.assertEqual(len(stops), 1)
        self.assertIn("previous-session", stops[0])
        self.assertNotIn("current-session", stops[0])

    def test_a_taken_decision_is_not_stopped(self):
        stops = self._capture_stops()

        class Video(object):
            ratingKey = "55"

        session = "taken-session"
        playbackprep._PREP = {
            "key": ("55", 1000),
            "obj": "decided",
            "at": time.time(),
            "session": session,
        }
        self._lease(session, drop="close", deadline=time.time() - 1)
        self.assertEqual(playbackprep.take(Video(), 1000), "decided")
        playbackprep.flush_drops()
        playbackprep.release_expired()
        self.assertEqual(stops, [])

    def test_a_session_already_playing_is_not_stopped(self):
        from lib import player

        stops = []
        playbackprep._perform_stop = lambda server, path: stops.append(path)
        session = "live-session"
        player.PLAYER.sessionID = session
        try:
            self._lease(session, deadline=time.time() - 1)
            playbackprep.release_expired()
            self.assertEqual(stops, [])
            self.assertNotIn(session, playbackprep._LEASES)
        finally:
            player.PLAYER.sessionID = None

    def test_cleanup_log_redacts_the_session(self):
        os.environ["PM4K_TIMING"] = "1"
        timing.reset()
        try:
            playbackprep._log_cleanup("timeout", "sess-secret-value", skipped=False)
            self.assertTrue(ENV.logged("TIMING PREP"))
            self.assertTrue(ENV.logged("action=release"))
            self.assertTrue(ENV.logged("reason=timeout"))
            self.assertTrue(ENV.logged("session=****"))
            self.assertFalse(ENV.logged("sess-secret-value"))
        finally:
            os.environ.pop("PM4K_TIMING", None)
            timing.reset()
