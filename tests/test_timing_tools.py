# coding=utf-8
"""Offline checks for the log summary and the library-size estimate."""
from __future__ import absolute_import

import unittest

from tools.crawl_feasibility import crawl, render
from tools.parse_timings import parse_lines, render_report

_LOG = """
script.plexmod: TIMING SPAN id=s1 name=open.show phase=begin parent=-
script.plexmod: TIMING REQ span=s1 method=GET endpoint={server}/library/metadata/{id} status=200 bytes=10 ttfb_ms=180 total_ms=400 cache=miss thread=MainThread ui_blocked=1
script.plexmod: TIMING SPAN id=s1 name=open.show phase=first ms=500
script.plexmod: TIMING SPAN id=s1 name=open.show phase=full ms=5000
script.plexmod: TIMING SPAN id=s2 name=open.show phase=begin parent=-
script.plexmod: TIMING REQ span=s2 method=GET endpoint={server}/library/metadata/{id} status=200 bytes=10 ttfb_ms=20 total_ms=30 cache=hit thread=MainThread ui_blocked=1
script.plexmod: TIMING SPAN id=s2 name=open.show phase=first ms=100
script.plexmod: TIMING SPAN id=s2 name=open.show phase=full ms=1000
script.plexmod: TIMING ART span=s1 cache=miss ms=800 endpoint={server}/photo/:/transcode
script.plexmod: TIMING ART span=s1 cache=hit ms=0 endpoint={server}/photo/:/transcode
script.plexmod: TIMING PLAY id=s3 phase=decision mode=direct ms=220
"""

_SECTIONS = b"""<MediaContainer size="2">
  <Directory key="1" type="movie" title="Films" />
  <Directory key="2" type="show" title="Series" />
</MediaContainer>"""

_MOVIES = b"""<MediaContainer size="1" totalSize="1">
  <Video ratingKey="9" type="movie" title="Example" thumb="/library/metadata/9/thumb/1" art="/library/metadata/9/art/1" />
</MediaContainer>"""

_SHOWS = b"""<MediaContainer size="1" totalSize="1">
  <Directory ratingKey="3" type="show" title="Example Show" thumb="/library/metadata/3/thumb/1" />
</MediaContainer>"""

_SEASONS = b"""<MediaContainer size="1" totalSize="1">
  <Directory ratingKey="4" type="season" title="Example Season" thumb="/library/metadata/4/thumb/1" />
</MediaContainer>"""

_EPISODES = b"""<MediaContainer size="1" totalSize="1">
  <Video ratingKey="5" type="episode" title="Example Episode" thumb="/library/metadata/5/thumb/1" />
</MediaContainer>"""


class ParseTimingsTest(unittest.TestCase):
    def test_report_has_percentiles_hit_rate_and_slowest(self):
        report = render_report(parse_lines(_LOG.splitlines()), top=5)
        self.assertIn("open.show  n=2", report)
        self.assertIn("first median=300 p90=460", report)
        self.assertIn("full median=3000 p90=4600", report)
        self.assertIn("cache hits: 1 (50%)", report)
        self.assertIn("400ms GET {server}/library/metadata/{id}", report)
        self.assertIn("art: n=2 hits=1", report)
        self.assertIn("decision mode=direct", report)


class CrawlFeasibilityTest(unittest.TestCase):
    def test_dashboard_counts_children_and_hides_the_token(self):
        clock = [0.0]
        slept = []

        def now():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds
            slept.append(seconds)

        calls = []

        def get(url, token):
            calls.append((url, token))
            if url.endswith("/library/sections"):
                return 200, _SECTIONS
            if "/library/sections/1/all" in url:
                return 200, _MOVIES
            if "/library/sections/2/all" in url:
                return 200, _SHOWS
            if "/library/metadata/3/children" in url:
                return 200, _SEASONS
            if "/library/metadata/4/children" in url:
                return 200, _EPISODES
            return 404, b""

        stats = crawl(
            "http://pms.example:32400", "sekret-token", scope="dashboard",
            rate=2.0, get=get, sleep=sleep, now=now,
        )
        text = render(stats)
        self.assertEqual(stats["sections"], 2)
        self.assertEqual(stats["items"], 2)
        self.assertEqual(stats["children"], 2)
        self.assertEqual(stats["art_urls"], 10)
        self.assertGreater(len(slept), 0)
        self.assertNotIn("sekret-token", text)
        self.assertNotIn("pms.example", text)
        self.assertNotIn("Example", text)
        self.assertTrue(all(token == "sekret-token" for _url, token in calls))
        self.assertTrue(all("sekret-token" not in url for url, _token in calls))

    def test_max_requests_stops_the_walk(self):
        def get(url, token):
            if url.endswith("/library/sections"):
                return 200, _SECTIONS
            return 200, _MOVIES

        stats = crawl(
            "http://server.example:32400", "t", scope="full", rate=0,
            max_requests=2, get=get, sleep=lambda _s: None, now=lambda: 0,
        )
        self.assertEqual(stats["stopped"], "max_requests")
        self.assertLessEqual(stats["requests"], 2)
