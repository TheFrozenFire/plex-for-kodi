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
