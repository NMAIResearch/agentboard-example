"""Synthetic tests of completing a task through the CLI and existing readers."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import agentboard


class DoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "board"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("AGENTBOARD_", "CLAUDE", "CODEX_", "KIMI"))}
        self.env.update(AGENTBOARD_DIR=str(self.directory), AGENTBOARD_HARNESS="shell",
                        AGENTBOARD_SESSION="test-session", AGENTBOARD_MODEL="test-model",
                        AGENTBOARD_EFFORT="high", HOME=str(self.root / "home"))

    def board(self, *args, name="agent_a", env=None):
        return subprocess.run([sys.executable, "-I", "-B", str(HERE / "agentboard.py"), *args]
                              + (["--as", name] if name else []),
                              env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=30)

    def claim(self, path, name="agent_a"):
        result = self.board("claim", str(self.root / path), name=name)
        self.assertEqual(result.returncode, 0, result.stderr)

    def events(self):
        return [json.loads(line) for line in (self.directory / "board.jsonl").read_text().splitlines()]

    def test_releases_all_own_active_claims_only_and_keeps_event_schema(self):
        self.claim("a")
        self.claim("b", "agent_b")
        self.claim("c")
        self.claim("old")
        self.assertEqual(self.board("release", "#4").returncode, 0)
        prefix = (self.directory / "board.jsonl").read_bytes()
        result = self.board("done", "finished", "café ✅")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("released #1", result.stdout)
        self.assertIn("released #3", result.stdout)
        self.assertIn("status set for agent_a: finished café ✅", result.stdout)
        self.assertTrue((self.directory / "board.jsonl").read_bytes().startswith(prefix))
        events = self.events()
        self.assertEqual([e["id"] for e in events], list(range(1, 9)))
        self.assertEqual([e["type"] for e in events[5:]], ["release", "release", "status"])
        self.assertEqual([e["claim"] for e in events[5:7]], [1, 3])
        for event in events[5:]:
            self.assertEqual(event["from"], "agent_a")
            self.assertEqual(event["session"], "test-session")
            self.assertEqual(event["model"], "test-model")
            self.assertNotIn("forced", event)
        self.assertNotIn("text", events[5])
        self.assertEqual([(c["id"], c["from"]) for c in agentboard.active_claims(events)], [(2, "agent_b")])

    def test_existing_claims_who_widget_and_api_state_show_completion(self):
        self.claim("a")
        self.claim("b", "agent_b")
        self.assertEqual(self.board("done", "finished task").returncode, 0)
        claims = self.board("claims").stdout
        self.assertNotIn(": agent_a,", claims)
        self.assertIn(": agent_b,", claims)
        who = self.board("who").stdout
        agent_a = next(row for row in who.splitlines() if row.startswith("agent_a "))
        self.assertIn(": finished task", agent_a)
        self.assertNotIn(", claims", agent_a)
        widget = self.board("widget").stdout
        self.assertIn("finished task", widget)
        self.assertNotIn("Owns  agent_a:", widget)
        self.assertIn("Owns  agent_b:", widget)
        with mock.patch.dict(os.environ, self.env):
            handler = object.__new__(agentboard.Handler)
            handler.server = argparse.Namespace(allowed_hosts={"localhost:7101"})
            handler.headers = {"Host": "localhost:7101"}
            handler.path = "/api/state"
            handler.reply = mock.Mock()
            handler.do_GET()
        code, kind, body = handler.reply.call_args.args
        self.assertEqual((code, kind), (200, "application/json"))
        state = json.loads(body)
        agent_a = next(row for row in state["agents"] if row["name"] == "agent_a")
        self.assertEqual((agent_a["status"], agent_a["claims"]), ("finished task", []))
        self.assertEqual([row["from"] for row in state["claims"]], ["agent_b"])

    def test_no_claims_still_sets_status_and_can_repeat(self):
        for text in ("first finish", "second finish"):
            result = self.board("done", text)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no active claims for agent_a", result.stdout)
        self.assertEqual([e["type"] for e in self.events()], ["status", "status"])
        self.assertEqual(self.events()[-1]["text"], "second finish")

    def test_environment_name_and_explicit_override(self):
        self.claim("a", "agent_b")
        result = self.board("done", "done from environment", name=None, env={"AGENTBOARD_NAME": "AGENT_B"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["from"], "agent_b")
        self.claim("c")
        result = self.board("done", "explicit name", env={"AGENTBOARD_NAME": "agent_b"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(agentboard.active_claims(self.events()), [])

    def test_status_boundary_accepts_exactly_the_limit(self):
        result = self.board("done", "x" * 200)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["text"], "x" * 200)

    def test_refusals_create_no_board_or_lock(self):
        cases = [(('done', 'finished'), None), (('done', 'finished'), 'Bad Name!'),
                 (('done', 'x' * 201), 'agent_a'), (('done', '  '), 'agent_a'),
                 (('done',), 'agent_a'), (('done', 'finished', '--force'), 'agent_a')]
        for args, name in cases:
            with self.subTest(args=args, name=name):
                self.assertNotEqual(self.board(*args, name=name).returncode, 0)
                self.assertFalse(self.directory.exists())

    def test_refusals_preserve_existing_board_bytes_and_lock_metadata(self):
        self.claim("a")
        snapshot = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        for args, name in [(('done', 'x' * 201), 'agent_a'), (('done', 'finished'), None),
                           (('done', ' '), 'agent_a'), (('done', 'finished', '--force'), 'agent_a')]:
            self.assertNotEqual(self.board(*args, name=name).returncode, 0)
            self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                              for p in self.directory.iterdir()}, snapshot)

    def test_snapshot_write_and_sync_are_under_one_exclusive_lock(self):
        self.claim("a")
        self.claim("b")
        original_load, original_write, original_fsync = agentboard.load, os.write, os.fsync
        locked, lock_calls = [], []
        def flock(fd, operation):
            self.assertEqual(operation, agentboard.fcntl.LOCK_EX)
            lock_calls.append(operation)
            locked.append(fd)
        def load(directory):
            self.assertTrue(locked and not locked[0].closed)
            return original_load(directory)
        def write(fd, data):
            self.assertTrue(locked and not locked[0].closed)
            self.assertEqual([json.loads(line)["type"] for line in data.splitlines()],
                             ["release", "release", "status"])
            return original_write(fd, data)
        def sync(fd):
            self.assertFalse(locked[0].closed)
            return original_fsync(fd)
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(agentboard.fcntl, "flock", side_effect=flock), \
                mock.patch.object(agentboard, "load", side_effect=load), \
                mock.patch.object(agentboard.os, "write", side_effect=write) as writes, \
                mock.patch.object(agentboard.os, "fsync", side_effect=sync) as syncs:
            self.assertEqual(agentboard.main(["done", "finished", "--as", "agent_a"]), 0)
        self.assertEqual(len(lock_calls), 1)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual(syncs.call_count, 1)
        self.assertTrue(locked[0].closed)

    def test_torn_tail_stays_separate_and_done_is_readable(self):
        self.claim("a")
        with (self.directory / "board.jsonl").open("ab") as stream:
            stream.write(b'{"id":999,"type":')
        result = self.board("done", "finished after torn tail")
        self.assertEqual(result.returncode, 0, result.stderr)
        with mock.patch.dict(os.environ, self.env):
            events, skipped = agentboard.load()
        self.assertEqual(skipped, 1)
        self.assertEqual([e["type"] for e in events], ["claim", "release", "status"])
        self.assertEqual([e["id"] for e in events], [1, 2, 3])

    def test_write_failure_reports_no_success_and_closes_descriptors(self):
        self.claim("a")
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()) as output, \
                mock.patch.object(agentboard.os, "write", return_value=0), \
                mock.patch.object(agentboard.os, "close", wraps=os.close) as close:
            with self.assertRaisesRegex(OSError, "short write"):
                agentboard.main(["done", "finished", "--as", "agent_a"])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(close.call_count, 1)
        self.assertEqual(len(self.events()), 1)

    def test_help_lists_done(self):
        self.assertIn("done", self.board("--help").stdout)


if __name__ == "__main__":
    unittest.main()
