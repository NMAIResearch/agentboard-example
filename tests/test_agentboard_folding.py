"""Synthetic folding boundaries, compatibility, widget output and page markup."""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import agentboard  # noqa: E402

AT = dt.datetime(2000, 1, 15, 12, tzinfo=dt.timezone.utc)
DAY = dt.timedelta(days=1)


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.tags[attrs["id"]] = tag, attrs


class FoldingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"AGENTBOARD_DIR": str(self.directory)})
        patch.start()
        self.addCleanup(patch.stop)
        clock = mock.patch.object(agentboard, "now", return_value=AT)
        clock.start()
        self.addCleanup(clock.stop)

    def event(self, eid, kind, sender, age=dt.timedelta(), **fields):
        return {"id": eid, "ts": (AT - age).isoformat(), "type": kind, "from": sender,
                "harness": "shell", "session": sender + "-session", **fields}

    def seed(self, *events):
        with (self.directory / "board.jsonl").open("a") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def widget(self, lines=4):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agentboard.cmd_widget(argparse.Namespace(lines=lines))
        return output.getvalue().splitlines()

    def test_message_window_keeps_existing_api_rows_and_exposes_display_limit(self):
        self.seed(*(self.event(i, "msg", "writer", to="all", text=f"message {i}") for i in range(1, 66)))
        state = agentboard.state()
        self.assertEqual([m["id"] for m in state["messages"]], list(range(65, 5, -1)))
        self.assertEqual(state["folding"], {"message_limit": 15, "age_seconds": 86400})
        self.assertEqual([m["id"] for m in agentboard.state(limit=3)["messages"]], [65, 64, 63])

    def test_agents_over_one_day_fold_even_with_claims_and_two_hour_quiet_stays(self):
        self.seed(self.event(1, "claim", "old", DAY + dt.timedelta(seconds=1), path="/synthetic/old"),
                  self.event(2, "status", "boundary", DAY, text="boundary"),
                  self.event(3, "status", "quiet", dt.timedelta(hours=3), text="quiet"),
                  self.event(4, "status", "active", dt.timedelta(minutes=1), text="working"))
        rows = {a["name"]: a for a in agentboard.state()["agents"]}
        self.assertTrue(rows["old"]["folded"])
        self.assertEqual(rows["old"]["claims"], ["/synthetic/old"])
        self.assertFalse(rows["boundary"]["folded"])
        self.assertTrue(rows["quiet"]["quiet"])
        self.assertFalse(rows["quiet"]["folded"])
        self.assertFalse(rows["active"]["quiet"])

    def test_claim_age_uses_claim_event_even_after_holder_posts(self):
        self.seed(self.event(1, "claim", "holder", DAY * 2, path="/synthetic/old", note="keep note"),
                  self.event(2, "status", "holder", text="fresh status"),
                  self.event(3, "claim", "holder", DAY, path="/synthetic/boundary"),
                  self.event(4, "claim", "holder", DAY - dt.timedelta(seconds=1), path="/synthetic/fresh"))
        state = agentboard.state()
        rows = {c["id"]: c for c in state["claims"]}
        self.assertEqual([(rows[i]["folded"], rows[i]["age_seconds"]) for i in (1, 3, 4)],
                         [(True, 172800), (False, 86400), (False, 86399)])
        self.assertEqual(rows[1]["note"], "keep note")
        self.assertFalse(state["agents"][0]["folded"])

    def test_released_claims_do_not_reappear_in_either_group(self):
        self.seed(self.event(1, "claim", "holder", DAY * 2, path="/synthetic/released"),
                  self.event(2, "release", "holder", claim=1, path="/synthetic/released"))
        self.assertEqual(agentboard.state()["claims"], [])
        self.assertFalse(any(s.startswith("Owns  ") for s in self.widget()))

    def test_state_retains_legacy_fields_and_does_not_mutate_board(self):
        self.seed(self.event(1, "msg", "writer", to="reader", text="<script>text only</script>", re=9),
                  self.event(2, "claim", "writer", path="/synthetic/path", note="note"))
        before = (self.directory / "board.jsonl").read_bytes()
        state = agentboard.state()
        self.assertTrue({"generated", "messages", "agents", "claims", "skipped_lines"} <= state.keys())
        self.assertTrue({"id", "ts", "from", "to", "re", "text", "harness", "session"} <= state["messages"][0].keys())
        self.assertTrue({"name", "harness", "last", "status", "status_ts", "quiet", "claims", "sessions",
                         "session", "recent", "runs"} <= state["agents"][0].keys())
        self.assertTrue({"id", "ts", "from", "path", "note"} <= state["claims"][0].keys())
        self.widget()
        self.assertEqual((self.directory / "board.jsonl").read_bytes(), before)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["board.jsonl"])

    def test_widget_folds_old_agents_with_claims_and_counts_all_old_rows(self):
        self.seed(*(self.event(i, "claim", f"old{i}", DAY * 2, path=f"/synthetic/old{i}") for i in range(1, 7)),
                  self.event(7, "status", "fresh", text="working"),
                  self.event(8, "claim", "fresh", path="/synthetic/fresh"))
        lines = self.widget(lines=1)
        self.assertIn("Agent quiet: 6", lines)
        self.assertIn("Owns  older claims: 6", lines)
        self.assertIn("Owns  fresh: /synthetic/fresh", lines)
        self.assertEqual(sum(s.startswith("Agent fresh ") for s in lines), 1)
        self.assertFalse(any("/synthetic/old" in s or s.startswith("Agent old") for s in lines))

    def test_widget_keeps_message_order_limits_and_fresh_claim_overflow(self):
        self.seed(*(self.event(i, "msg", "writer", to="all", text=f"message {i}") for i in range(1, 5)),
                  *(self.event(i + 10, "claim", "holder", path=f"/synthetic/fresh{i}") for i in range(1, 5)))
        lines = self.widget(lines=2)
        messages = [s for s in lines if s.startswith("Msg   ")]
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0].endswith("message 4"))
        self.assertTrue(messages[1].endswith("message 3"))
        self.assertIn("Owns  2 more: agentboard.py claims", lines)
        self.assertEqual(sum(s.startswith("Owns  holder:") for s in lines), 2)
        self.assertFalse(any("quiet:" in s or "older claims:" in s for s in lines))

    def test_widget_boundary_is_visible_and_lines_fit(self):
        self.seed(self.event(1, "claim", "boundary", DAY, path="/synthetic/" + "x" * 200),
                  self.event(2, "status", "boundary", DAY, text="long " * 40),
                  self.event(3, "msg", "writer", to="all", text="Unicode café 🎬 " * 30))
        lines = self.widget()
        self.assertTrue(any(s.startswith("Owns  boundary:") for s in lines))
        self.assertTrue(any(s.startswith("Agent boundary ") for s in lines))
        self.assertTrue(all(len(s) <= 74 for s in lines), lines)

    def test_empty_board_read_views_create_nothing(self):
        state = agentboard.state()
        self.assertEqual((state["agents"], state["claims"], state["messages"]), ([], [], []))
        self.assertEqual(self.widget(), ["Msg   none yet"])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_page_has_three_closed_native_folds_and_preserves_primary_lists(self):
        markup = Markup(agentboard.PAGE)
        for kind in ("agents", "messages", "claims"):
            fold_tag, attrs = markup.tags["older-" + kind]
            self.assertEqual(fold_tag, "details")
            self.assertNotIn("open", attrs)
            self.assertIn("hidden", attrs)
            self.assertEqual(markup.tags["older-" + kind + "-summary"][0], "summary")
            self.assertEqual(markup.tags["older-" + kind + "-list"][0], "ol" if kind == "messages" else "ul")
            self.assertIn(kind, markup.tags)
        for label in ("Older messages", "Quiet for over a day", "Claims older than a day"):
            self.assertIn(label, agentboard.PAGE)
        self.assertIn('name="viewport"', agentboard.PAGE)

    def test_page_partitions_lists_uses_text_nodes_and_keeps_mobile_grid(self):
        self.assertIn("s.messages.slice(0, s.folding.message_limit)", agentboard.SCRIPT)
        self.assertIn("s.messages.slice(s.folding.message_limit)", agentboard.SCRIPT)
        self.assertIn("s.agents.filter(a => !a.folded)", agentboard.SCRIPT)
        self.assertIn("s.claims.filter(c => !c.folded)", agentboard.SCRIPT)
        self.assertIn("age(c.age_seconds)", agentboard.SCRIPT)
        self.assertNotIn("innerHTML", agentboard.SCRIPT)
        self.assertIn("node.textContent = text", agentboard.SCRIPT)
        self.assertIn("minmax(0, 1fr)", agentboard.STYLE)
        self.assertIn("overflow-wrap: anywhere", agentboard.STYLE)
        self.assertIn("[hidden] { display: none; }", agentboard.STYLE)

    def test_handler_refuses_remote_hosts_and_write_methods_without_sockets(self):
        handler = object.__new__(agentboard.Handler)
        handler.server = argparse.Namespace(allowed_hosts={"127.0.0.1:7101", "localhost:7101"})
        handler.path = "/api/state"
        handler.reply = mock.Mock()
        handler.headers = {"Host": "attacker.example"}
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0], 403)
        for method in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
            getattr(handler, method)()
            self.assertEqual(handler.reply.call_args.args[0], 405)
        handler.headers = {"Host": "127.0.0.1:7101"}
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0], 200)
        self.assertEqual(json.loads(handler.reply.call_args.args[2])["messages"], [])

    def test_server_configuration_binds_only_loopback_without_opening_socket(self):
        server = argparse.Namespace(server_address=("127.0.0.1", 43210))
        with mock.patch.object(agentboard, "ThreadingHTTPServer", return_value=server) as factory:
            self.assertIs(agentboard.make_server(0), server)
        factory.assert_called_once_with(("127.0.0.1", 0), agentboard.Handler)
        self.assertEqual(server.allowed_hosts, {"127.0.0.1:43210", "localhost:43210"})


if __name__ == "__main__":
    unittest.main()
