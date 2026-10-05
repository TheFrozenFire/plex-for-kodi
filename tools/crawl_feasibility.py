# coding=utf-8
"""Estimate what a metadata mirror of a Plex library would cost.

The add-on does not call this. Run it from a machine that can reach the
server, with the token in the environment (it is sent as a header and is not
printed).

    PLEX_URL=http://SERVER:32400 PLEX_TOKEN=... python3 tools/crawl_feasibility.py --scope dashboard
    PLEX_URL=http://SERVER:32400 PLEX_TOKEN=... python3 tools/crawl_feasibility.py --scope full --fetch-art

``dashboard`` walks the first page of each library section, then one page of
children for those shows (seasons, then episodes). That is a stand-in for
"prefetch what a home screen can reach", not a literal copy of the hub rows.

``full`` pages through every item and its children. Both modes are serial and
rate-limited (default 2 requests/second) and stop at ``--max-requests``.

Artwork bytes are counted only with ``--fetch-art``. Otherwise the report
counts the transcode URLs the UI would request at the home poster size
(244x361) and the 16:9 size (532x299). Those are the unscaled cell sizes in
``HomeWindow``.

Change detection, so a later mirror could stay current without a full walk:

- Items and directories carry ``updatedAt`` (unix seconds). A section query
  can filter with ``updatedAt>>=<unix>`` (the same operator Plex uses for
  "greater than or equal") and return only what changed.
- ``GET /library/recentlyAdded`` is a short list, not a full delta.
- ``/:/websockets/notifications`` pushes ``library.new``, ``library.on.deck``,
  ``timeline``, and ``activity`` notifications. Those are hints to refetch the
  rating keys they mention. ``updatedAt`` is the backstop after a disconnect.
"""
from __future__ import absolute_import

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

_CHILD_TYPES = ("show", "artist", "season", "album")
_ART_SIZES = ((244, 361), (532, 299))
_PAGE = 50


class CrawlError(RuntimeError):
    pass


class Limiter(object):
    def __init__(self, rate, sleep, now):
        self.interval = (1.0 / rate) if rate and rate > 0 else 0
        self.sleep = sleep
        self.now = now
        self._next = 0

    def wait(self):
        if self.interval <= 0:
            return
        current = self.now()
        if current < self._next:
            self.sleep(self._next - current)
        self._next = self.now() + self.interval


def _transcode_url(base, path, width, height):
    return "{0}/photo/:/transcode?{1}".format(
        base.rstrip("/"),
        urlencode({
            "width": width,
            "height": height,
            "minSize": 1,
            "upscale": 1,
            "url": path,
        }),
    )


def crawl(base_url, token, scope="dashboard", rate=2.0, max_requests=2000,
          fetch_art=False, page_size=_PAGE, get=None, sleep=time.sleep, now=time.monotonic):
    if scope not in ("dashboard", "full"):
        raise CrawlError("scope must be dashboard or full")
    if not base_url or not token:
        raise CrawlError("base URL and token are required")
    base = base_url.rstrip("/")
    getter = get or _requests_get
    limiter = Limiter(rate, sleep, now)
    stats = {
        "scope": scope,
        "requests": 0,
        "metadata_bytes": 0,
        "art_requests": 0,
        "art_bytes": 0,
        "sections": 0,
        "items": 0,
        "children": 0,
        "art_urls": 0,
        "stopped": "",
        "by_type": {},
    }
    seen_art = set()
    started = now()

    def fetch(path, params=None):
        if stats["requests"] >= max_requests:
            stats["stopped"] = "max_requests"
            return None
        limiter.wait()
        query = dict(params or {})
        url = base + path
        if query:
            url = url + "?" + urlencode(query)
        status, body = getter(url, token)
        stats["requests"] += 1
        stats["metadata_bytes"] += len(body or b"")
        if status == 401:
            raise CrawlError("server rejected the token")
        if status < 200 or status >= 300:
            raise CrawlError("request failed with status {0}".format(status))
        try:
            return ET.fromstring(body)
        except ET.ParseError:
            raise CrawlError("response was not XML")

    container = fetch("/library/sections")
    if container is None:
        stats["elapsed_s"] = now() - started
        return stats

    for section in list(container):
        if stats["stopped"]:
            break
        key = section.attrib.get("key")
        if not key:
            continue
        stats["sections"] += 1
        _walk_container(
            fetch, stats, "/library/sections/{0}/all".format(key),
            scope=scope, depth=0, page_size=page_size, one_page=(scope == "dashboard"),
            seen_art=seen_art,
        )

    stats["art_urls"] = len(seen_art)
    if fetch_art and not stats["stopped"]:
        for path, width, height in sorted(seen_art):
            if stats["requests"] >= max_requests:
                stats["stopped"] = "max_requests"
                break
            limiter.wait()
            url = _transcode_url(base, path, width, height)
            status, body = getter(url, token)
            stats["requests"] += 1
            stats["art_requests"] += 1
            if status == 200:
                stats["art_bytes"] += len(body or b"")
    stats["elapsed_s"] = now() - started
    return stats


