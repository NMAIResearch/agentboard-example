"""Tests for agentboard.py: messages and read state, claims, refusals, concurrent writers, torn lines and the
read-only page."""
import datetime as dt
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
SCRIPT = HERE / "agentboard.py"
sys.path.insert(0, str(HERE))
import agentboard  # noqa: E402

WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
import agentboard
for k in range(25):
    agentboard.append({"type": "msg", "from": "w" + sys.argv[2], "harness": "shell", "to": "all", "text": str(k)})
"""


class AgentBoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dir, self.work = root / "board", root / "work"
        self.work.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTBOARD_", "CLAUDE", "CODEX_", "KIMI"))}
        self.env.update({"AGENTBOARD_DIR": str(self.dir), "AGENTBOARD_HARNESS": "shell", "HOME": str(root / "home")})

    def tearDown(self):
        self.tmp.cleanup()

    def board(self, *args, name=None, env=None):
        argv = [sys.executable, "-I", "-B", str(SCRIPT), *args] + (["--as", name] if name else [])
        return subprocess.run(argv, env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=30)

    def events(self):
        return [json.loads(line) for line in (self.dir / "board.jsonl").read_text().splitlines()]

    def test_message_reaches_its_recipient_once(self):
        self.assertEqual(self.board("post", "agent_b", "hello", "agent_b", name="agent_a").stdout.strip(), "posted #1 to agent_b")
        self.assertIn("agent_a > agent_b: hello agent_b", self.board("inbox", name="agent_b").stdout)
        self.assertEqual(self.board("inbox", name="agent_b").stdout.strip(), "no unread messages")
        self.assertEqual(self.board("inbox", name="agent_a").stdout.strip(), "no unread messages")

    def test_broadcast_is_read_separately_and_peek_keeps_it_unread(self):
        self.board("post", "all", "standup", name="agent_a")
        self.assertIn("standup", self.board("inbox", "--peek", name="agent_b").stdout)
        self.assertIn("standup", self.board("inbox", name="agent_b").stdout)
        self.assertIn("standup", self.board("inbox", name="agent_c").stdout)
        self.assertEqual(self.board("inbox", name="agent_b").stdout.strip(), "no unread messages")

    def test_long_message_previews_then_reads_in_full_with_replies(self):
        text = "x" * 500
        self.board("post", "agent_b", text, name="agent_a")
        self.assertIn("[+340 chars: read 1]", self.board("inbox", "--peek", name="agent_b").stdout)
        self.board("post", "agent_a", "got it", "--re", "1", name="agent_b")
        out = self.board("read", "#1", name="agent_b").stdout
        self.assertIn(text, out)
        self.assertIn("replies: #2 from agent_b", out)
        self.assertEqual(self.board("inbox", name="agent_b").stdout.strip(), "no unread messages")

    def test_emoji_and_accents_round_trip(self):
        text = "render done 🎬✅ naïve café 往前走"
        self.board("post", "all", text, name="agent_a")
        self.assertEqual(self.events()[0]["text"], text)
        self.assertIn(text, self.board("inbox", name="agent_b").stdout)
        self.assertIn(text, self.board("widget").stdout)

    def test_refused_requests_write_nothing(self):
        cases = [(("post", "agent_b", "y" * 601), "agent_a", "more than 600"),
                 (("post", "agent_b", "hi"), None, "board name"),
                 (("post", "agent_b", "hi"), "Bad Name!", "board name"),
                 (("post", "Invalid Name", "hi"), "agent_a", "not a board name"),
                 (("post", "agent_b", "hi", "--re", "9"), "agent_a", "no message #9"),
                 (("status", " "), "agent_a", "empty")]
        for args, name, reason in cases:
            r = self.board(*args, name=name)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(reason, r.stderr, args)
        self.assertFalse((self.dir / "board.jsonl").exists())

    def test_overlapping_claims_are_refused_and_name_the_holder(self):
        proj = self.work / "proj"
        self.assertEqual(self.board("claim", str(proj), "--note", "render", name="agent_a").returncode, 0)
        for path in (proj, proj / "sub", self.work):
            r = self.board("claim", str(path), name="agent_b")
            self.assertEqual(r.returncode, 1, path)
            self.assertIn("claimed by agent_a (#1", r.stderr)
            self.assertIn(": render", r.stderr)
        self.assertIn("already yours: #1", self.board("claim", str(proj), name="agent_a").stdout)
        self.assertEqual(self.board("claim", str(proj / "sub"), name="agent_a").returncode, 0)
        self.assertEqual(self.board("claim", str(self.work / "proj2"), name="agent_b").returncode, 0)
        self.assertEqual([e["path"] for e in self.events() if e["type"] == "claim"],
                         [str(proj), str(proj / "sub"), str(self.work / "proj2")])

    def test_claims_too_broad_are_refused(self):
        for path in (self.env["HOME"], os.path.dirname(self.env["HOME"]), "/"):
            r = self.board("claim", path, name="agent_a")
            self.assertEqual(r.returncode, 1, path)
            self.assertIn("too broad", r.stderr)

    def test_check_exit_codes(self):
        target = str(self.work / "proj" / "f.txt")
        self.assertEqual(self.board("check", target).returncode, 0)
        self.board("claim", str(self.work / "proj"), name="agent_a")
        self.assertEqual(self.board("check", target).returncode, 1)
        self.assertEqual(self.board("check", target, name="agent_b").returncode, 1)
        self.assertEqual(self.board("check", target, name="agent_a").returncode, 0)
        self.assertEqual(self.board("check", str(self.work / "elsewhere"), name="agent_b").returncode, 0)

    def test_only_the_holder_releases_unless_forced_with_a_reason(self):
        proj = self.work / "proj"
        self.board("claim", str(proj), name="agent_a")
        r = self.board("release", str(proj), name="agent_b")
        self.assertEqual(r.returncode, 1)
        self.assertIn("held by agent_a", r.stderr)
        self.assertEqual(self.board("release", str(proj), "--force", name="agent_b").returncode, 1)
        self.assertEqual(self.board("release", "#1", "--force", "--reason", "session ended", name="owner").returncode, 0)
        self.assertEqual(self.board("claim", str(proj), name="agent_b").returncode, 0)
        forced = next(e for e in self.events() if e["type"] == "release")
        self.assertEqual((forced["forced"], forced["holder"], forced["reason"]), (True, "agent_a", "session ended"))
        self.assertEqual(self.board("release", str(proj), name="agent_b").stdout.strip(), f"released #3 {proj}")
        self.assertEqual(self.board("claims").stdout.strip(), "no active claims")

    def test_concurrent_writers_get_unique_consecutive_ids(self):
        procs = [subprocess.Popen([sys.executable, "-I", "-B", "-c", WRITER, str(HERE), str(i)], env=self.env)
                 for i in range(8)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0] * 8)
        with mock.patch.dict(os.environ, {"AGENTBOARD_DIR": str(self.dir)}):
            events, skipped = agentboard.load()
        self.assertEqual(skipped, 0)
        self.assertEqual([e["id"] for e in events], list(range(1, 201)))

    def test_a_torn_line_is_skipped_and_the_next_event_kept(self):
        self.board("post", "agent_b", "first", name="agent_a")
        with open(self.dir / "board.jsonl", "a") as f:
            f.write('{"id": 2, "type": "msg", "fr')
        self.board("post", "agent_b", "second", name="agent_a")
        lines = self.board("log").stdout.splitlines()
        self.assertEqual(lines[2], "1 torn or malformed lines skipped")
        self.assertEqual([(e["id"], e["text"]) for e in map(json.loads, lines[:2])], [(1, "first"), (2, "second")])

    def test_who_shows_status_harness_and_quiet_agents(self):
        self.board("status", "building", "the", "board", name="agent_a",
                   env={"AGENTBOARD_HARNESS": "claude-code", "CLAUDE_CODE_SESSION_ID": "s1"})
        old = {"id": 7, "ts": "2000-01-01T00:00:00+00:00", "type": "status", "from": "agent_b", "harness": "codex", "text": "rendagent_eg"}
        with open(self.dir / "board.jsonl", "a") as f:
            f.write(json.dumps(old) + "\n")
        lines = self.board("who").stdout.splitlines()
        self.assertTrue(lines[0].startswith("agent_a (claude-code, s1) seen 0 min ago: building the board"), lines)
        self.assertIn("agent_b (codex, no session)", lines[1])
        self.assertIn("QUIET", lines[1])
        self.assertEqual(self.events()[0]["session"], "s1")

    def test_page_reads_large_and_groups_the_board(self):
        for part in ('id="summary"', 'id="agents"', 'id="messages"', 'id="claims"'):
            self.assertIn(part, agentboard.PAGE)
        self.assertRegex(agentboard.STYLE, r"body \{[^}]*font: 18px")

    def test_quiet_after_two_hours_and_sessions_counted_over_one(self):
        def at(minutes, eid, name, session):
            ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
            return {"id": eid, "ts": ts.isoformat(timespec="seconds"), "type": "status", "from": name,
                    "harness": "codex", "session": session, "text": "working"}

        rows = [at(130, 1, "agent_d", "d1"), at(90, 2, "agent_c", "c1"), at(90, 3, "agent_e", "e1"), at(5, 4, "agent_e", "e2"),
                at(50, 5, "agent_f", "f1"), at(5, 6, "agent_f", "f2")]
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "board.jsonl", "a") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        who = {line.split(" ", 1)[0]: line for line in self.board("who").stdout.splitlines()}
        self.assertIn("QUIET", who["agent_d"])
        self.assertNotIn("QUIET", who["agent_c"])
        self.assertNotIn("sessions in the last hour", who["agent_e"])
        self.assertIn("2 sessions in the last hour", who["agent_f"])

    def test_a_role_moves_to_a_new_session_with_its_inbox_and_claims(self):
        self.board("post", "manager", "budget check at 14:00", name="research")
        old = {"AGENTBOARD_HARNESS": "claude-code", "CLAUDE_CODE_SESSION_ID": "old"}
        self.board("claim", str(self.work / "ops"), name="manager", env=old)
        self.board("inbox", name="manager", env=old)
        new = {"AGENTBOARD_HARNESS": "codex", "AGENTBOARD_SESSION": "new"}
        self.assertEqual(self.board("inbox", name="manager", env=new).stdout.strip(), "no unread messages")
        self.assertIn("research > manager: budget check at 14:00", self.board("inbox", "--all", name="manager", env=new).stdout)
        self.assertEqual(self.board("check", str(self.work / "ops" / "f"), name="manager", env=new).returncode, 0)
        self.assertIn("manager (claude-code, old)", self.board("who").stdout)
        self.board("status", "took over as manager", name="manager", env=new)
        who = self.board("who").stdout
        self.assertIn("manager (codex, new)", who)
        self.assertIn("2 sessions in the last hour (old, new): check only one acts as manager", who)
        self.assertEqual(self.board("inbox", "--all", name="content").stdout.strip(), "no messages for content")

    def test_harness_is_found_from_the_launching_process(self):
        h = agentboard.harness
        self.assertEqual(h(env={}, chain=[["bash"], ["/home/x/.local/bin/hermes", "chat"]]), "hermes")
        self.assertEqual(h(env={}, chain=[["/bin/sh", "-c", "x"], ["node", "/brew/bin/cline", "task"]]), "cline")
        self.assertEqual(h(env={}, chain=[["bash"], ["claude"], ["/usr/bin/konsole"]]), "claude-code")
        self.assertEqual(h(env={}, chain=[["/usr/bin/python3", "/opt/kimi-code/bin/kimi"]]), "kimi")
        self.assertEqual(h(env={}, chain=[["bash"], ["codex"]]), "codex")
        self.assertEqual(h(env={"CLAUDECODE": "1"}, chain=[["bash"], ["/usr/bin/konsole"]]), "claude-code")
        self.assertEqual(h(env={}, chain=[["bash"], ["/usr/bin/konsole"]]), "shell")
        self.assertEqual(h(env={"AGENTBOARD_HARNESS": "cline"}, chain=[["claude"]]), "cline")
        self.assertIsInstance(agentboard.ancestry(), list)

    def test_every_event_carries_a_session_people_can_read(self):
        s = agentboard.session
        self.assertEqual(s(env={"CLAUDE_CODE_SESSION_ID": "session1-569c"}, procs=[]), "session1-569c")
        self.assertEqual(s(env={"AGENTBOARD_SESSION": "x", "CLAUDE_CODE_SESSION_ID": "y"}, procs=[]), "x")
        chain = [(10, "500", ["bash"]), (20, "400", ["/home/x/.kimi-code/bin/kimi"]), (30, "300", ["konsole"])]
        self.assertEqual(s(env={}, procs=chain), "pid20-400")
        self.assertEqual(s(env={}, procs=[(10, "500", ["bash"]), (30, "300", ["konsole"])]), "pid10-500")
        tag = agentboard.tag
        self.assertEqual((tag("pid20-400"), tag("session1-569c-4622"), tag(None)), ("pid 20", "session1", "no session"))
        procs = agentboard.processes()
        self.assertTrue(procs and all(isinstance(p, int) and isinstance(t, str) for p, t, _ in procs), procs)
        self.board("status", "no harness variable here", name="agent_c")
        self.assertRegex(self.events()[-1]["session"], r"^pid\d+-\d*$")
        self.assertRegex(self.board("who").stdout, r"agent_c \(shell, pid \d+\) seen")

    def test_model_and_effort_come_from_the_harness(self):
        home = Path(self.tmp.name) / "home"

        def put(rel, text):
            path = home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        put(".codex/config.toml", 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "max"\n')
        put(".codex/sessions/2000/01/15/rollout-2000-01-15T12-00-00-abc-123.jsonl",
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "ultra"}}) + "\n")
        put(".claude/projects/-x/s9.jsonl", json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n")
        put(".kimi-code/config.toml", 'default_model = "kimi-code/k3"\n[models."kimi-code/k3"]\nmodel = "k3"\n'
                                      'display_name = "K3"\ndefault_effort = "high"\n')
        put(".gemini/antigravity-cli/settings.json", json.dumps({"model": "Gemini 4 Argon (High)"}))
        put(".cline/data/globalState.json", json.dumps({"actModeApiProvider": "ollama",
                                                        "actModeOllamaModelId": "gemma4:31b-cloud"}))
        put(".hermes/config.yaml", "model:\n  default: gemma4-12b\n  provider: custom\nstt:\n  local:\n    model: base\n"
                                   "agent:\n  reasoning_effort: medium\n")

        def rt(name, sid=None, env=None, procs=()):
            return agentboard.runtime(name, sid, env=env or {}, procs=list(procs), home=home)

        self.assertEqual(rt("codex"), {"model": "gpt-6.1-sol", "model_from": "config", "effort": "max"})
        self.assertEqual(rt("codex", "abc-123"), {"model": "gpt-6-astra", "model_from": "session", "effort": "ultra"})
        self.assertEqual(rt("claude-code", "s9", env={"CLAUDE_EFFORT": "max"}),
                         {"model": "claude-opus-5-5", "model_from": "session", "effort": "max"})
        self.assertEqual(rt("kimi"), {"model": "K3", "model_from": "config", "effort": "high"})
        self.assertEqual(rt("antigravity"), {"model": "Gemini 4 Argon (High)", "model_from": "config"})
        self.assertEqual(rt("cline"), {"model": "gemma4:31b-cloud", "model_from": "config"})
        self.assertEqual(rt("hermes"), {"model": "gemma4-12b", "model_from": "config", "effort": "medium"})
        kimi = [(7, "1", ["/usr/bin/kimi", "-m", "kimi-for-coding", "--effort", "max"])]
        self.assertEqual(rt("kimi", procs=kimi), {"model": "kimi-for-coding", "model_from": "flags", "effort": "max"})
        codex = [(8, "1", ["codex", "exec", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="ultra"'])]
        self.assertEqual(rt("codex", procs=codex), {"model": "gpt-6-astra", "model_from": "flags", "effort": "ultra"})
        self.assertEqual(rt("hermes", env={"AGENTBOARD_MODEL": "m1", "AGENTBOARD_EFFORT": "e1"}),
                         {"model": "m1", "model_from": "declared", "effort": "e1"})
        self.assertEqual(rt("shell"), {})
        self.assertEqual(agentboard.runs({"model": "K3", "effort": "high", "model_from": "config"}), "K3 high by default")
        self.board("status", "rendagent_eg", name="agent_g", env={"AGENTBOARD_MODEL": "gemma4-12b", "AGENTBOARD_EFFORT": "medium"})
        last = self.events()[-1]
        self.assertEqual((last["model"], last["effort"], last["model_from"]), ("gemma4-12b", "medium", "declared"))
        self.assertRegex(self.board("who").stdout, r"agent_g \(shell, pid \d+, gemma4-12b medium\) seen")

    def test_widget_lists_messages_agents_and_claims(self):
        self.board("post", "all", "render done", name="agent_a")
        self.board("status", "checking renders", name="agent_b")
        self.board("claim", str(self.work / "proj"), name="agent_b")
        out = self.board("widget").stdout.splitlines()
        self.assertTrue(out[0].startswith("Msg   ") and out[0].endswith("agent_a > all: render done"), out)
        self.assertTrue(any(re.fullmatch(r"Agent agent_b \(shell, pid \d+\): checking renders", x) for x in out), out)
        self.assertIn(f"Owns  agent_b: {self.work / 'proj'}", out)

    def test_page_is_loopback_only_and_read_only(self):
        self.board("post", "all", "<b>hi</b>", name="agent_a")
        with mock.patch.dict(os.environ, {"AGENTBOARD_DIR": str(self.dir)}):
            server = agentboard.make_server(0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def get(path, host=f"127.0.0.1:{port}", method="GET"):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        conn.putrequest(method, path, skip_host=True)
                        conn.putheader("Host", host)
                        conn.endheaders()
                        r = conn.getresponse()
                        return r.status, r.getheader("Content-Security-Policy") or "", r.read()
                    finally:
                        conn.close()

                status, csp, body = get("/api/state")
                self.assertEqual(status, 200)
                state = json.loads(body)
                self.assertEqual(state["messages"][0]["text"], "<b>hi</b>")
                self.assertRegex(state["messages"][0]["session"], r"^pid \d+$")
                self.assertEqual(state["agents"][0]["session"], state["messages"][0]["session"])
                self.assertIn("frame-ancestors 'self' http://127.0.0.1:5280", csp)
                self.assertEqual(get("/")[0], 200)
                self.assertEqual(get(f"/api/state", host=f"localhost:{port}")[0], 200)
                self.assertEqual(get("/api/state", host="attacker.example")[0], 403)
                self.assertEqual(get("/api/state", method="POST")[0], 405)
                self.assertEqual(get("/nothing")[0], 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class AgentBoardWakeTest(unittest.TestCase):
    setUp = AgentBoardTest.setUp
    tearDown = AgentBoardTest.tearDown
    board = AgentBoardTest.board
    events = AgentBoardTest.events

    def seed(self, *events):
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "board.jsonl").open("a") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def event(self, eid, kind="msg", sender="agent_a", **fields):
        return {"id": eid, "ts": "2000-01-15T04:00:00+00:00", "type": kind,
                "from": sender, "harness": "shell", **fields}

    def snapshot(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
                for path in self.dir.iterdir()} if self.dir.exists() else None

    def test_wait_returns_immediately_for_every_unread_message_with_short_preview(self):
        self.seed(self.event(1, to="agent_b", text="x" * 150), self.event(2, to="agent_b", text="second"))
        before = self.snapshot()
        result = self.board("wait", "--timeout", "0", name="agent_b")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "#1 2000-01-15T04:00:00+00:00 agent_a: " + "x" * 120
                         + " | agentboard read 1 --as agent_b")
        self.assertIn("second | agentboard read 2 --as agent_b", lines[1])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.board("wait", "--timeout", "0", name="agent_b").stdout, result.stdout)

    def test_wait_for_a_message_arriving_while_the_process_waits(self):
        argv = [sys.executable, "-B", "-E", "-P", "-W", "error::ResourceWarning", str(SCRIPT),
                "wait", "--as", "agent_b", "--timeout", "5", "--interval", "0.02"]
        with subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            try:
                import time
                time.sleep(0.15)
                self.assertIsNone(proc.poll(), "wait exited before any message")
                self.seed(self.event(1, to="agent_b", text="arrived during wait"))
                stdout, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertIn("arrived during wait", stdout)
                self.assertEqual(len(self.events()), 1)
                self.assertFalse((self.dir / "board.lock").exists())
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.communicate(timeout=5)

    def test_wait_ignores_other_recipients_and_times_out_silently(self):
        self.seed(self.event(1, to="agent_c", text="not agent_b"))
        before = self.snapshot()
        result = self.board("wait", "--timeout", "0.03", "--interval", "0.01", name="agent_b")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.snapshot(), before)

    def test_wait_broadcast_is_unread_separately_for_each_name(self):
        self.seed(self.event(1, to="all", text="broadcast"), self.event(2, "read", sender="agent_c", msgs=[1]))
        self.assertEqual(self.board("wait", "--timeout", "0", name="agent_c").returncode, 3)
        self.assertEqual(self.board("wait", "--timeout", "0", name="agent_b").returncode, 0)
        self.assertEqual(self.board("wait", "--timeout", "0", name="agent_a").returncode, 3)
        self.assertEqual(len(self.events()), 2)

    def test_wait_excludes_messages_already_read_by_the_recipient(self):
        self.seed(self.event(1, to="agent_b", text="read"), self.event(2, "read", sender="agent_b", msgs=[1]))
        self.assertEqual(self.board("wait", "--timeout", "0", name="agent_b").returncode, 3)
        self.assertEqual(len(self.events()), 2)

    def test_wait_timeout_on_missing_board_creates_no_directory(self):
        result = self.board("wait", "--timeout", "0", name="agent_b")
        self.assertEqual(result.returncode, 3)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertFalse(self.dir.exists())
        self.assertIn("3 on timeout", self.board("wait", "--help").stdout)

    def test_wait_sleeps_and_loads_only_when_metadata_changes(self):
        clock = {"now": 0.0}
        sleeps = []

        def sleep(duration):
            sleeps.append(duration)
            clock["now"] += duration

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(agentboard.time, "monotonic", side_effect=lambda: clock["now"]), \
                mock.patch.object(agentboard.time, "sleep", side_effect=sleep), \
                mock.patch.object(agentboard, "load", wraps=agentboard.load) as load:
            self.assertEqual(agentboard.main(["wait", "--as", "agent_b", "--timeout", "0.3", "--interval", "0.1"]), 3)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(len(sleeps), 3)
        self.assertTrue(all(duration > 0 for duration in sleeps))

    def test_wait_catches_an_append_during_its_first_read(self):
        original_load = agentboard.load
        reads = []

        def load(directory):
            result = original_load(directory)
            reads.append(1)
            if len(reads) == 1:
                self.seed(self.event(1, to="agent_b", text="raced the read"))
            return result

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(agentboard, "load", side_effect=load), \
                mock.patch.object(agentboard.time, "sleep"):
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(agentboard.main(["wait", "--as", "agent_b", "--timeout", "1"]), 0)
        self.assertEqual(len(reads), 2)
        self.assertIn("raced the read", output.getvalue())

    def test_wait_rejects_invalid_timing_and_missing_name_without_writing(self):
        for option, value in [("--timeout", "-1"), ("--timeout", "nan"), ("--timeout", "inf"),
                              ("--interval", "0"), ("--interval", "-1"), ("--interval", "nan")]:
            with self.subTest(option=option, value=value):
                self.assertEqual(self.board("wait", option, value, name="agent_b").returncode, 2)
        self.assertEqual(self.board("wait", "--timeout", "0").returncode, 1)
        self.assertFalse(self.dir.exists())

    def test_wait_uses_the_environment_name_and_flattens_multiline_unicode(self):
        self.seed(self.event(1, to="agent_b", text="café\nsecond line 🎬"))
        result = self.board("wait", "--timeout", "0", env={"AGENTBOARD_NAME": "agent_b"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn("café second line 🎬", result.stdout)

    def test_wait_reports_read_error_without_writing(self):
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(agentboard, "board_stamp", side_effect=PermissionError("read denied")), \
                mock.patch("sys.stderr") as stderr:
            self.assertEqual(agentboard.main(["wait", "--as", "agent_b", "--timeout", "0"]), 1)
        self.assertIn("cannot watch the board", "".join(str(call) for call in stderr.write.call_args_list))
        self.assertFalse(self.dir.exists())

    def test_nudge_prints_each_help_derived_resume_command_and_newest_unread_pointer(self):
        import shlex
        pointer = "Board message #3 for agent_b: agentboard read 3 --as agent_b"
        for harness, expected in [
                ("codex", ["codex", "resume", "s-123", pointer]),
                ("kimi", ["kimi", "--session", "s-123", "--prompt", pointer]),
                ("antigravity", ["agy", "--conversation", "s-123", "--prompt-interactive", pointer]),
                ("claude-code", ["claude", "--resume", "s-123", pointer])]:
            with self.subTest(harness=harness):
                self.seed(self.event(1, "status", sender="agent_b", harness=harness, session="s-123", text="idle"),
                          self.event(2, to="agent_b", text="older"), self.event(3, to="agent_b", text="newest"))
                before = self.snapshot()
                result = self.board("nudge", "agent_b", name="manager")
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(lines[0], f"agent_b: session s-123, harness {harness}")
                self.assertEqual(shlex.split(lines[1]), expected)
                self.assertEqual("SendMessage" in result.stdout, harness == "claude-code")
                self.assertEqual(self.snapshot(), before)

    def test_nudge_uses_the_latest_record_without_combining_stale_session_fields(self):
        self.seed(self.event(1, "status", sender="agent_b", harness="codex", session="old", text="old"),
                  self.event(2, "status", sender="agent_b", harness="kimi", session="new", text="new"),
                  self.event(3, to="agent_b", text="pointer"))
        self.assertIn("kimi --session new --prompt", self.board("nudge", "agent_b").stdout)
        self.seed(self.event(4, "status", sender="agent_b", harness="codex", text="no session"))
        result = self.board("nudge", "agent_b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("session unknown, harness codex", result.stdout)
        self.assertIn("no resumable session id", result.stderr)

    def test_nudge_refuses_a_name_with_no_session(self):
        self.seed(self.event(1, "status", sender="agent_b", text="old record"), self.event(2, to="agent_b", text="pending"))
        before = self.snapshot()
        result = self.board("nudge", "agent_b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no resumable session id", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_nudge_refuses_unknown_name_and_writes_nothing(self):
        result = self.board("nudge", "nobody")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown board name", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.dir.exists())

    def test_nudge_emits_no_command_without_unread_messages(self):
        self.seed(self.event(1, "status", sender="agent_b", harness="codex", session="s-123", text="idle"),
                  self.event(2, to="agent_b", text="seen"), self.event(3, "read", sender="agent_b", harness="codex",
                  session="s-123", msgs=[2]))
        result = self.board("nudge", "agent_b")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no unread messages", result.stdout)
        self.assertNotIn("codex resume", result.stdout)

    def test_nudge_includes_broadcasts_but_ignores_newer_read_and_other_messages(self):
        self.seed(self.event(1, "status", sender="agent_b", harness="codex", session="s-123", text="idle"),
                  self.event(2, to="all", text="broadcast"), self.event(3, to="agent_b", text="seen"),
                  self.event(4, "read", sender="agent_b", harness="codex", session="s-123", msgs=[3]),
                  self.event(5, to="agent_c", text="not agent_b"))
        result = self.board("nudge", "agent_b")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Board message #2 for agent_b: agentboard read 2 --as agent_b", result.stdout)

    def test_nudge_refuses_synthetic_unsafe_sessions_and_unknown_harness(self):
        for sid, harness in [("pid123-456", "codex"), ("--last", "codex"), ("s;echo bad", "codex"),
                             ("s-123", "unknown")]:
            with self.subTest(sid=sid, harness=harness):
                self.seed(self.event(1, "status", sender="agent_b", harness=harness, session=sid, text="idle"),
                          self.event(2, to="agent_b", text="pending"))
                result = self.board("nudge", "agent_b")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_wait_and_nudge_never_call_writer_or_runtime_discovery(self):
        from contextlib import redirect_stdout
        from io import StringIO
        self.seed(self.event(1, "status", sender="agent_b", harness="codex", session="s-123", text="idle"),
                  self.event(2, to="agent_b", text="pending"))
        before = self.snapshot()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(agentboard, "append", side_effect=AssertionError("write attempted")), \
                mock.patch.object(agentboard, "runtime", side_effect=AssertionError("runtime discovery attempted")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("external command attempted")), \
                redirect_stdout(StringIO()):
            self.assertEqual(agentboard.main(["wait", "--as", "agent_b", "--timeout", "0"]), 0)
            self.assertEqual(agentboard.main(["nudge", "agent_b"]), 0)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
