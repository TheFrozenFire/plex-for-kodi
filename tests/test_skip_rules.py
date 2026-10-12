# coding=utf-8
"""Content skip rules: matching, ranges, and the offline cache."""
from __future__ import absolute_import

import json
import os
import tempfile
import unittest

import lib.skiprules as skiprules


def _episode(**overrides):
    item = {
        "rating_key": "12345",
        "server_id": "machine-1",
        "server_name": "Example",
        "type": "episode",
        "imdb": {"tt0000001"},
        "tvdb": {"100"},
        "show_title": "Example Show",
        "season": 1,
        "episode": 2,
        "movie_title": "Example Show",
        "year": 2020,
        "duration_ms": 1_800_000,
    }
    item.update(overrides)
    return item


def _movie(**overrides):
    item = {
        "rating_key": "999",
        "server_id": "machine-1",
        "server_name": "Example",
        "type": "movie",
        "imdb": {"tt0000009"},
        "tvdb": set(),
        "show_title": "",
        "season": None,
        "episode": None,
        "movie_title": "Example Movie",
        "year": 1999,
        "duration_ms": 5_000_000,
    }
    item.update(overrides)
    return item


def _rule(**overrides):
    rule = {
        "id": "rule-1",
        "type": "skip_range",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "reason": "intro",
    }
    rule.update(overrides)
    return rule


class ParseTests(unittest.TestCase):
    def test_unknown_fields_and_invalid_rules(self):
        document, errors = skiprules.parse_document({
            "version": 1,
            "updated_at": "2026-01-01T00:00:00Z",
            "extra": "ignored",
            "rules": [
                _rule(id="ok", plex_rating_key="12345", note="ignored"),
                {"id": "", "type": "skip_range", "start_ms": 0, "end_ms": 1},
                {"id": "bad-end", "type": "skip_range", "plex_rating_key": "1",
                 "start_ms": 5, "end_ms": 5},
                {"type": "skip_item"},
                {"id": "no-match", "type": "skip_item"},
            ],
        })
        self.assertEqual(["ok"], [rule["id"] for rule in document["rules"]])
        self.assertGreaterEqual(len(errors), 3)
        self.assertNotIn("note", document["rules"][0])


class MatchTests(unittest.TestCase):
    def test_rating_key_beats_guid_and_title(self):
        rules, _ = skiprules.parse_document({
            "version": 1,
            "rules": [
                _rule(id="by-key", plex_rating_key="12345", show_title="Other", season=9, episode=9),
                _rule(id="by-imdb", imdb="tt0000001", start_ms=30_000, end_ms=40_000),
                _rule(id="by-title", show_title="example show", season=1, episode=2,
                      start_ms=50_000, end_ms=60_000),
                _rule(id="wrong-key", plex_rating_key="000", show_title="Example Show",
                      season=1, episode=2),
            ],
        })
        matched = skiprules.select(rules["rules"], _episode())
        self.assertEqual(["by-key"], [rule["id"] for rule in matched])

    def test_guid_used_when_no_rating_key_hits(self):
        rules, _ = skiprules.parse_document({
            "version": 1,
            "rules": [
                _rule(id="wrong-key", plex_rating_key="000", show_title="Example Show",
                      season=1, episode=2),
                _rule(id="by-tvdb", tvdb="100", start_ms=1, end_ms=2),
                _rule(id="by-title", show_title="Example Show", season=1, episode=2),
            ],
        })
        matched = skiprules.select(rules["rules"], _episode())
        self.assertEqual(["by-tvdb"], [rule["id"] for rule in matched])

    def test_title_is_case_insensitive_and_last(self):
        rules, _ = skiprules.parse_document({
            "version": 1,
            "rules": [
                _rule(id="by-title", show_title="EXAMPLE SHOW", season=1, episode=2),
                _rule(id="other-episode", show_title="Example Show", season=1, episode=3),
            ],
        })
        matched = skiprules.select(rules["rules"], _episode())
        self.assertEqual(["by-title"], [rule["id"] for rule in matched])

    def test_server_must_match_when_given(self):
        rules, _ = skiprules.parse_document({
            "version": 1,
            "rules": [
                _rule(id="other-server", plex_rating_key="12345", server="machine-9"),
                _rule(id="by-name", plex_rating_key="12345", server="example"),
            ],
        })
        matched = skiprules.select(rules["rules"], _episode())
        self.assertEqual(["by-name"], [rule["id"] for rule in matched])

    def test_movie_title_and_year(self):
        rules, _ = skiprules.parse_document({
            "version": 1,
            "rules": [
                {"id": "film", "type": "skip_item", "movie_title": "example movie", "year": 1999,
                 "reason": "credits"},
            ],
        })
        matched = skiprules.select(rules["rules"], _movie())
        self.assertEqual(["film"], [rule["id"] for rule in matched])

    def test_episode_guids_come_from_the_episode(self):
        class _Guid(object):
            def __init__(self, gid):
                self.id = gid

        class _Item(object):
            ratingKey = "12345"
            type = "episode"
            guid = "plex://episode/abc"
            guids = [_Guid("imdb://tt0000002"), _Guid("tvdb://55")]
            grandparentTitle = "Example Show"
            parentIndex = 1
            index = 2
            title = "Episode"
            year = 2020
            duration = 1000
            server = None

        ident = skiprules.identity(_Item())
        self.assertEqual({"tt0000002"}, ident["imdb"])
        self.assertEqual({"55"}, ident["tvdb"])
        self.assertNotIn("tt0000001", ident["imdb"])


