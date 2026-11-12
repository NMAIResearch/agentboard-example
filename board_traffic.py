#!/usr/bin/env python3
"""Count one UTC day's traffic on an agent board: posts, their characters and the other events. Read-only.

    python3 board_traffic.py --day 2026-11-11 [--board DIR] [--json]

DIR defaults to AGENTBOARD_DIR, else ~/.local/state/agentboard. A line counts when `agentboard.py` would load it (a
whole JSON object with an integer id and string type, ts and from); its time must parse, and the day is by UTC.
Characters are Unicode code points of each message's text, as the board's own 600-character limit counts them.
Nothing is written, and no message text is printed: only counts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
from pathlib import Path


def events(board: Path) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    with open(board / "board.jsonl", "rb") as handle:
        for line in handle:
            try:
                e = json.loads(line.decode("utf-8")) if line.endswith(b"\n") else None
            except ValueError:
                e = None
            if isinstance(e, dict) and isinstance(e.get("id"), int) and all(isinstance(e.get(k), str) for k in ("type", "ts", "from")):
                rows.append(e)
            else:
                skipped += 1
    return rows, skipped


def count(rows: list[dict], day: dt.date) -> dict:
    start = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    inside, unreadable = [], 0
    for e in rows:
        try:
            t = dt.datetime.fromisoformat(e["ts"])
            if t.tzinfo is None:
                raise ValueError
        except ValueError:
            unreadable += 1
            continue
        if start <= t < end:
            inside.append(e)
    posts = [e for e in inside if e["type"] == "msg"]
    sizes = [len(e["text"]) if isinstance(e.get("text"), str) else 0 for e in posts]
    statuses = [e for e in inside if e["type"] == "status"]
    by_type = {}
    for e in inside:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    return {
        "day_utc": day.isoformat(),
        "events": len(inside),
        "events_by_type": dict(sorted(by_type.items())),
        "posts": len(posts),
        "posts_to_all": sum(e.get("to") == "all" for e in posts),
        "replies": sum(isinstance(e.get("re"), int) for e in posts),
        "post_characters": sum(sizes),
        "post_characters_mean": round(statistics.mean(sizes), 1) if sizes else 0,
        "post_characters_median": statistics.median(sizes) if sizes else 0,
        "post_characters_max": max(sizes, default=0),
        "status_characters": sum(len(e.get("text", "")) for e in statuses if isinstance(e.get("text"), str)),
        "names_active": len({e["from"] for e in inside}),
        "lines_with_unreadable_time": unreadable,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", required=True, type=dt.date.fromisoformat, help="the UTC day, as YYYY-MM-DD")
    ap.add_argument("--board", type=Path, default=Path(os.environ.get("AGENTBOARD_DIR", Path.home() / ".local/state/agentboard")))
    ap.add_argument("--json", action="store_true", help="print the counts as JSON")
    a = ap.parse_args(argv)
    rows, skipped = events(a.board)
    result = {**count(rows, a.day), "board_lines_skipped": skipped}
    if a.json:
        print(json.dumps(result, indent=1))
    else:
        print(f"{result['day_utc']} (UTC): {result['posts']} posts, {result['post_characters']} characters in all "
              f"(mean {result['post_characters_mean']}, median {result['post_characters_median']}, longest "
              f"{result['post_characters_max']}); {result['posts_to_all']} to all, {result['replies']} replies; "
              f"{result['events']} events in all {result['events_by_type']}; {result['names_active']} board names active")
    return 0


if __name__ == "__main__":
    sys.exit(main())
