"""Tests for the pinned line, the one-word acknowledgement, the closed mark on a thread, the length refusal and
--check, the record reader, the model flags with their clearing and Claude Code's switch hook, and what the page gets.

Synthetic boards, homes and session files in temporary folders only; no existing board or home is read. Run from the
repository's root:

    python3 -I -B -m unittest tests/test_agentboard_friendly.py
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
SCRIPT = HERE / "agentboard.py"
sys.path.insert(0, str(HERE))
import agentboard  # noqa: E402

AT = dt.datetime(2026, 11, 13, 12, tzinfo=dt.timezone.utc)
OPUS, OPUS5, FABLE = "claude-opus-5-5", "claude-opus-5", "claude-fable-5-2"


def answer(model, requested=None, sidechain=False, kind="assistant", ts="2026-11-13T11:00:00.000Z", content=None):
    rec = {"type": kind, "isSidechain": sidechain, "timestamp": ts,
           "message": {"model": model, "role": "assistant", "content": content or [{"type": "text", "text": "words"}]}}
    if requested:
        rec["requestedModel"] = requested
    return rec


def fallback(ts, original=OPUS, to=OPUS5, trigger="refusal"):
    return {"type": "system", "subtype": "model_refusal_fallback", "level": "warning", "trigger": trigger,
            "direction": "retry", "scope": "session", "originalModel": original, "fallbackModel": to,
            "apiRefusalCategory": "example", "timestamp": ts, "content": "a notice the board never keeps"}


def stop_notice(ts):
    return {"type": "system", "subtype": "informational", "timestamp": ts,
            "content": "Opus 5.5's safeguards stopped the response above · continuing once with that noted"}


class Cli(unittest.TestCase):
    """The board run as a command, as a seat runs it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dir, self.home = root / "board", root / "home"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTBOARD_", "CLAUDE", "CODEX_", "KIMI"))}
        self.env.update({"AGENTBOARD_DIR": str(self.dir), "AGENTBOARD_HARNESS": "shell", "HOME": str(self.home)})

    def board(self, *args, name=None, env=None, stdin=None, script=SCRIPT):
        argv = [sys.executable, "-I", "-B", str(script), *args] + (["--as", name] if name else [])
        return subprocess.run(argv, env={**self.env, **(env or {})}, input=stdin, capture_output=True, text=True,
                              timeout=30)

    def ok(self, *args, **kw):
        r = self.board(*args, **kw)
        self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
        return r.stdout

    def refused(self, *args, reason, **kw):
        """The command fails with `reason` and leaves the board's bytes as they were."""
        before = self.raw()
        r = self.board(*args, **kw)
        self.assertNotEqual(r.returncode, 0, (args, r.stdout))
        self.assertIn(reason, r.stderr, args)
        self.assertEqual(self.raw(), before, args)
        return r

    def raw(self):
        path = self.dir / "board.jsonl"
        return path.read_bytes() if path.exists() else None

    def events(self):
        return [json.loads(row) for row in (self.dir / "board.jsonl").read_text().splitlines()]

    def seat(self, sid, *records):
        path = self.home / ".claude" / "projects" / "-x" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    @staticmethod
    def claude(sid):
        return {"AGENTBOARD_HARNESS": "claude-code", "CLAUDE_CODE_SESSION_ID": sid}

    def wakes(self, name):
        return self.board("wait", "--timeout", "0", name=name).returncode == 0


