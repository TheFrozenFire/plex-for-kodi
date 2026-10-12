# coding=utf-8
"""A subtitle choice made during playback applies to later episodes of that show."""
from __future__ import absolute_import

import json
import os
import shutil
import tempfile

from lib import playbackprep, stickysubs, util

from .base import KodiTestCase


class _Stream(object):
    def __init__(self, ident, language, type_index, forced=False, sdh=False, codec="srt",
                 selected=False, title=""):
        self.id = ident
        self.languageCode = language
        self.typeIndex = type_index
        self.forced_subtitle = forced
        self.sdh = sdh
        self.codec = codec
        self.selected = selected
        self.embedded = True
        self.title = title

    def isSelected(self):
        return self.selected

    def setSelected(self, selected):
        self.selected = bool(selected)


class _Video(object):
    def __init__(self, rating, show, streams, kind="episode"):
        self.ratingKey = rating
        self.grandparentRatingKey = show
        self.type = kind
        self.subtitleStreams = streams
        self._current_subtitle_idx = None
        self.manually_selected_sub_stream = False
        self.current_subtitle_is_embedded = False


def _english_pair():
    full = _Stream("11", "eng", 0, title="do-not-store-this-title")
    forced = _Stream("12", "eng", 1, forced=True, codec="srt")
    other = _Stream("13", "deu", 2, codec="srt")
    return [full, forced, other]


