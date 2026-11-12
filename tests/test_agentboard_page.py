"""The page: the wider state(), the page's markup, script and colours.

Synthetic boards in temporary folders only, with the clock pinned; no existing board is read. Run from the
repository's root:

    python3 -I -B -W error::ResourceWarning tests/test_agentboard_page.py
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import agentboard  # noqa: E402

AT = dt.datetime(2026, 11, 12, 12, tzinfo=dt.timezone.utc)
MIN, HOUR = dt.timedelta(minutes=1), dt.timedelta(hours=1)


class Ids(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids[attrs["id"]] = tag


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"AGENTBOARD_DIR": str(self.directory)})
        env.start()
        self.addCleanup(env.stop)
        clock = mock.patch.object(agentboard, "now", return_value=AT)
        clock.start()
        self.addCleanup(clock.stop)
        self.assertEqual(agentboard.board_dir(), self.directory)
        self.next_id = 0

    def event(self, kind, sender, age=dt.timedelta(), **fields):
        self.next_id += 1
        return {"id": self.next_id, "ts": (AT - age).isoformat(timespec="seconds"), "type": kind, "from": sender, **fields}

    def msg(self, sender, to, text, age=dt.timedelta(), **fields):
        return self.event("msg", sender, age, to=to, text=text, **fields)

    def seed(self, *events):
        with (self.directory / "board.jsonl").open("a", encoding="utf-8") as stream:
            for e in events:
                stream.write(json.dumps(e, ensure_ascii=False) + "\n")


class NoticeTest(Board):
    def test_kinds_from_the_first_words_as_the_board_has_used_them(self):
        cases = {
            "HOLD, all, from now: no writes in the shared folder": "hold",
            "HOLD again, all, from now, about 6 minutes": "hold",
            "HOLD #12 goes on, about 40 minutes more.": "hold",
            "HOLD for a release window": "hold",
            "QUIET WINDOW for everyone, from now": "quiet",
            "EVERYTHING STOPS, every agent waits": "stop",
            "RESUME: the release is out": "resume",
            "RELEASE, all: the hold of #12 is over; write as usual.": "resume",
            "Hold released, 12:34Z (#12). Window done": "resume",
            "HOLD released at 12:40Z": "resume",
            "HOLDING pattern for the queue": None,
            "Agent-b: the hold of #12 is still on": None,
            "NOTICE, a hold is coming in a few minutes": None,
            "To all: back in ten minutes": None,
            "hold on, all": None,
            "": None,
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(agentboard.notice_kind(text), kind)
        self.assertIsNone(agentboard.notice_kind(None))

    def test_mode_follows_the_newest_notice_to_all_until_an_end(self):
        steps = [("HOLD, all, from now", "hold"), ("RESUME: done", None), ("QUIET WINDOW for everyone", "quiet"),
                 ("EVERYTHING STOPS, all five seats", "stop"), ("Hold released, 12:00Z", None),
                 ("HOLD for a push window", "hold"), ("RELEASE, all: the hold is over", None)]
        for text, kind in steps:
            with self.subTest(text=text):
                e = self.msg("manager", "all", text)
                self.seed(e)
                mode = agentboard.state()["mode"]
                if kind is None:
                    self.assertIsNone(mode)
                else:
                    self.assertEqual((mode["kind"], mode["id"], mode["from"], mode["text"]), (kind, e["id"], "manager", text))

    def test_only_messages_to_all_are_notices(self):
        self.seed(self.msg("manager", "agent-b", "HOLD, you only"), self.event("status", "manager", text="HOLD as a status"))
        s = agentboard.state()
        self.assertEqual((s["mode"], s["notices"]), (None, []))

    def test_last_six_notices_newest_first_each_clipped(self):
        long = "HOLD " + "x" * 700
        self.seed(*[self.msg("manager", "all", f"HOLD {i}" if i < 7 else long) for i in range(8)])
        s = agentboard.state()
        self.assertEqual([n["id"] for n in s["notices"]], [8, 7, 6, 5, 4, 3])
        self.assertEqual(len(s["notices"][0]["text"]), agentboard.NOTICE_TEXT)
        self.assertEqual(s["mode"]["id"], 8)


class CountTest(Board):
    def test_hour_buckets_per_seat_newest_last(self):
        ages = [dt.timedelta(), 59 * MIN + dt.timedelta(seconds=59), HOUR, 11 * HOUR + 59 * MIN + dt.timedelta(seconds=59), 12 * HOUR]
        self.seed(*[self.event("status", "alice", a, text="x") for a in ages], self.event("read", "bob", 2 * HOUR, msgs=[]))
        s = agentboard.state()
        want = [0] * 12
        want[11], want[10], want[0] = 2, 1, 1
        self.assertEqual(s["activity"]["alice"], want)
        self.assertEqual(s["activity"]["bob"], [0] * 9 + [1, 0, 0])
        self.assertEqual(s["activity_hours"], 12)

    def test_ten_minute_slots_by_type_over_six_hours(self):
        ages = [dt.timedelta(), 9 * MIN + dt.timedelta(seconds=59), 10 * MIN, 5 * HOUR + 59 * MIN + dt.timedelta(seconds=59), 6 * HOUR]
        self.seed(*[self.msg("alice", "bob", "hi", a) for a in ages], self.event("claim", "bob", 30 * MIN, path="/x"),
                  self.event("nudge-test", "bob", 0 * MIN))
        s = agentboard.state()
        want = [0] * 36
        want[35], want[34], want[0] = 2, 1, 1
        self.assertEqual(s["timeline"]["msg"], want)
        self.assertEqual(s["timeline"]["claim"].index(1), 32)
        self.assertEqual(s["timeline"]["nudge-test"][35], 1)
        self.assertEqual((s["timeline_slots"], s["timeline_slot_minutes"]), (36, 10))
        self.assertTrue(all(len(v) == 36 for v in s["timeline"].values()))

    def test_a_time_after_now_counts_as_now(self):
        self.seed(self.msg("alice", "bob", "from a fast clock", -5 * MIN))
        s = agentboard.state()
        self.assertEqual(s["activity"]["alice"][11], 1)
        self.assertEqual(s["timeline"]["msg"][35], 1)
        self.assertEqual(s["counts"]["messages_last_hour"], 1)

    def test_messages_in_the_last_hour_and_all_events(self):
        self.seed(*[self.msg("alice", "bob", "m", a) for a in (dt.timedelta(), 59 * MIN, 60 * MIN, 61 * MIN)],
                  self.event("status", "alice", dt.timedelta(), text="s"))
        s = agentboard.state()
        self.assertEqual(s["counts"], {"messages_last_hour": 3, "events": 5})
        self.assertEqual(s["now_epoch"], AT.timestamp())

    def test_a_time_that_does_not_parse_is_left_out_of_the_counts(self):
        good = self.event("status", "alice", dt.timedelta(), text="s")
        bad = {**self.event("status", "alice", dt.timedelta(), text="s"), "ts": "not a time"}
        naive = {**self.event("status", "alice", dt.timedelta(), text="s"), "ts": "2026-11-12T11:59:00"}
        counts = agentboard.activity([bad, naive, good], AT)
        self.assertEqual(counts["activity"]["alice"][11], 1)
        self.assertEqual(counts["counts"]["events"], 3)


class ThreadTest(Board):
    def test_replies_group_under_their_root_newest_thread_first(self):
        self.seed(self.msg("alice", "bob", "root", 50 * MIN), self.msg("bob", "alice", "re 1", 40 * MIN, re=1),
                  self.msg("carol", "all", "other root", 30 * MIN), self.msg("alice", "bob", "re 2", 20 * MIN, re=2),
                  self.msg("dave", "alice", "re 1 again", 10 * MIN, re=1))
        t = agentboard.state()["threads"]
        self.assertEqual([x["root"] for x in t], [1, 3])
        self.assertEqual([m["id"] for m in t[0]["msgs"]], [1, 2, 4, 5])
        self.assertEqual((t[0]["count"], t[0]["last"], t[0]["people"]), (4, (AT - 10 * MIN).isoformat(), ["alice", "bob", "dave"]))
        self.assertEqual((t[1]["count"], t[1]["people"]), (1, ["carol", "all"]))
        self.assertNotIn("order", t[0])

    def test_a_long_thread_keeps_its_first_and_last_five(self):
        self.seed(self.msg("alice", "bob", "root"), *[self.msg("bob", "alice", f"re {i}", re=i) for i in range(1, 8)])
        t = agentboard.state()["threads"]
        self.assertEqual((len(t), t[0]["count"]), (1, 8))
        self.assertEqual([m["id"] for m in t[0]["msgs"]], [1, 4, 5, 6, 7, 8])

    def test_six_messages_are_kept_whole(self):
        self.seed(self.msg("alice", "bob", "root"), *[self.msg("bob", "alice", f"re {i}", re=i) for i in range(1, 6)])
        self.assertEqual([m["id"] for m in agentboard.state()["threads"][0]["msgs"]], [1, 2, 3, 4, 5, 6])

    def test_hand_made_lines_cannot_loop_or_break_it(self):
        lines = [self.msg("alice", "bob", "points at a claim", re=99), self.msg("bob", "alice", "a", re=3),
                 self.msg("alice", "bob", "b", re=2), self.msg("carol", "all", "self", re=4), self.msg("dave", "all", "odd", re=[1])]
        t = agentboard.threads(lines)
        self.assertEqual(sorted(m["id"] for x in t for m in x["msgs"]), [1, 2, 3, 4, 5])
        self.assertEqual(sum(x["count"] for x in t), 5)

    def test_only_the_newest_twenty_five_threads(self):
        self.seed(*[self.msg("alice", "bob", f"root {i}") for i in range(30)], self.msg("bob", "alice", "late reply", re=2))
        t = agentboard.state()["threads"]
        self.assertEqual(len(t), agentboard.THREADS_SHOWN)
        self.assertEqual([x["root"] for x in t[:3]], [2, 30, 29])

    def test_other_events_are_not_threads(self):
        self.seed(self.event("status", "alice", text="s"), self.event("claim", "alice", path="/x"))
        self.assertEqual(agentboard.state()["threads"], [])


class StateTest(Board):
    def test_empty_board_new_keys_and_nothing_written(self):
        s = agentboard.state()
        self.assertEqual((s["mode"], s["notices"], s["threads"], s["activity"], s["timeline"]), (None, [], [], {}, {}))
        self.assertEqual(s["counts"], {"messages_last_hour": 0, "events": 0})
        self.assertEqual(list(self.directory.iterdir()), [])
        json.dumps(s)

    def test_the_state_route_serves_the_new_keys(self):
        self.seed(self.msg("manager", "all", "HOLD, all"))
        handler = object.__new__(agentboard.Handler)
        handler.server = argparse.Namespace(allowed_hosts={"127.0.0.1:7101"})
        handler.headers, handler.path, handler.reply = {"Host": "127.0.0.1:7101"}, "/api/state", mock.Mock()
        handler.do_GET()
        body = json.loads(handler.reply.call_args.args[2])
        self.assertEqual(handler.reply.call_args.args[0], 200)
        self.assertEqual(body["mode"]["kind"], "hold")
        for key in ("notices", "threads", "activity", "timeline", "counts", "now_epoch"):
            self.assertIn(key, body)

    def test_a_theme_in_the_address_still_gets_the_page(self):
        handler = object.__new__(agentboard.Handler)
        handler.server = argparse.Namespace(allowed_hosts={"127.0.0.1:7101"})
        handler.headers, handler.path, handler.reply = {"Host": "127.0.0.1:7101"}, "/?theme=dark", mock.Mock()
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[:2], (200, "text/html"))


class PageTest(unittest.TestCase):
    def test_the_page_names_the_board_and_shows_home_paths_short(self):
        self.assertIn("<title>Agent board</title>", agentboard.PAGE)
        self.assertIn("<h1>Agent board</h1>", agentboard.PAGE)
        self.assertNotIn("__HOME_PREFIX__", agentboard.SCRIPT)
        self.assertIn("const homePrefix = " + json.dumps(agentboard.HOME_ROOT + os.sep) + ";", agentboard.SCRIPT)

    def test_every_id_the_script_names_is_on_the_page(self):
        ids = Ids(agentboard.PAGE).ids
        named = set(re.findall(r"\$\('([a-z-]+)'\)", agentboard.SCRIPT))
        named |= {f + s for f in re.findall(r"fold\('([a-z-]+)'", agentboard.SCRIPT) for s in ("", "-summary", "-list")}
        named |= set(re.findall(r"fill\('([a-z-]+)'", agentboard.SCRIPT)) | {"view-latest", "view-threads"}
        self.assertGreater(len(named), 15)
        self.assertEqual(sorted(named - set(ids)), [])

    def test_the_script_builds_text_nodes_only(self):
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(unsafe, agentboard.SCRIPT)
        self.assertNotIn("<script>", agentboard.PAGE.replace('<script src="/app.js"></script>', ""))

    def test_the_dark_colours_cover_every_light_one_in_both_dark_scopes(self):
        light, dark = colours(agentboard.LIGHT_COLOURS), colours(agentboard.DARK_COLOURS)
        self.assertEqual(set(light), set(dark))
        self.assertEqual(agentboard.STYLE.count(agentboard.DARK_COLOURS), 2)
        self.assertIn(':root:not([data-theme="light"])', agentboard.STYLE)
        self.assertIn(':root[data-theme="dark"]', agentboard.STYLE)

    def test_text_contrast_is_at_least_4_5_to_1_in_both_themes(self):
        pairs = [("fg", "bg"), ("muted", "bg"), ("fg", "panel"), ("muted", "panel"), ("fg", "panel2"), ("muted", "panel2"),
                 ("accent", "panel"), ("accent", "panel2"), ("warn", "panel"), ("accent", "accent-bg"), ("warn", "warn-bg"),
                 ("info", "info-bg"), ("stop", "stop-bg"), ("panel", "accent")]
        for name, text in (("light", agentboard.LIGHT_COLOURS), ("dark", agentboard.DARK_COLOURS)):
            c = colours(text)
            for fg, bg in pairs:
                with self.subTest(theme=name, pair=(fg, bg)):
                    self.assertGreaterEqual(contrast(c[fg], c[bg]), 4.5)

    def test_marks_and_field_edges_reach_3_to_1_where_they_carry_meaning(self):
        dark = colours(agentboard.DARK_COLOURS)
        for k in ("c-msg", "c-read", "c-status", "c-claim", "c-release", "c-other"):
            with self.subTest(theme="dark", mark=k):
                self.assertGreaterEqual(contrast(dark[k], dark["panel"]), 3)
        for name, text in (("light", agentboard.LIGHT_COLOURS), ("dark", agentboard.DARK_COLOURS)):
            c = colours(text)
            for bg in ("panel", "panel2"):
                with self.subTest(theme=name, field_on=bg):
                    self.assertGreaterEqual(contrast(c["field"], c[bg]), 3)

    def test_the_banner_and_quiet_lane_keep_full_colour(self):
        self.assertNotRegex(agentboard.STYLE, r"\.mode[^{]*\{[^}]*opacity")
        self.assertNotRegex(agentboard.STYLE, r"lane-quiet[^{]*\{[^}]*opacity")
        self.assertIn(".mode:not([hidden]) { display: flex;", agentboard.STYLE)


def colours(text: str) -> dict:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6})", text))


def contrast(a: str, b: str) -> float:
    def lum(h):
        rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b_ = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


if __name__ == "__main__":
    unittest.main()