class AckTest(Cli):
    def test_an_acknowledgement_is_stored_shown_once_and_wakes_no_one(self):
        self.ok("post", "bob", "the render is in", name="alice")
        self.ok("inbox", name="bob")
        self.assertIn("acknowledged #1 as seen; alice sees it", self.ok("ack", "1", "seen", name="bob"))
        e = self.events()[-1]
        self.assertEqual((e["type"], e["from"], e["msg"], e["to"], e["word"]), ("ack", "bob", 1, "alice", "seen"))
        self.assertFalse(self.wakes("alice"))
        self.assertIn(f"acknowledged: seen by bob {agentboard.when(e['ts'])}", self.ok("read", "1", name="bob"))
        out = self.ok("inbox", name="alice")
        self.assertEqual(out.splitlines(), ["no unread messages", "acknowledged: #1 seen by bob"])
        self.assertEqual(self.events()[-1]["acks"], [e["id"]])
        self.assertEqual(self.ok("inbox", name="alice").strip(), "no unread messages")

    def test_a_message_to_all_takes_one_acknowledgement_from_each_seat(self):
        self.ok("post", "all", "HOLD for ten minutes", name="lead")
        self.ok("ack", "#1", "noted", name="bob")
        self.ok("ack", "1", "Thanks", name="carol")
        self.assertEqual([(e["from"], e["word"]) for e in self.events()[1:]], [("bob", "noted"), ("carol", "thanks")])
        self.assertEqual(self.ok("inbox", name="lead").splitlines()[1:],
                         ["acknowledged: #1 noted by bob", "acknowledged: #1 thanks by carol"])

    def test_refusals_write_nothing(self):
        self.ok("post", "bob", "hi", name="alice")
        self.ok("post", "carol", "not for bob", name="alice")
        self.refused("ack", "9", "seen", name="bob", reason="there is no message #9")
        self.refused("ack", "1", "seen", name="alice", reason="#1 is your own message")
        self.refused("ack", "2", "seen", name="bob", reason="#2 is to carol, not to you or to all")
        self.refused("ack", "1", "great", name="bob", reason="is not an acknowledgement: use seen, thanks or noted")
        self.refused("ack", "1", "seen", reason="give your board name")
        self.ok("ack", "1", "seen", name="bob")
        self.refused("ack", "1", "thanks", name="bob", reason="you acknowledged #1 as seen at")

    def test_more_than_five_show_five_then_a_count(self):
        self.ok("post", "all", "notice", name="lead")
        for k in range(7):
            self.ok("ack", "1", "seen", name=f"s{k}")
        first = self.ok("inbox", name="lead").splitlines()
        self.assertEqual(len([x for x in first if x.startswith("acknowledged:")]), 5)
        self.assertIn("2 more acknowledgements: run inbox again", first)
        again = self.ok("inbox", name="lead").splitlines()
        self.assertEqual([x for x in again if x.startswith("acknowledged:")],
                         ["acknowledged: #1 seen by s5", "acknowledged: #1 seen by s6"])


class CloseTest(Cli):
    def test_a_close_is_shown_to_the_starter_and_a_reply_reopens_it(self):
        self.ok("post", "lead", "the copy is ready", name="agent-b")
        self.ok("post", "agent-b", "placed", "--re", "1", name="lead")
        self.ok("inbox", name="agent-b")
        self.assertIn("closed thread #1; a reply reopens it", self.ok("close", "2", "--note", "placed", name="lead"))
        c = self.events()[-1]
        self.assertEqual((c["type"], c["root"], c["msg"], c["to"], c["note"]), ("close", 1, 2, "agent-b", "placed"))
        self.assertFalse(self.wakes("agent-b"))
        self.assertIn(f"thread #1 closed by lead {agentboard.when(c['ts'])}: placed", self.ok("read", "1", name="x"))
        out = self.ok("inbox", name="agent-b")
        self.assertIn(f"closed: thread #1 by lead {agentboard.when(c['ts'])}: placed", out)
        self.assertEqual(self.events()[-1]["closes"], [c["id"]])
        self.assertNotIn("closed:", self.ok("inbox", name="agent-b"))
        self.refused("close", "1", name="agent-b", reason="thread #1 is already closed, by lead")
        reply = int(self.ok("post", "lead", "one more thing", "--re", "2", name="agent-b").split("#")[1].split()[0])
        self.assertIn(f"thread #1 reopened by #{reply}", self.ok("read", "2", name="x"))
        self.ok("close", str(reply), name="agent-b")
        self.assertEqual(agentboard.closures(self.events())[1]["closed"]["by"], "agent-b")

    def test_only_the_threads_people_close_it_unless_it_was_to_all(self):
        self.ok("post", "bob", "private", name="alice")
        self.refused("close", "1", name="carol", reason="you are not in thread #1: only its people close it")
        self.refused("close", "7", name="alice", reason="there is no message #7")
        self.refused("close", "1", "--note", "n" * 201, name="alice", reason="the note has 201 characters, more than 200 by 1")
        self.ok("post", "all", "to everyone", name="alice")
        self.ok("close", "2", name="carol")
        self.assertNotIn("closed:", self.ok("inbox", name="carol"))
        self.assertIn("closed: thread #2 by carol", self.ok("inbox", name="alice"))


