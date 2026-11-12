"""Synthetic boards only: board_traffic.py's day boundaries, time zones, character counts and output."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import board_traffic  # noqa: E402


class TrafficTest(unittest.TestCase):
    def board(self, *lines):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        with open(path / "board.jsonl", "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return path

    @staticmethod
    def line(eid, ts, kind="msg", text="hi", **fields):
        return json.dumps({"id": eid, "ts": ts, "type": kind, "from": "a", "to": "b", "text": text, **fields}) + "\n"

    def run_main(self, board, day):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            board_traffic.main(["--day", day, "--board", str(board), "--json"])
        return json.loads(out.getvalue())

    def test_a_utc_day_includes_its_start_and_excludes_the_next(self):
        b = self.board(self.line(1, "2026-11-10T23:59:59+00:00"), self.line(2, "2026-11-11T00:00:00+00:00"),
                       self.line(3, "2026-11-11T23:59:59+00:00"), self.line(4, "2026-11-12T00:00:00+00:00"))
        self.assertEqual(self.run_main(b, "2026-11-11")["posts"], 2)

    def test_other_offsets_fall_on_their_utc_day(self):
        b = self.board(self.line(1, "2026-11-11T00:30:00+01:00"), self.line(2, "2026-11-11T23:30:00-01:00"))
        self.assertEqual(self.run_main(b, "2026-11-10")["posts"], 1)
        self.assertEqual(self.run_main(b, "2026-11-12")["posts"], 1)

    def test_characters_are_code_points_and_other_events_are_counted_apart(self):
        b = self.board(self.line(1, "2026-11-11T10:00:00+00:00", text="café 🎬"),
                       self.line(2, "2026-11-11T10:01:00+00:00", text="x" * 600, re=1),
                       self.line(3, "2026-11-11T10:02:00+00:00", kind="status", text="busy"),
                       self.line(4, "2026-11-11T10:03:00+00:00", to="all", text=""))
        r = self.run_main(b, "2026-11-11")
        self.assertEqual((r["posts"], r["post_characters"], r["post_characters_max"]), (3, 606, 600))
        self.assertEqual((r["replies"], r["posts_to_all"], r["status_characters"]), (1, 1, 4))
        self.assertEqual(r["events_by_type"], {"msg": 3, "status": 1})

    def test_torn_malformed_and_naive_lines_are_left_out(self):
        b = self.board(self.line(1, "2026-11-11T10:00:00+00:00"), "{not json}\n", json.dumps({"id": "2", "ts": "x"}) + "\n",
                       self.line(3, "2026-11-11T10:00:00"), self.line(4, "2026-11-11T11:00:00+00:00").rstrip("\n"))
        r = self.run_main(b, "2026-11-11")
        self.assertEqual((r["posts"], r["board_lines_skipped"], r["lines_with_unreadable_time"]), (1, 3, 1))

    def test_the_plain_output_holds_counts_and_never_message_text(self):
        b = self.board(self.line(1, "2026-11-11T10:00:00+00:00", text="a private note"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            board_traffic.main(["--day", "2026-11-11", "--board", str(b)])
        self.assertIn("1 posts, 14 characters", out.getvalue())
        self.assertNotIn("private", out.getvalue())


if __name__ == "__main__":
    unittest.main()
