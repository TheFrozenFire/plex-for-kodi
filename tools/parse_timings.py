# coding=utf-8
"""Summarize TIMING lines from a Kodi log.

Usage:
    python3 tools/parse_timings.py kodi.log
    python3 tools/parse_timings.py kodi.log --top 15

Reads a log file (or stdin when the path is "-") and prints, per transition,
how many times it ran and the median and 90th percentile of time-to-first and
time-to-full. It also lists the slowest requests and the cache hit rate.
Nothing here contacts a server.
"""
from __future__ import absolute_import

import argparse
import re
import sys

_SPAN_BEGIN = re.compile(r"TIMING SPAN id=(\S+) name=(\S+) phase=begin\b")
_SPAN_MARK = re.compile(r"TIMING SPAN id=(\S+) name=(\S+) phase=(first|full) ms=(\d+(?:\.\d+)?)")
_REQ = re.compile(
    r"TIMING REQ span=(\S+) method=(\S+) endpoint=(\S+) status=(\S+) "
    r"bytes=(\S+) ttfb_ms=(\S+) total_ms=(\d+(?:\.\d+)?) cache=(\S+)"
)
_ART = re.compile(r"TIMING ART span=(\S+) cache=(\S+) ms=(\d+(?:\.\d+)?)")
_PLAY = re.compile(r"TIMING PLAY id=(\S+) phase=(\S+)(?: mode=(\S+))? ms=(\d+(?:\.\d+)?)")


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * weight


def _fmt(value):
    if value is None:
        return "-"
    return "{0:.0f}".format(value)


def parse_lines(lines):
    spans = {}
    requests = []
    art = []
    play = []
    for line in lines:
        match = _SPAN_BEGIN.search(line)
        if match:
            span_id, name = match.group(1), match.group(2)
            spans.setdefault(span_id, {"id": span_id, "name": name, "first": None, "full": None})
            continue
        match = _SPAN_MARK.search(line)
        if match:
            span_id, name, phase, ms = match.groups()
            entry = spans.setdefault(span_id, {"id": span_id, "name": name, "first": None, "full": None})
            entry["name"] = name
            entry[phase] = float(ms)
            continue
        match = _REQ.search(line)
        if match:
            requests.append({
                "span": match.group(1),
                "method": match.group(2),
                "endpoint": match.group(3),
                "status": match.group(4),
                "bytes": match.group(5),
                "ttfb_ms": float(match.group(6)),
                "total_ms": float(match.group(7)),
                "cache": match.group(8),
            })
            continue
        match = _ART.search(line)
        if match:
            art.append({"span": match.group(1), "cache": match.group(2), "ms": float(match.group(3))})
            continue
        match = _PLAY.search(line)
        if match:
            play.append({
                "span": match.group(1),
                "phase": match.group(2),
                "mode": match.group(3) or "",
                "ms": float(match.group(4)),
            })
    return {"spans": spans, "requests": requests, "art": art, "play": play}


def render_report(parsed, top=10):
    by_name = {}
    for span in parsed["spans"].values():
        by_name.setdefault(span["name"], []).append(span)

    lines = []
    if not by_name:
        lines.append("transitions: none")
    else:
        lines.append("transitions:")
        for name in sorted(by_name):
            group = by_name[name]
            firsts = [item["first"] for item in group if item["first"] is not None]
            fulls = [item["full"] for item in group if item["full"] is not None]
            lines.append(
                "  {0}  n={1}  first median={2} p90={3}  full median={4} p90={5}".format(
                    name,
                    len(group),
                    _fmt(_percentile(firsts, 50)),
                    _fmt(_percentile(firsts, 90)),
                    _fmt(_percentile(fulls, 50)),
                    _fmt(_percentile(fulls, 90)),
                )
            )

    requests = parsed["requests"]
    hits = sum(1 for item in requests if item["cache"] == "hit")
    if requests:
        rate = 100.0 * hits / len(requests)
        lines.append("requests: {0}  cache hits: {1} ({2:.0f}%)".format(len(requests), hits, rate))
    else:
        lines.append("requests: 0")

    slowest = sorted(requests, key=lambda item: item["total_ms"], reverse=True)[:top]
    if slowest:
        lines.append("slowest requests:")
        names = {span_id: span["name"] for span_id, span in parsed["spans"].items()}
        for item in slowest:
            where = names.get(item["span"], item["span"])
            lines.append(
                "  {0:.0f}ms {1} {2} {3} cache={4} span={5}".format(
                    item["total_ms"], item["method"], item["endpoint"], item["status"], item["cache"], where
                )
            )

    art = parsed["art"]
    if art:
        misses = [item["ms"] for item in art if item["cache"] == "miss"]
        hits = sum(1 for item in art if item["cache"] == "hit")
        timeouts = sum(1 for item in art if item["cache"] == "timeout")
        lines.append(
            "art: n={0} hits={1} timeouts={2} miss median={3} p90={4}".format(
                len(art), hits, timeouts, _fmt(_percentile(misses, 50)), _fmt(_percentile(misses, 90))
            )
        )

    if parsed["play"]:
        lines.append("playback phases:")
        for item in parsed["play"]:
            mode = " mode={0}".format(item["mode"]) if item["mode"] else ""
            lines.append("  {0} {1}{2} {3:.0f}ms".format(item["span"], item["phase"], mode, item["ms"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarize PM4K TIMING lines from a Kodi log.")
    parser.add_argument("log", help="Path to kodi.log, or - for stdin")
    parser.add_argument("--top", type=int, default=10, help="How many slow requests to print")
    args = parser.parse_args(argv)
    if args.log == "-":
        source = sys.stdin
    else:
        source = open(args.log, "r", errors="replace")
    try:
        report = render_report(parse_lines(source), top=args.top)
    finally:
        if source is not sys.stdin:
            source.close()
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