class LengthTest(Cli):
    def test_the_refusal_says_how_much_to_cut(self):
        r = self.refused("post", "bob", "y" * 607, name="alice", reason="more than 600")
        self.assertEqual(r.stderr.strip(), "agentboard: the text has 607 characters, more than 600 by 7: cut 7, or put "
                                           "the detail in a file and post its path")

    def test_check_counts_as_the_command_would_and_writes_nothing(self):
        self.assertEqual(self.ok("post", "bob", "y" * 598, "--check").strip(), "598 characters, 2 to spare of 600")
        self.assertEqual(self.ok("post", "bob", "  y  ", "--check").strip(), "1 character, 599 to spare of 600")
        self.refused("post", "bob", "y" * 601, "--check", reason="the text has 601 characters, more than 600 by 1: cut 1")
        self.refused("post", "Bob Smith", "hi", "--check", reason="not a board name")
        self.assertEqual(self.ok("status", "s" * 200, "--check").strip(), "200 characters, 0 to spare of 200")
        self.refused("done", "s" * 205, "--check", reason="the status has 205 characters, more than 200 by 5: cut 5")
        self.assertEqual(self.ok("claim", str(self.home / "w"), "--note", "n" * 10, "--check").strip(),
                         "10 characters, 190 to spare of 200")
        self.assertIn("no note given", self.ok("claim", str(self.home / "w"), "--check"))
        self.assertIn("no note given", self.ok("close", "1", "--check"))
        self.assertIsNone(self.raw())
        self.assertFalse(self.dir.exists())


class PinnedTest(Cli):
    def write_line(self, text):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "pinned.txt").write_text(text, encoding="utf-8")

    def test_shown_when_it_changes_and_recorded_as_its_digest(self):
        self.ok("post", "bob", "hello", name="alice")
        self.assertNotIn("Pinned", self.ok("inbox", "--peek", name="bob"))
        self.write_line("\n   \nPlease keep   posts short.\nsecond line\n")
        self.assertEqual(self.ok("inbox", "--peek", name="bob").splitlines()[0], "Pinned: Please keep posts short.")
        before = self.raw()
        self.assertIn("Pinned: Please keep posts short.", self.ok("inbox", "--all", name="bob"))
        self.assertEqual(self.raw(), before)
        out = self.ok("inbox", name="bob").splitlines()
        self.assertEqual(out[0], "Pinned: Please keep posts short.")
        self.assertIn("alice > bob: hello", out[1])
        self.assertEqual(self.events()[-1]["note"], agentboard.digest("Please keep posts short."))
        self.assertEqual(self.ok("inbox", name="bob").strip(), "no unread messages")
        self.write_line("A new line.")
        self.assertEqual(self.ok("inbox", name="bob").splitlines(), ["Pinned: A new line.", "no unread messages"])
        read = self.events()[-1]
        self.assertEqual((read["type"], read["msgs"], read["note"]), ("read", [], agentboard.digest("A new line.")))
        self.assertIn("Pinned: A new line.", self.ok("inbox", name="carol"))

    def test_a_long_line_is_cut_and_an_empty_file_shows_nothing(self):
        self.write_line("w" * 250)
        self.assertEqual(self.ok("inbox", name="bob").splitlines()[0], "Pinned: " + "w" * 200 + " (cut)")
        self.write_line("\n \n")
        self.assertEqual(self.ok("inbox", name="carol").strip(), "no unread messages")
        (self.dir / "pinned.txt").write_bytes(b"\xff\xfe not text")
        self.assertEqual(self.ok("inbox", name="dana").strip(), "no unread messages")


class PinnedDayTest(unittest.TestCase):
    def test_shown_again_once_a_day(self):
        said = "Keep it lean."
        read = {"id": 1, "type": "read", "from": "bob", "ts": AT.isoformat(), "msgs": [], "note": agentboard.digest(said)}
        self.assertFalse(agentboard.pinned_due([read], "bob", said, AT + dt.timedelta(hours=23, minutes=59)))
        self.assertTrue(agentboard.pinned_due([read], "bob", said, AT + dt.timedelta(hours=24)))
        self.assertTrue(agentboard.pinned_due([read], "bob", "Something else.", AT))
        self.assertTrue(agentboard.pinned_due([read], "carol", said, AT))
        later = {"id": 2, "type": "read", "from": "bob", "ts": (AT + dt.timedelta(hours=5)).isoformat(), "msgs": [3]}
        self.assertFalse(agentboard.pinned_due([read, later], "bob", said, AT + dt.timedelta(hours=6)))