class RangeTests(unittest.TestCase):
    def _watch(self, ranges, lead=1000):
        for rule in ranges:
            rule.setdefault("plex_rating_key", "12345")
        parsed, errors = skiprules.parse_document({"version": 1, "rules": ranges})
        self.assertFalse(errors)
        merged = skiprules.merge_ranges(parsed["rules"])
        return skiprules.Watch(merged, lead), merged

    def test_normal_play_seeks_once_until_the_playhead_leaves(self):
        watch, _ = self._watch([_rule(start_ms=10_000, end_ms=20_000)])
        self.assertIsNone(watch.consider(0, 100_000))
        entered = watch.consider(9_000, 100_000)
        self.assertEqual("seek", entered["action"])
        self.assertEqual(20_000, entered["to_ms"])
        self.assertIsNone(watch.consider(12_000, 100_000))
        self.assertIsNone(watch.consider(19_999, 100_000))

    def test_seek_back_can_skip_again(self):
        watch, _ = self._watch([_rule(start_ms=10_000, end_ms=20_000)])
        self.assertEqual(20_000, watch.consider(15_000, 100_000)["to_ms"])
        self.assertIsNone(watch.consider(21_000, 100_000))
        self.assertEqual(20_000, watch.consider(9_500, 100_000)["to_ms"])

    def test_resume_inside_the_range(self):
        watch, _ = self._watch([_rule(start_ms=10_000, end_ms=20_000)])
        decision = watch.consider(15_000, 100_000)
        self.assertEqual({"action": "seek", "to_ms": 20_000, "ids": ["rule-1"]}, decision)

    def test_overlapping_ranges_merge(self):
        watch, merged = self._watch([
            _rule(id="a", start_ms=10_000, end_ms=20_000),
            _rule(id="b", start_ms=15_000, end_ms=25_000),
            _rule(id="c", start_ms=40_000, end_ms=50_000),
        ])
        self.assertEqual(2, len(merged))
        self.assertEqual(10_000, merged[0]["start_ms"])
        self.assertEqual(25_000, merged[0]["end_ms"])
        self.assertEqual(["a", "b"], merged[0]["ids"])
        self.assertEqual(25_000, watch.consider(12_000, 100_000)["to_ms"])

    def test_range_to_the_end_of_the_file(self):
        watch, _ = self._watch([_rule(start_ms=1_700_000, end_ms=1_800_000)])
        self.assertEqual("end", watch.consider(1_750_000, 1_800_000)["action"])
        open_end, _ = self._watch([_rule(start_ms=1_000, end_ms=None)])
        self.assertEqual("end", open_end.consider(2_000, 50_000)["action"])