def _walk_container(fetch, stats, path, scope, depth, page_size, one_page, seen_art):
    start = 0
    while not stats["stopped"]:
        container = fetch(path, {
            "X-Plex-Container-Start": start,
            "X-Plex-Container-Size": page_size,
        })
        if container is None:
            return
        elements = list(container)
        if not elements:
            return
        for element in elements:
            kind = element.attrib.get("type") or element.tag
            stats["by_type"][kind] = stats["by_type"].get(kind, 0) + 1
            if depth == 0:
                stats["items"] += 1
            else:
                stats["children"] += 1
            for attr in ("thumb", "art", "grandparentThumb", "parentThumb"):
                value = element.attrib.get(attr) or ""
                if value.startswith("/"):
                    for width, height in _ART_SIZES:
                        seen_art.add((value, width, height))
            rating = element.attrib.get("ratingKey")
            if rating and kind in _CHILD_TYPES and depth < 2 and not stats["stopped"]:
                child_one_page = one_page or scope == "dashboard"
                _walk_container(
                    fetch, stats, "/library/metadata/{0}/children".format(rating),
                    scope=scope, depth=depth + 1, page_size=page_size,
                    one_page=child_one_page, seen_art=seen_art,
                )
        size = len(elements)
        total = int(container.attrib.get("totalSize") or size)
        start += size
        if one_page or start >= total or size < page_size:
            return


def _requests_get(url, token):
    import requests
    response = requests.get(
        url,
        headers={"X-Plex-Token": token, "Accept": "application/xml"},
        timeout=20,
    )
    return response.status_code, response.content or b""


def render(stats):
    types = ", ".join("{0}={1}".format(key, stats["by_type"][key]) for key in sorted(stats["by_type"]))
    lines = [
        "scope: {0}".format(stats["scope"]),
        "sections: {0}".format(stats["sections"]),
        "items: {0}".format(stats["items"]),
        "children: {0}".format(stats["children"]),
        "metadata requests: {0}".format(stats["requests"] - stats["art_requests"]),
        "metadata bytes: {0}".format(stats["metadata_bytes"]),
        "art urls: {0}".format(stats["art_urls"]),
        "art requests: {0}".format(stats["art_requests"]),
        "art bytes: {0}".format(stats["art_bytes"]),
        "elapsed seconds: {0:.1f}".format(stats["elapsed_s"]),
        "by type: {0}".format(types or "-"),
    ]
    if stats["stopped"]:
        lines.append("stopped: {0}".format(stats["stopped"]))
    if not stats["art_requests"]:
        lines.append("art bytes are 0 until --fetch-art is passed")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Estimate a Plex library mirror. Rate-limited.")
    parser.add_argument("--base-url", default=os.environ.get("PLEX_URL", ""))
    parser.add_argument("--token-env", default="PLEX_TOKEN")
    parser.add_argument("--scope", choices=("dashboard", "full"), default="dashboard")
    parser.add_argument("--rate", type=float, default=2.0, help="Max requests per second")
    parser.add_argument("--max-requests", type=int, default=2000)
    parser.add_argument("--fetch-art", action="store_true")
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "")
    try:
        stats = crawl(
            args.base_url, token, scope=args.scope, rate=args.rate,
            max_requests=args.max_requests, fetch_art=args.fetch_art,
        )
    except CrawlError as exc:
        sys.stderr.write("{0}\n".format(exc))
        return 1
    sys.stdout.write(render(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