class ReaderTest(unittest.TestCase):
    def tail(self, *records, torn=True):
        rows = [json.dumps(r) for r in records]
        return ('el":{"model":"claude-torn-1"}}\n' if torn else "") + "\n".join(rows) + "\n"

    def test_the_last_main_loop_answer_and_nothing_else(self):
        tool = [{"type": "tool_use", "name": "Agent", "input": {"model": "claude-haiku-5-5", "prompt": "p"}}]
        text = self.tail(answer(FABLE), answer(OPUS, content=tool), answer("claude-haiku-5-5", sidechain=True),
                         answer("<synthetic>"), {"type": "user", "message": {"role": "user", "content": [
                             {"type": "tool_result", "content": '{"model": "gpt-6-astra"}'}]}})
        self.assertEqual(agentboard.claude_records(text), {"model": OPUS})
        # the reader it replaces took the last "model" key anywhere in the tail: here a tool call's own argument
        self.assertEqual(re.findall(r'"model"\s*:\s*"([^"<]+)"', text)[-1], "claude-haiku-5-5")
        self.assertEqual(agentboard.claude_records(self.tail({"message": {"model": OPUS}}, torn=False)), {"model": OPUS})
        self.assertEqual(agentboard.claude_records("not json\n[1, 2]\n" + self.tail(answer(""), torn=False)), {})

    def test_requested_fallback_and_stop_as_fields_only(self):
        text = self.tail(stop_notice("2026-11-12T18:42:30.120Z"), fallback("2026-11-12T18:45:32.500Z"),
                         answer(OPUS5, requested=OPUS))
        got = agentboard.claude_records(text)
        self.assertEqual(got["model"], OPUS5)
        self.assertEqual(got["requested"], OPUS)
        self.assertEqual(got["stopped"], "2026-11-12T18:42:30.120Z")
        self.assertEqual(got["fallback"], {"originalModel": OPUS, "fallbackModel": OPUS5, "trigger": "refusal",
                                           "scope": "session", "timestamp": "2026-11-12T18:45:32.500Z"})
        self.assertNotIn("notice", json.dumps(got))
        self.assertNotIn("requested", agentboard.claude_records(text + json.dumps(answer(OPUS5, requested=OPUS5)) + "\n"))
        self.assertNotIn("fallback", agentboard.claude_records(self.tail(
            {"type": "system", "subtype": "stop_hook_summary", "timestamp": "t"}, answer(OPUS))))

    def test_runtime_returns_as_before_and_extras_follow_the_session_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / ".claude" / "projects" / "-x" / "s1.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(self.tail(fallback("2026-11-13T11:00:00Z"), answer(OPUS5, requested=OPUS), torn=False))
            extra = {}
            out = agentboard.runtime("claude-code", "s1", env={"CLAUDE_EFFORT": "max"}, procs=[], home=home, extra=extra)
            self.assertEqual(out, {"model": OPUS5, "model_from": "session", "effort": "max"})
            self.assertEqual(set(extra), {"requested", "fallback", "record_at"})
            self.assertEqual(extra["record_at"], path.stat().st_size)
            declared = {}
            agentboard.runtime("claude-code", "s1", env={"AGENTBOARD_MODEL": "m"}, procs=[], home=home, extra=declared)
            self.assertEqual(declared, {})
            roll = home / ".codex" / "sessions" / "2026" / "11" / "13" / "rollout-2026-11-13T10-00-00-c9.jsonl"
            roll.parent.mkdir(parents=True)
            roll.write_text(json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "high"}}) + "\n")
            codex = {}
            self.assertEqual(agentboard.runtime("codex", "c9", env={}, procs=[], home=home, extra=codex),
                             {"model": "gpt-6-astra", "model_from": "session", "effort": "high"})
            self.assertEqual(codex, {})


