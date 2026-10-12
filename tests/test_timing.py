# coding=utf-8
"""The opt-in timing helper stays silent unless it is explicitly turned on."""
from __future__ import absolute_import

import os

from kodienv import ENV

from lib import timing

from .base import KodiTestCase


class TimingHelperTest(KodiTestCase):
    def setUp(self):
        super(TimingHelperTest, self).setUp()
        self._previous = os.environ.pop("PM4K_TIMING", None)
        timing.reset()

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("PM4K_TIMING", None)
        else:
            os.environ["PM4K_TIMING"] = self._previous
        timing.reset()
        super(TimingHelperTest, self).tearDown()

    def test_silent_when_nothing_enables_it(self):
        with timing.timed("GET /library/sections?X-Plex-Token=secret"):
            pass
        self.assertFalse(ENV.logged("TIMING"))
        self.assertFalse(ENV.logged("secret"))

    def test_env_var_logs_duration_and_redacts_the_token(self):
        os.environ["PM4K_TIMING"] = "1"
        with timing.timed("GET /library/sections?X-Plex-Token=secret"):
            pass
        self.assertTrue(ENV.logged("TIMING"))
        self.assertTrue(ENV.logged("GET /library/sections"))
        self.assertTrue(ENV.logged("X-Plex-Token=****"))
        self.assertFalse(ENV.logged("secret"))

    def test_env_zero_forces_it_off_even_if_the_setting_is_on(self):
        os.environ["PM4K_TIMING"] = "0"
        ENV.settings["timing_log"] = "true"
        timing.reset()
        with timing.timed("should-not-log"):
            pass
        self.assertFalse(ENV.logged("TIMING"))

    def test_hidden_setting_enables_it(self):
        ENV.settings["timing_log"] = "true"
        timing.reset()
        with timing.timed("hubs"):
            pass
        self.assertTrue(ENV.logged("TIMING hubs"))

    def test_exceptions_still_propagate(self):
        os.environ["PM4K_TIMING"] = "1"

        def boom():
            with timing.timed("failing"):
                raise RuntimeError("nope")

        self.assertRaises(RuntimeError, boom)
        self.assertTrue(ENV.logged("TIMING failing"))

    def test_decorator_names_the_call(self):
        os.environ["PM4K_TIMING"] = "1"

        @timing.timed_call(lambda args, kwargs: "decorated {0}".format(args[0]))
        def fetch(path):
            return path

        self.assertEqual(fetch("/hubs"), "/hubs")
        self.assertTrue(ENV.logged("TIMING decorated /hubs"))

    def test_request_line_redacts_host_token_and_ids(self):
        os.environ["PM4K_TIMING"] = "1"

        class Response(object):
            status_code = 200
            from_cache = False
            headers = {"Content-Length": "12"}
            _content = b"hello there!"
            elapsed = None

        def fetch():
            return Response()

        result = timing.observe_http(
            "GET",
            "http://pms.example:32400/library/metadata/4242?X-Plex-Token=sekret&X-Plex-Container-Size=10",
            fetch,
        )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(ENV.logged("TIMING REQ"))
        self.assertTrue(ENV.logged("endpoint={server}/library/metadata/{id}"))
        self.assertTrue(ENV.logged("X-Plex-Token=****"))
        self.assertTrue(ENV.logged("X-Plex-Container-Size=10"))
        self.assertTrue(ENV.logged("status=200"))
        self.assertTrue(ENV.logged("bytes=12"))
        self.assertTrue(ENV.logged("cache=miss"))
        self.assertTrue(ENV.logged("ui_blocked=1"))
        self.assertFalse(ENV.logged("sekret"))
        self.assertFalse(ENV.logged("pms.example"))
        self.assertFalse(ENV.logged("4242"))

    def test_hex_catalog_id_is_fully_redacted(self):
        endpoint = timing.redact_endpoint(
            "https://discover.provider.plex.tv/library/metadata/1abc2345def67890abcd1234"
        )
        self.assertEqual(endpoint, "discover.provider.plex.tv/library/metadata/{id}")
        self.assertNotIn("1abc2345", endpoint)
        self.assertNotIn("abc2345", endpoint)
        self.assertNotIn("abcd1234", endpoint)

    def test_request_cache_hit_and_span_id(self):
        os.environ["PM4K_TIMING"] = "1"

        class Response(object):
            status_code = 200
            from_cache = True
            headers = {}
            _content = b"ab"
            elapsed = None

        with timing.span("open.show") as opened:
            timing.observe_http("GET", "https://plex.tv/users/account", lambda: Response())
            opened.mark("first")
        self.assertTrue(ENV.logged("name=open.show phase=begin"))
        self.assertTrue(ENV.logged("name=open.show phase=first"))
        self.assertTrue(ENV.logged("name=open.show phase=full"))
        self.assertTrue(ENV.logged("span={0}".format(opened.id)))
        self.assertTrue(ENV.logged("endpoint=plex.tv/users/account"))
        self.assertTrue(ENV.logged("cache=hit"))

    def test_a_finished_span_still_names_an_adopted_request(self):
        os.environ["PM4K_TIMING"] = "1"

        class Response(object):
            status_code = 200
            from_cache = False
            headers = {}
            _content = b"ab"
            elapsed = None

        opened = timing.begin("open.season")
        timing.finish(opened)
        self.assertTrue(ENV.logged("name=open.season phase=full"))
        with timing.adopt(opened):
            timing.observe_http(
                "GET",
                "http://pms.example:32400/library/metadata/16?includeMarkers=1",
                lambda: Response(),
            )
        self.assertTrue(ENV.logged("span={0}".format(opened.id)))
        self.assertTrue(ENV.logged("endpoint={server}/library/metadata/{id}"))
        timing.observe_http("GET", "http://pms.example/library/sections", lambda: Response())
        later = [message for message, _level in ENV.log_lines if "library/sections" in message]
        self.assertTrue(later)
        self.assertTrue(all("span=-" in message for message in later))

    def test_idle_thread_opens_its_own_span_until_the_task_finishes(self):
        os.environ["PM4K_TIMING"] = "1"

        class Task(object):
            pass

        task = Task()
        with timing.span("return.home"):
            self.assertIsNone(timing.begin_if_idle("home.hubs"))
        opened = timing.begin_if_idle("home.hubs")
        self.assertTrue(getattr(opened, "real", False))
        timing.hold_until_tasks(opened, [task])
        self.assertFalse(ENV.logged("name=home.hubs phase=full"))
        timing.release_task(task)
        self.assertTrue(ENV.logged("name=home.hubs phase=full"))

    def test_full_waits_for_tasks(self):
        os.environ["PM4K_TIMING"] = "1"

        class Task(object):
            pass

        task = Task()
        with timing.span("open.detail") as opened:
            opened.mark("first")
            opened.after_tasks([task])
        self.assertFalse(ENV.logged("phase=full"))
        timing.release_task(task)
        self.assertTrue(ENV.logged("name=open.detail phase=full"))

    def test_off_skips_http_and_art_probe(self):
        calls = []
        timing.set_art_probe(lambda url: calls.append(url) or False)
        timing.observe_http("GET", "http://pms.example/library/sections", lambda: calls.append("http"))
        timing.watch_art("http://pms.example/photo")
        self.assertEqual(calls, ["http"])
        self.assertFalse(ENV.logged("TIMING"))
        self.assertFalse(ENV.logged("pms.example"))

    def test_art_hit_and_later_miss(self):
        os.environ["PM4K_TIMING"] = "1"
        state = {"ready": True}

        def probe(url):
            return state["ready"]

        timing.set_art_probe(probe)
        timing.watch_art("https://example.test/photo/a")
        self.assertTrue(ENV.logged("TIMING ART"))
        self.assertTrue(ENV.logged("cache=hit"))
        state["ready"] = False
        timing.watch_art("https://example.test/photo/b")
        self.assertFalse(ENV.logged("endpoint={server}/photo/b"))
        state["ready"] = True
        timing.poll_art()
        self.assertTrue(ENV.logged("cache=miss"))
        self.assertTrue(ENV.logged("endpoint={server}/photo/b"))

    def test_texture_database_lookup(self):
        import os as os_mod
        import sqlite3
        import tempfile

        directory = tempfile.mkdtemp()
        path = os_mod.path.join(directory, "Textures13.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE texture (id integer primary key, url text, cachedurl text)")
        conn.execute(
            "INSERT INTO texture (url, cachedurl) VALUES (?, ?)",
            ("https://example.test/poster", "a/abc.jpg"),
        )
        conn.commit()
        conn.close()
        original = timing._translate_special
        timing._translate_special = lambda special: path
        try:
            self.assertTrue(timing._textures_db_has("https://example.test/poster"))
            self.assertFalse(timing._textures_db_has("https://example.test/missing"))
        finally:
            timing._translate_special = original