class PlaybackDecisionTests(unittest.TestCase):
    def setUp(self):
        skiprules.reset()
        self._dir = tempfile.TemporaryDirectory()
        skiprules.set_rules_path(os.path.join(self._dir.name, "skip_rules.json"))
        self.toasts = []
        skiprules.set_toast_hook(self.toasts.append)
        skiprules.set_override({
            "enabled": True,
            "lead_ms": 1000,
            "toast": True,
        })

    def tearDown(self):
        skiprules.reset()
        self._dir.cleanup()

    def _write(self, document, mtime=None):
        path = skiprules.rules_path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_file_load_skips_episode_and_movie(self):
        self._write({
            "version": 1,
            "updated_at": "2026-01-01T00:00:00Z",
            "rules": [
                {"id": "ep", "type": "skip_item", "plex_rating_key": "12345", "reason": "recap"},
                {"id": "film", "type": "skip_item", "plex_rating_key": "999", "reason": "trailer"},
            ],
        })
        episode = skiprules.prepare(_episode(), has_next=True)
        self.assertEqual("next", episode["action"])
        self.assertIn("recap", self.toasts[-1])
        movie = skiprules.prepare(_movie(), has_next=True)
        self.assertEqual("stop", movie["action"])
        alone = skiprules.prepare(_episode(), has_next=False)
        self.assertEqual("stop", alone["action"])

    def test_missing_file_means_no_rules(self):
        self.assertEqual("play", skiprules.prepare(_episode(), has_next=True)["action"])
        self.assertEqual([], skiprules.load()["rules"])

    def test_reload_when_mtime_changes(self):
        path = self._write({
            "version": 1,
            "rules": [{"id": "first", "type": "skip_item", "plex_rating_key": "12345", "reason": "first"}],
        })
        first = skiprules.prepare(_episode(), has_next=False)
        self.assertEqual("first", first["reason"])
        stamp = os.stat(path).st_mtime
        self._write({
            "version": 1,
            "rules": [{"id": "second", "type": "skip_item", "plex_rating_key": "12345", "reason": "second"}],
        }, mtime=stamp)
        held = skiprules.prepare(_episode(), has_next=False)
        self.assertEqual("first", held["reason"])
        os.utime(path, (stamp + 5, stamp + 5))
        changed = skiprules.prepare(_episode(), has_next=False)
        self.assertEqual("second", changed["reason"])

    def test_forced_reload_reads_even_when_mtime_is_unchanged(self):
        path = self._write({
            "version": 1,
            "rules": [{"id": "first", "type": "skip_item", "plex_rating_key": "12345", "reason": "first"}],
        })
        skiprules.load()
        stamp = os.stat(path).st_mtime
        self._write({
            "version": 1,
            "rules": [{"id": "second", "type": "skip_item", "plex_rating_key": "12345", "reason": "second"}],
        }, mtime=stamp)
        self.assertEqual(["first"], [rule["id"] for rule in skiprules.load()["rules"]])
        skiprules.reload_now()
        self.assertEqual(["second"], [rule["id"] for rule in skiprules.load()["rules"]])

    def test_bad_file_keeps_the_last_good_copy(self):
        path = self._write({
            "version": 1,
            "rules": [{"id": "kept", "type": "skip_item", "plex_rating_key": "12345", "reason": "kept"}],
        })
        self.assertEqual("kept", skiprules.prepare(_episode(), has_next=False)["reason"])
        stamp = os.stat(path).st_mtime + 5
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        os.utime(path, (stamp, stamp))
        decision = skiprules.prepare(_episode(), has_next=False)
        self.assertEqual("kept", decision["reason"])
        os.utime(path, (stamp + 5, stamp + 5))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 2, "rules": []}, handle)
        still = skiprules.prepare(_episode(), has_next=False)
        self.assertEqual("kept", still["reason"])

    def test_disabled_does_not_skip(self):
        self._write({
            "version": 1,
            "rules": [{"id": "ep", "type": "skip_item", "plex_rating_key": "12345", "reason": "recap"}],
        })
        skiprules.set_override({
            "enabled": False,
            "lead_ms": 1000,
            "toast": True,
        })
        self.assertEqual("play", skiprules.prepare(_episode(), has_next=True)["action"])
        self.assertIsNone(skiprules.poll(15_000, 100_000, now=10_000))


if __name__ == "__main__":
    unittest.main()