class SpanTest(unittest.TestCase):
    """The session file is read from where the previous board call stopped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.jsonl"

    def test_from_the_previous_call_the_tail_or_the_cap_whichever_fits(self):
        self.path.write_bytes(b"a\n" * 10 + b"half a li")
        text, at = agentboard.read_span(self.path)
        self.assertEqual((text, at), ("a\n" * 10 + "half a li", 20))
        with mock.patch.object(agentboard, "SESSION_TAIL", 6):
            # the last 6 bytes hold no whole line, so the span's own start is where the next call begins
            self.assertEqual(agentboard.read_span(self.path), ("f a li", 23))
            self.assertEqual(agentboard.read_span(self.path, 4), ("a\n" * 8 + "half a li", 20))
            self.assertEqual(agentboard.read_span(self.path, 999), ("f a li", 23))
            with mock.patch.object(agentboard, "SPAN_MAX", 13):
                self.assertEqual(agentboard.read_span(self.path, 0)[0], "a\n" * 2 + "half a li")

    def test_the_previous_call_is_found_by_its_session(self):
        events = [{"session": "s1", "record_at": 10}, {"session": "s2", "record_at": 99}, {"session": "s1"},
                  {"session": "s1", "record_at": True}]
        self.assertEqual(agentboard.last_record_at(events, "s1"), 10)
        self.assertIsNone(agentboard.last_record_at(events, "s3"))
        self.assertIsNone(agentboard.last_record_at(events, None))


class FlagTest(unittest.TestCase):
    def setUp(self):
        self.n = 0

    def ev(self, name="research", model=OPUS, source="session", sid="s1", minutes=0, reader=2, kind="status", **extra):
        self.n += 1
        e = {"id": self.n, "ts": (AT + dt.timedelta(minutes=minutes)).isoformat(), "type": kind, "from": name,
             "session": sid, "model": model, "model_from": source, **extra}
        if reader:
            e["model_reader"] = reader
        return e

    def flags(self, *events):
        return [(f["id"], f["kind"], f["from"], f["to"], f["safeguard"]) for f in agentboard.model_flags(list(events))]

    def test_a_change_raises_once_and_only_from_the_seats_own_record(self):
        self.assertEqual(self.flags(self.ev(), self.ev(), self.ev(model=OPUS5), self.ev(model=OPUS5)),
                         [(3, "change", OPUS, OPUS5, False)])
        self.n = 0
        self.assertEqual(self.flags(self.ev(source="config"), self.ev(model=OPUS5, source="config")), [])
        self.n = 0
        self.assertEqual(self.flags(self.ev(reader=None), self.ev(), self.ev(model=OPUS5, reader=None)), [])
        self.n = 0
        self.assertEqual(self.flags(self.ev(sid="s1"), self.ev(sid="s2", model=OPUS5), self.ev(name="other", model=FABLE)), [])
        self.n = 0
        self.assertEqual(self.flags(self.ev(source="flags", model="opus"), self.ev(source="session")), [])

    def test_a_served_model_unlike_the_requested_one_is_a_safeguard_change(self):
        self.assertEqual(self.flags(self.ev(), self.ev(model=OPUS5, requested=OPUS)), [(2, "change", OPUS, OPUS5, True)])

    def test_a_new_fallback_raises_once_unless_a_change_already_names_it(self):
        fb = {"originalModel": OPUS, "fallbackModel": OPUS5, "trigger": "refusal", "timestamp": "2026-11-13T12:30:00Z"}
        self.assertEqual(self.flags(self.ev(minutes=10), self.ev(model=OPUS5, fallback=fb, minutes=40),
                                    self.ev(model=OPUS5, fallback=fb, minutes=50)), [(2, "change", OPUS, OPUS5, True)])
        self.n = 0
        self.assertEqual(self.flags(self.ev(name="other"), self.ev(model=OPUS5, fallback=fb, minutes=40)),
                         [(2, "change", OPUS, OPUS5, True)])
        self.n = 0
        got = agentboard.model_flags([self.ev(), self.ev(model=OPUS5, minutes=20), self.ev(model=OPUS5, fallback=fb, minutes=40)])
        self.assertEqual([(f["id"], f["safeguard"], f["fallback"]) for f in got], [(2, True, fb)])

    def test_a_cleared_fallback_still_in_the_tail_raises_nothing_more(self):
        fb = {"originalModel": OPUS, "fallbackModel": OPUS5, "trigger": "refusal", "timestamp": "2026-11-13T12:30:00Z"}
        events = [self.ev(minutes=10), self.ev(model=OPUS5, fallback=fb, minutes=40)]
        events.append(self.ev(kind="clear", name="lead", flag=2, reason="known", reader=None))
        events += [self.ev(model=OPUS5, fallback=fb, minutes=50), self.ev(model=OPUS5, fallback=fb, minutes=60)]
        self.assertEqual([f["id"] for f in agentboard.model_flags(events)], [2])
        self.assertEqual(agentboard.open_flags(events), [])

    def test_records_from_before_the_first_reader_event_raise_nothing(self):
        old = {"originalModel": OPUS, "fallbackModel": OPUS5, "trigger": "refusal", "timestamp": "2026-11-12T18:45:32Z"}
        self.assertEqual(self.flags(self.ev(model=OPUS5, fallback=old, stopped="2026-11-12T18:42:30Z")), [])

    def test_a_stop_alone_raises_the_lighter_flag_once(self):
        got = agentboard.model_flags([self.ev(), self.ev(stopped="2026-11-13T12:05:00Z", minutes=10),
                                      self.ev(stopped="2026-11-13T12:05:00Z", minutes=20)])
        self.assertEqual([(f["id"], f["kind"], f["from"], f["to"], f["stopped"]) for f in got],
                         [(2, "stopped", OPUS, OPUS, "2026-11-13T12:05:00Z")])
        self.assertEqual(agentboard.flag_words(got[0]), f"stopped at {agentboard.stamp('2026-11-13T12:05:00Z')}, model unchanged")

    def test_a_deliberate_switch_raises_nothing_and_an_auto_one_raises_at_once(self):
        def sw(source):
            self.n += 1
            return {"id": self.n, "ts": AT.isoformat(), "type": "switch", "from": "research", "session": "s1",
                    "was": OPUS, "now": FABLE, "source": source}
        for source in agentboard.DELIBERATE:
            self.n = 0
            self.assertEqual(self.flags(self.ev(), sw(source), self.ev(model="claude-fable-5-2[1m]")), [], source)
        for source in ("auto", "unknown"):
            self.n = 0
            self.assertEqual(self.flags(self.ev(), sw(source), self.ev(model=FABLE)), [(2, "change", OPUS, FABLE, False)])

    def test_a_clear_from_a_lead_closes_a_flag(self):
        events = [self.ev(), self.ev(model=OPUS5), self.ev(kind="clear", name="bob", flag=2, reason="r", reader=None)]
        self.assertIsNone(agentboard.model_flags(events)[0]["cleared"])
        events.append(self.ev(kind="clear", name="lead", flag=2, reason="wanted", reader=None))
        self.assertEqual(agentboard.model_flags(events)[0]["cleared"]["by"], "lead")
        self.assertEqual(agentboard.open_flags(events), [])
        events.append(self.ev(model=OPUS))
        self.assertEqual([f["id"] for f in agentboard.open_flags(events)], [5])


class FlagCliTest(Cli):
    def test_a_fallback_in_the_seats_own_record_flags_who_and_the_inboxes(self):
        self.seat("s1", answer(OPUS))
        self.ok("status", "working", name="research", env=self.claude("s1"))
        self.ok("post", "research", "a note", name="lead")
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
        self.seat("s1", stop_notice(stamp), fallback(stamp), answer(OPUS5, requested=OPUS))
        self.ok("status", "still working", name="research", env=self.claude("s1"))
        e = self.events()[-1]
        self.assertEqual((e["model"], e["requested"], e["fallback"]["fallbackModel"], e["model_reader"]), (OPUS5, OPUS, OPUS5, 2))
        words = f"{OPUS} to {OPUS5} since {agentboard.when(e['ts'])}, after a safeguard stop"
        self.assertIn(f", MODEL {words} (flag {e['id']}), seen", self.ok("who"))
        self.assertIn(f"flag {e['id']}: research's model {words}", self.ok("inbox", name="lead"))
        self.assertNotIn("flag", self.ok("inbox", name="lead"))
        self.assertIn(f"flag {e['id']}: research's model", self.ok("inbox", name="research"))
        self.assertNotIn("flag", self.ok("inbox", name="agent-b"))
        self.refused("clear", str(e["id"]), "--reason", "fine", name="research", reason="only lead clears a model flag")
        self.refused("clear", "99", "--reason", "fine", name="lead", reason="there is no model flag 99")
        self.refused("clear", str(e["id"]), name="lead", reason="--reason")
        self.assertIn(f"cleared flag {e['id']}", self.ok("clear", str(e["id"]), "--reason", "the fallback is known", name="lead"))
        self.refused("clear", str(e["id"]), "--reason", "again", name="lead", reason=f"flag {e['id']} was cleared by lead")
        self.assertNotIn("MODEL", self.ok("who"))
        self.assertFalse(self.wakes("research"))

    def test_a_stop_far_before_the_next_call_still_raises_its_flag(self):
        # a stop far more than the 256 KiB tail before the next board call
        self.seat("s1", answer(OPUS))
        self.ok("status", "working", name="research", env=self.claude("s1"))
        first = self.events()[-1]
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
        pad = [{"type": "user", "message": {"role": "user", "content": "x" * 1000}} for _ in range(400)]
        self.seat("s1", stop_notice(stamp), *pad, answer(OPUS))
        size = (self.home / ".claude" / "projects" / "-x" / "s1.jsonl").stat().st_size
        self.assertGreater(size - first["record_at"], agentboard.SESSION_TAIL + 100000)
        self.ok("status", "still working", name="research", env=self.claude("s1"))
        e = self.events()[-1]
        self.assertEqual((e["stopped"], e["record_at"]), (stamp, size))
        got = agentboard.open_flags(self.events())
        self.assertEqual([(f["id"], f["kind"], f["stopped"]) for f in got], [(e["id"], "stopped", stamp)])
        self.ok("status", "and again", name="research", env=self.claude("s1"))
        self.assertNotIn("stopped", self.events()[-1])
        self.assertEqual(len(agentboard.open_flags(self.events())), 1)

    def test_an_old_fallback_in_the_tail_raises_nothing(self):
        self.seat("s1", fallback("2026-11-12T18:45:32.000Z"), answer(OPUS5))
        self.ok("status", "working", name="research", env=self.claude("s1"))
        self.ok("status", "working", name="research", env=self.claude("s1"))
        self.assertEqual(agentboard.model_flags(self.events()), [])


class SwitchTest(Cli):
    def hook(self, **fields):
        return self.board("switch", "--hook", stdin=json.dumps(fields))

    def test_the_hook_records_the_switch_for_the_seat_that_used_the_session(self):
        self.seat("s1", answer(OPUS))
        self.ok("status", "working", name="agent-b", env=self.claude("s1"))
        r = self.hook(session_id="s1", from_model=OPUS, to_model=FABLE, requested_model=FABLE, source="picker")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"switch recorded for agent-b: {OPUS} to {FABLE} (picker)"))
        e = self.events()[-1]
        self.assertEqual((e["type"], e["from"], e["session"], e["was"], e["now"], e["source"]),
                         ("switch", "agent-b", "s1", OPUS, FABLE, "picker"))
        self.assertFalse(self.wakes("agent-b"))
        self.seat("s1", answer(FABLE))
        self.ok("status", "working", name="agent-b", env=self.claude("s1"))
        self.assertEqual(agentboard.model_flags(self.events()), [])
        self.hook(session_id="s1", from_model=FABLE, to_model=OPUS5, source="auto")
        self.assertEqual([(f["kind"], f["from"], f["to"]) for f in agentboard.model_flags(self.events())],
                         [("change", FABLE, OPUS5)])

    def test_nothing_is_written_without_a_known_session_or_usable_input(self):
        for stdin in ("", "not json", "[1]", json.dumps({"session_id": "../x"}), json.dumps({"session_id": "s9", "to_model": OPUS})):
            r = self.board("switch", "--hook", stdin=stdin)
            self.assertEqual(r.returncode, 0, stdin)
            self.assertIn("nothing written", r.stdout)
            self.assertIsNone(self.raw())
        self.assertFalse(self.dir.exists())
        self.refused("switch", reason="run it as switch --hook")


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        for patch in (mock.patch.dict(os.environ, {"AGENTBOARD_DIR": str(self.directory)}),
                      mock.patch.object(agentboard, "now", return_value=AT)):
            patch.start()
            self.addCleanup(patch.stop)

    def seed(self, *events):
        with (self.directory / "board.jsonl").open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_the_page_gets_acks_closes_flags_and_pinned_line(self):
        ts = lambda m: (AT - dt.timedelta(minutes=m)).isoformat()  # noqa: E731
        self.seed({"id": 1, "ts": ts(50), "type": "msg", "from": "alice", "to": "bob", "text": "root"},
                  {"id": 2, "ts": ts(40), "type": "msg", "from": "bob", "to": "alice", "text": "re", "re": 1},
                  {"id": 3, "ts": ts(30), "type": "ack", "from": "alice", "msg": 2, "to": "bob", "word": "thanks"},
                  {"id": 4, "ts": ts(20), "type": "close", "from": "bob", "msg": 2, "root": 1, "to": "alice", "note": "done"},
                  {"id": 5, "ts": ts(15), "type": "status", "from": "research", "session": "s1", "model": OPUS,
                   "model_from": "session", "model_reader": 2, "text": "a"},
                  {"id": 6, "ts": ts(10), "type": "status", "from": "research", "session": "s1", "model": OPUS5,
                   "model_from": "session", "model_reader": 2, "requested": OPUS, "text": "b"})
        (self.directory / "pinned.txt").write_text("Lean week.\n")
        s = agentboard.state()
        by_id = {m["id"]: m for m in s["messages"]}
        self.assertEqual(by_id[2]["acks"], [{"by": "alice", "word": "thanks", "at": ts(30)}])
        self.assertEqual(by_id[1]["thread_closed"], {"by": "bob", "at": ts(20), "note": "done"})
        self.assertIsNone(by_id[2]["thread_closed"])
        self.assertEqual((s["threads"][0]["closed"]["by"], s["threads"][0]["reopened_by"]), ("bob", None))
        self.assertEqual([(f["id"], f["from"], f["to"], f["safeguard"], f["session"]) for f in s["flags"]], [(6, OPUS, OPUS5, True, "s1")])
        self.assertEqual(next(a for a in s["agents"] if a["name"] == "research")["flag"]["id"], 6)
        self.assertIsNone(next(a for a in s["agents"] if a["name"] == "alice")["flag"])
        self.assertEqual(s["pinned"], "Lean week.")
        self.assertNotIn("flowers", s)
        json.dumps(s)

    def test_an_empty_board_has_the_new_keys_and_nothing_is_written(self):
        s = agentboard.state()
        self.assertEqual((s["flags"], s["pinned"]), ([], None))
        self.assertEqual(list(self.directory.iterdir()), [])


class PageTest(unittest.TestCase):
    def test_no_flowers_and_nothing_moves(self):
        # nothing decorative is added; the header, the footer and the empty states are as before
        self.assertFalse(hasattr(agentboard, "FLOWERS"))
        page = agentboard.PAGE + agentboard.SCRIPT + agentboard.STYLE
        for gone in ("flower", "sprig", "meadow", "petal", "leaf", "<svg", "ellipse"):
            self.assertNotIn(gone, page.lower())
        self.assertIn('<span class="mark" aria-hidden="true"></span>', agentboard.PAGE)
        self.assertIn('<footer>Read-only: this page never writes to the board.</footer>', agentboard.PAGE)
        self.assertIn("box.append(el('p', 'empty', 'No seats yet'));", agentboard.SCRIPT)
        self.assertIn("}), 'No notices to all seats');", agentboard.SCRIPT)
        for still in ("animation", "transition", "@keyframes", "<animate"):
            self.assertNotIn(still, agentboard.STYLE + agentboard.SCRIPT)
        self.assertNotRegex(agentboard.STYLE + agentboard.SCRIPT, r"url\(|https?://(?!www\.w3\.org/2000/svg')")

    def test_the_new_marks_use_the_pages_own_colours(self):
        added = agentboard.STYLE[agentboard.STYLE.index(".pinned"):]
        self.assertNotRegex(added, r"#[0-9a-fA-F]{3,6}\b")
        self.assertIn(".alert.flag { color: var(--stop); background: var(--stop-bg); }", agentboard.STYLE)

    def test_the_seat_card_flag_and_the_summary_tile(self):
        self.assertIn("if (a.flag) b.append(el('div', 'alert flag', flagText(a.flag)));", agentboard.SCRIPT)
        self.assertIn("'model flags'", agentboard.SCRIPT)
        self.assertIn("'thread closed'", agentboard.SCRIPT)
        self.assertIn("'Pinned: '", agentboard.SCRIPT)


if __name__ == "__main__":
    unittest.main()