class StickySubsTest(KodiTestCase):
    def setUp(self):
        super(StickySubsTest, self).setUp()
        self._dir = tempfile.mkdtemp()
        self._path = os.path.join(self._dir, "sticky_subs.json")
        os.environ["PM4K_STICKY_SUBS"] = self._path
        self._setting = os.environ.get("PM4K_TIMING")
        stickysubs._reset_for_tests()
        util.setSetting("sticky_subtitles", True)

    def tearDown(self):
        stickysubs._reset_for_tests()
        playbackprep._reset_for_tests()
        os.environ.pop("PM4K_STICKY_SUBS", None)
        util.setSetting("sticky_subtitles", True)
        shutil.rmtree(self._dir, ignore_errors=True)
        super(StickySubsTest, self).tearDown()

    def _episode(self, rating, streams=None, show="1001"):
        return _Video(rating, show, streams if streams is not None else _english_pair())

    def test_off_is_remembered_and_applied_to_the_next_episode(self):
        first = self._episode("501")
        self.assertTrue(stickysubs.remember_stream(first, None))
        stickysubs._reset_for_tests()
        second = self._episode("502")
        second.subtitleStreams[0].setSelected(True)
        self.assertTrue(stickysubs.apply(second))
        self.assertFalse(any(stream.isSelected() for stream in second.subtitleStreams))
        self.assertIsNone(second._current_subtitle_idx)
        with open(self._path, encoding="utf-8") as handle:
            text = handle.read()
        stored = json.loads(text)
        self.assertEqual({"1001": {"mode": "off"}}, stored["shows"])
        self.assertNotIn("do-not-store-this-title", text)

    def test_a_language_choice_picks_the_matching_track(self):
        first = self._episode("501")
        stickysubs.remember_stream(first, first.subtitleStreams[0])
        second = self._episode("502", [
            _Stream("21", "deu", 0),
            _Stream("22", "en", 1, codec="srt"),
            _Stream("23", "en", 2, forced=True),
        ])
        self.assertTrue(stickysubs.apply(second))
        self.assertTrue(second.subtitleStreams[1].isSelected())
        self.assertFalse(second.subtitleStreams[2].isSelected())
        self.assertEqual(1, second._current_subtitle_idx)

    def test_forced_and_stream_id_are_preferred_when_they_match(self):
        video = self._episode("501")
        stickysubs.remember_stream(video, video.subtitleStreams[1])
        later = self._episode("502")
        self.assertTrue(stickysubs.apply(later))
        self.assertTrue(later.subtitleStreams[1].isSelected())
        same = self._episode("501")
        pref = stickysubs.get("1001")
        pref["language"] = "de"
        stickysubs._put("1001", pref)
        self.assertTrue(stickysubs.apply(same))
        self.assertTrue(same.subtitleStreams[1].isSelected())

    def test_a_missing_language_does_not_pick_a_random_track(self):
        video = self._episode("501")
        stickysubs._put("1001", {"mode": "track", "language": "ja", "forced": False, "sdh": False})
        video.subtitleStreams[0].setSelected(True)
        self.assertFalse(stickysubs.apply(video))
        self.assertTrue(video.subtitleStreams[0].isSelected())

    def test_the_initial_apply_does_not_become_a_saved_choice(self):
        video = self._episode("501")
        self.assertFalse(stickysubs.apply(video))
        self.assertFalse(os.path.isfile(self._path))
        stickysubs.on_subtitle_pass(video)
        self.assertFalse(os.path.isfile(self._path))

    def test_a_later_pass_does_not_overwrite_a_new_selection(self):
        video = self._episode("501")
        stickysubs.remember_stream(video, None)
        stickysubs.on_subtitle_pass(video)
        self.assertFalse(any(stream.isSelected() for stream in video.subtitleStreams))
        video.subtitleStreams[0].setSelected(True)
        video._current_subtitle_idx = 0
        stickysubs.on_subtitle_pass(video)
        self.assertTrue(video.subtitleStreams[0].isSelected())
        self.assertEqual(0, video._current_subtitle_idx)
        nxt = self._episode("502")
        nxt.subtitleStreams[0].setSelected(True)
        stickysubs.on_subtitle_pass(nxt)
        self.assertFalse(any(stream.isSelected() for stream in nxt.subtitleStreams))

    def test_turning_subtitles_back_on_replaces_off(self):
        video = self._episode("501")
        stickysubs.remember_stream(video, None)
        stickysubs.remember_stream(video, video.subtitleStreams[0])
        self.assertEqual("track", stickysubs.get("1001")["mode"])
        self.assertEqual("en", stickysubs.get("1001")["language"])

    def test_kodi_changes_after_arm_are_saved_and_the_snapshot_is_not(self):
        video = self._episode("501")
        initial = {
            "subtitleenabled": True,
            "currentsubtitle": {"index": 0, "language": "eng", "isforced": False, "isimpaired": False},
        }
        stickysubs.arm(video, initial)
        stickysubs._QUIET_UNTIL = 0
        stickysubs.poll(video, initial)
        self.assertIsNone(stickysubs.get("1001"))
        stickysubs.poll(video, {"subtitleenabled": False, "currentsubtitle": {}})
        self.assertEqual("off", stickysubs.get("1001")["mode"])
        stickysubs.poll(video, {
            "subtitleenabled": True,
            "currentsubtitle": {"index": 4, "language": "ger", "isforced": False, "isimpaired": True},
        })
        saved = stickysubs.get("1001")
        self.assertEqual("track", saved["mode"])
        self.assertEqual("de", saved["language"])
        self.assertTrue(saved["sdh"])

    def test_kodi_changes_while_the_add_on_is_setting_the_track_are_ignored(self):
        video = self._episode("501")
        stickysubs.arm(video, {"subtitleenabled": True, "currentsubtitle": {"index": 0, "language": "eng"}})
        stickysubs.quiet(30)
        stickysubs.poll(video, {"subtitleenabled": False, "currentsubtitle": {}})
        self.assertIsNone(stickysubs.get("1001"))
        stickysubs._QUIET_UNTIL = 0
        stickysubs.poll(video, {"subtitleenabled": False, "currentsubtitle": {}})
        self.assertIsNone(stickysubs.get("1001"))
        stickysubs.poll(video, {
            "subtitleenabled": True,
            "currentsubtitle": {"index": 1, "language": "spa", "isforced": True, "isimpaired": False},
        })
        self.assertEqual("es", stickysubs.get("1001")["language"])

    def test_clear_one_show_leaves_the_other(self):
        stickysubs.remember_stream(self._episode("1", show="1001"), None)
        stickysubs.remember_stream(self._episode("2", show="1002"), None)
        show = _Video("1001", "", [], kind="show")
        keys = [item["key"] for item in stickysubs.menu_options(show)]
        self.assertEqual(["sticky_subs_clear", "sticky_subs_clear_all"], keys)
        self.assertTrue(stickysubs.handle_menu("sticky_subs_clear", show))
        self.assertIsNone(stickysubs.get("1001"))
        self.assertEqual("off", stickysubs.get("1002")["mode"])
        self.assertTrue(stickysubs.handle_menu("sticky_subs_clear_all", show))
        self.assertFalse(stickysubs.any_saved())
        self.assertEqual([], stickysubs.menu_options(show))

    def test_the_setting_off_does_not_save_or_apply(self):
        util.setSetting("sticky_subtitles", False)
        video = self._episode("501")
        self.assertFalse(stickysubs.remember_stream(video, None))
        stickysubs._put("1001", {"mode": "off"})
        video.subtitleStreams[0].setSelected(True)
        self.assertFalse(stickysubs.apply(video))
        self.assertTrue(video.subtitleStreams[0].isSelected())
        self.assertEqual([], stickysubs.menu_options(video))

    def test_a_saved_choice_drops_a_prefetched_decision(self):
        playbackprep._PREP = {"key": ("501", 0)}
        stickysubs.remember_stream(self._episode("501"), None)
        self.assertIsNone(playbackprep._PREP)

    def test_debug_lines_do_not_include_the_show_key(self):
        captured = []

        def capture(msg, *args, **kwargs):
            if args:
                msg = msg.format(*args)
            captured.append(msg)

        original = util.DEBUG_LOG
        util.DEBUG_LOG = capture
        try:
            stickysubs.remember_stream(self._episode("501", show="99887766"), None)
            stickysubs.apply(self._episode("502", show="99887766"))
        finally:
            util.DEBUG_LOG = original
        self.assertTrue(captured)
        self.assertNotIn("99887766", "\n".join(captured))

    def test_language_codes_share_one_form(self):
        self.assertEqual("en", stickysubs.norm_lang("eng"))
        self.assertEqual("en", stickysubs.norm_lang("en"))
        self.assertEqual("de", stickysubs.norm_lang("ger"))
        self.assertEqual("de", stickysubs.norm_lang("deu"))
        self.assertEqual("", stickysubs.norm_lang(""))
