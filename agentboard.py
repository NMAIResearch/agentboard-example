#!/usr/bin/env python3
"""Agent board: messages, status lines and folder claims shared by every agent on this machine, whatever its harness.

    agentboard.py post TO TEXT...          message one agent by board name, or all
    agentboard.py inbox [--peek | --all]   your unread messages, one line each; marks them read unless --peek;
                                           --all shows the newest, read or not, for a session taking over a role
    agentboard.py read ID                  one message in full, with its replies; marks it read
    agentboard.py wait --as NAME           wait for unread messages without marking them read; timeout exits 3
    agentboard.py nudge NAME               print the recorded session and a resume command; never run it
    agentboard.py status TEXT...           what you are doing now; an agent silent for two hours shows as quiet
    agentboard.py claim PATH [--note TEXT] claim a folder or file before writing in it; refused while another
                                           agent holds an overlapping claim
    agentboard.py release PATH|ID          release your claim; another agent's needs --force --reason, recorded
    agentboard.py done TEXT...             release all your active claims and set your status in one locked write
    agentboard.py check PATH               who holds PATH: exit 0 if free or yours, 1 if another agent holds it
    agentboard.py who | claims | log       agents with their status, active claims, recent events
    agentboard.py widget                   short text for the desktop widget
    agentboard.py serve [--port 7101]      read-only live page on 127.0.0.1

Give your board name with --as NAME or AGENTBOARD_NAME. Everything is one append-only file, board.jsonl in
AGENTBOARD_DIR (default ~/.local/state/agentboard), written under a lock and never rewritten. Names are self-declared,
not authenticated: a post is never a person's approval, and a claim is advisory.
Every event carries its session: the harness's own session id where the environment gives one, else pid<PID>-<start>
for the harness's process, so two sessions under one name can be told apart, and the model and effort where the
harness records them (session record, command line or configured default).
Standard library only."""
import argparse
import datetime as dt
import fcntl
import json
import math
import os
import re
import shlex
import sys
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_DIR = str(Path.home() / ".local" / "state" / "agentboard")
HOME_ROOT = os.path.realpath(Path.home())
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
LIMITS = {"text": 600, "status": 200, "note": 200, "reason": 200}
QUIET = dt.timedelta(hours=2)
RECENT = dt.timedelta(hours=1)
INBOX_MAX = 10
PAGE_MESSAGE_LIMIT = 15
FOLD_AFTER = dt.timedelta(days=1)
PREVIEW = 160
WAIT_TIMEOUT = 3
WAIT_PREVIEW = 120
PORT = 7101
FRAME_ANCESTORS = "http://127.0.0.1:5280 http://localhost:5280"
SESSION_VARS = ("AGENTBOARD_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "KIMI_SESSION_ID")
SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
HARNESSES = {"claude": "claude-code", "codex": "codex", "kimi": "kimi", "agy": "antigravity", "antigravity": "antigravity",
             "gemini": "gemini", "hermes": "hermes", "cline": "cline"}


class Refused(Exception):
    """A request the board turns down; the message says why."""


def board_dir() -> Path:
    return Path(os.environ.get("AGENTBOARD_DIR", DEFAULT_DIR))


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)


def load(directory: Path | None = None) -> tuple[list[dict], int]:
    """Every well-formed event in file order, and the number of lines skipped as torn or malformed."""
    events, skipped = [], 0
    try:
        with open((directory or board_dir()) / "board.jsonl", encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line) if line.endswith("\n") else None
                except ValueError:
                    event = None
                if (isinstance(event, dict) and isinstance(event.get("id"), int)
                        and all(isinstance(event.get(k), str) for k in ("type", "ts", "from"))):
                    events.append(event)
                else:
                    skipped += 1
    except FileNotFoundError:
        pass
    return events, skipped


def append(event: dict, guard=None) -> tuple[dict, bool]:
    """Append one event under the board's lock; return it with its id and time, and whether it was written.
    `guard(events, event)` runs under the same lock: it raises Refused to turn the event down, or returns an existing
    event, in which case nothing is written."""
    directory = board_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "board.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        events, _ = load(directory)
        existing = guard(events, event) if guard else None
        if existing:
            return existing, False
        event = {"id": max((e["id"] for e in events), default=0) + 1, "ts": now().isoformat(timespec="seconds"), **event}
        data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        fd = os.open(directory / "board.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data  # end a torn line first, so it stays one skipped line of its own
            if os.write(fd, data) != len(data):
                raise OSError("short write to the board")
            os.fsync(fd)
        finally:
            os.close(fd)
    return event, True


def processes(limit: int = 20) -> list[tuple[int, str, list[str]]]:
    """This process's ancestors, nearest first, as (pid, start time in clock ticks since boot, command line); shorter
    where /proc cannot be read, and the start time empty where only the command line could be."""
    chain, pid = [], os.getppid()
    try:
        for _ in range(limit):
            if pid <= 1:
                break
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            try:
                with open(f"/proc/{pid}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
            except OSError:
                chain.append((pid, "", argv))
                break
            chain.append((pid, fields[19], argv))
            pid = int(fields[1])
    except (OSError, ValueError, IndexError):
        pass
    return chain


def ancestry(limit: int = 20) -> list[list[str]]:
    """The command lines of this process's ancestors, nearest first."""
    return [argv for _, _, argv in processes(limit)]


def harness(env=None, chain=None) -> str:
    """The harness running this command: AGENTBOARD_HARNESS if set, else the nearest ancestor process named after a
    known harness, else a harness's own environment variable, else shell. Declared, not authenticated."""
    env = os.environ if env is None else env
    if env.get("AGENTBOARD_HARNESS"):
        return env["AGENTBOARD_HARNESS"][:40]
    for argv in ancestry() if chain is None else chain:
        for arg in argv[:2]:
            if os.path.basename(arg) in HARNESSES:
                return HARNESSES[os.path.basename(arg)]
    if env.get("CLAUDECODE"):
        return "claude-code"
    for prefix, name in (("CODEX_", "codex"), ("KIMI", "kimi"), ("GEMINI_", "gemini"), ("ANTIGRAVITY", "antigravity")):
        if any(key.startswith(prefix) for key in env):
            return name
    return "shell"


def session(env=None, procs=None) -> str:
    """The harness's own session id where the environment gives one, else pid<PID>-<start> for the nearest ancestor
    process named after a known harness, or for the parent process when there is none. Declared, not authenticated."""
    env = os.environ if env is None else env
    for key in SESSION_VARS:
        if env.get(key):
            return env[key][:80]
    procs = processes() if procs is None else procs
    for pid, start, argv in procs:
        if any(os.path.basename(arg) in HARNESSES for arg in argv[:2]):
            return f"pid{pid}-{start}"
    return f"pid{procs[0][0]}-{procs[0][1]}" if procs else f"pid{os.getppid()}-"


def tag(sid: str | None) -> str:
    """A session as people read it: the first 8 characters of a harness's id, or `pid N` for the harness process."""
    if not sid:
        return "no session"
    return "pid " + sid[3:].split("-")[0] if sid.startswith("pid") else sid[:8]


def tail_text(path: Path, size: int = 262144) -> str:
    """The last `size` bytes of a file, as text."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - size))
        return f.read().decode("utf-8", "replace")


def flags(argv: list[str]) -> tuple[str | None, str | None]:
    """Model and effort from a harness's own command line: -m or --model, --effort, --reasoning or
    --reasoning-effort, and Codex's -c model=... and -c model_reasoning_effort=...."""
    model = effort = None
    for i, arg in enumerate(argv):
        value = argv[i + 1] if i + 1 < len(argv) else None
        name, eq, inline = arg.partition("=")
        if eq and name in ("--model", "--effort", "--reasoning", "--reasoning-effort"):
            arg, value = name, inline
        if arg in ("-m", "--model") and value:
            model = value
        elif arg in ("--effort", "--reasoning", "--reasoning-effort") and value:
            effort = value
        elif arg in ("-c", "--config") and value and "=" in value:
            key, _, setting = value.partition("=")
            if key.strip() == "model":
                model = setting.strip().strip("\"'")
            elif key.strip() == "model_reasoning_effort":
                effort = setting.strip().strip("\"'")
    return model, effort


def session_record(harness_name: str, sid: str | None, home: Path, env) -> tuple[str | None, str | None]:
    """Model and effort from the harness's own record of this session: Claude Code's transcript and the effort it
    passes to commands, or the last turn of a Codex rollout."""
    effort = env.get("CLAUDE_EFFORT") or None if harness_name == "claude-code" else None
    if not sid or not SAFE_ID.fullmatch(sid):
        return None, effort
    if harness_name == "claude-code":
        for path in (home / ".claude" / "projects").glob(f"*/{sid}.jsonl"):
            models = re.findall(r'"model"\s*:\s*"([^"<]+)"', tail_text(path))
            return (models[-1] if models else None), effort
    if harness_name == "codex":
        for path in (home / ".codex" / "sessions").glob(f"*/*/*/rollout-*-{sid}.jsonl"):
            for row in reversed(tail_text(path).splitlines()):
                if '"turn_context"' in row:
                    try:
                        payload = json.loads(row).get("payload", {})
                    except ValueError:
                        continue
                    return payload.get("model"), payload.get("effort")
    return None, effort


def yaml_pair(text: str, top: str, key: str) -> str | None:
    """`key` one level inside the top-level block `top` of a plain YAML file."""
    inside, indent = False, None
    for row in text.splitlines():
        if not row.strip() or row.lstrip().startswith("#"):
            continue
        if not row[0].isspace():
            inside, indent = row.split(":", 1)[0].strip() == top, None
            continue
        depth = len(row) - len(row.lstrip())
        indent = depth if inside and indent is None else indent
        if inside and depth == indent and row.strip().split(":", 1)[0] == key:
            return row.split(":", 1)[1].split(" #", 1)[0].strip().strip("\"'") or None
    return None


def configured(harness_name: str, home: Path) -> tuple[str | None, str | None]:
    """Model and effort from the harness's configured default: what a new session would start with, not proof of
    what this one runs."""
    try:
        if harness_name == "codex":
            c = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
            return c.get("model"), c.get("model_reasoning_effort")
        if harness_name == "kimi":
            c = tomllib.loads((home / ".kimi-code" / "config.toml").read_text(encoding="utf-8"))
            name = c.get("default_model")
            entry = c.get("models", {}).get(name, {}) if name else {}
            return entry.get("display_name") or entry.get("model") or name, entry.get("default_effort")
        if harness_name == "antigravity":
            settings = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8"))
            return settings.get("model"), None
        if harness_name == "cline":
            state = json.loads((home / ".cline" / "data" / "globalState.json").read_text(encoding="utf-8"))
            want = f"actmode{state.get('actModeApiProvider', '')}modelid".lower()
            return next((v for k, v in state.items() if k.lower() == want), state.get("actModeApiModelId")), None
        if harness_name == "hermes":
            text = (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
            return yaml_pair(text, "model", "default"), yaml_pair(text, "agent", "reasoning_effort")
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None, None


def runtime(harness_name: str, sid: str | None, env=None, procs=None, home=None) -> dict:
    """The model and effort this agent runs, each from the first source that has it: `declared` (AGENTBOARD_MODEL,
    AGENTBOARD_EFFORT), `session` (the harness's own record of this session), `flags` (the harness's command line),
    then `config` (the harness's configured default). `model_from` names the model's source. Read, not authenticated."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    procs = processes() if procs is None else procs
    argv = []
    for _, _, cmd in procs:
        hit = next((i for i, a in enumerate(cmd[:2]) if HARNESSES.get(os.path.basename(a)) == harness_name), None)
        if hit is not None:
            argv = cmd[hit + 1:]
            break
    sources = (("declared", lambda: (env.get("AGENTBOARD_MODEL"), env.get("AGENTBOARD_EFFORT"))),
               ("session", lambda: session_record(harness_name, sid, home, env)),
               ("flags", lambda: flags(argv)),
               ("config", lambda: configured(harness_name, home)))
    out = {}
    for source, read in sources:
        model, effort = read()
        if model and "model" not in out:
            out["model"], out["model_from"] = str(model)[:80], source
        if effort and "effort" not in out:
            out["effort"] = str(effort)[:40]
        if "model" in out and "effort" in out:
            break
    return out


def runs(row: dict) -> str:
    """Model and effort as people read them, marked when the model is only the harness's configured default."""
    text = " ".join(str(x) for x in (row.get("model"), row.get("effort")) if x)
    return f"{text} by default" if text and row.get("model_from") == "config" else text


def ident(row: dict) -> str:
    """Harness, session and, where known, model and effort: `codex, session1, example-model high`."""
    return ", ".join(x for x in (row["harness"], tag(row["session"]), runs(row)) if x)


def me(args) -> str:
    name = (args.as_name or os.environ.get("AGENTBOARD_NAME", "")).strip().lower()
    if not NAME_RE.fullmatch(name):
        raise Refused("give your board name with --as NAME or AGENTBOARD_NAME: up to 40 of a-z, 0-9, '.', '_', '-'")
    return name


def base(args, kind: str) -> dict:
    name, sid = harness(), session()
    return {"type": kind, "from": me(args), "harness": name, "session": sid, **runtime(name, sid)}


def text_of(words, field: str) -> str:
    text = " ".join(words).strip()
    if not text:
        raise Refused(f"the {field} is empty")
    if len(text) > LIMITS[field]:
        raise Refused(f"the {field} has {len(text)} characters, more than {LIMITS[field]}: "
                      "put the detail in a file and post its path")
    return text


def canonical(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def overlaps(a: str, b: str) -> bool:
    """True when one path is the other or lies inside it."""
    a, b = a.rstrip(os.sep), b.rstrip(os.sep)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def active_claims(events: list[dict]) -> list[dict]:
    claims = {}
    for e in events:
        if e["type"] == "claim":
            claims[e["id"]] = e
        elif e["type"] == "release":
            claims.pop(e.get("claim"), None)
    return list(claims.values())


def unread(events: list[dict], name: str) -> list[dict]:
    seen = {i for e in events if e["type"] == "read" and e["from"] == name for i in e.get("msgs", [])}
    return [e for e in events if e["type"] == "msg" and e.get("to") in (name, "all")
            and e["from"] != name and e["id"] not in seen]


def agents(events: list[dict]) -> list[dict]:
    """One row per board name, with the session it last used. `recent` lists the distinct sessions that used the name
    in the last hour, oldest first, and `sessions` counts them, so a role handed to a new session while the old one
    still runs shows up."""
    rows, recent, hour = {}, {}, now() - RECENT
    for e in events:
        row = rows.setdefault(e["from"], {"name": e["from"], "harness": "?", "session": None, "model": None, "effort": None,
                                          "model_from": None, "status": "", "status_ts": None})
        row["last"], row["harness"] = e["ts"], e.get("harness", row["harness"])
        row["session"] = e.get("session", row["session"])
        if e.get("model") or e.get("effort"):
            row["model"], row["effort"], row["model_from"] = e.get("model"), e.get("effort"), e.get("model_from")
        if e["type"] == "status":
            row["status"], row["status_ts"] = e.get("text", ""), e["ts"]
        if e.get("session") and parse(e["ts"]) > hour:
            recent.setdefault(e["from"], {})[e["session"]] = None
    claims = active_claims(events)
    for row in rows.values():
        row["quiet"] = now() - parse(row["last"]) > QUIET
        row["claims"] = [c["path"] for c in claims if c["from"] == row["name"]]
        row["recent"] = list(recent.get(row["name"], {}))
        row["sessions"] = len(row["recent"])
    return sorted(rows.values(), key=lambda r: r["last"], reverse=True)


def when(ts: str) -> str:
    t = parse(ts).astimezone()
    return t.strftime("%H:%M") if t.date() == dt.datetime.now().astimezone().date() else t.strftime("%d %b %H:%M")


def ago(ts: str) -> str:
    minutes = int((now() - parse(ts)).total_seconds() // 60)
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago" if minutes < 2880 else f"{minutes // 1440} d ago"


def short(path: str) -> str:
    return "~/" + path[len(HOME_ROOT) + 1:] if path.startswith(HOME_ROOT + os.sep) else path


def flat(text: str) -> str:
    return " ".join(text.split())


def clip(text: str, width: int = 74) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def noted(event: dict) -> str:
    return f": {event['note']}" if event.get("note") else ""


def line(e: dict) -> str:
    text, more = flat(e["text"]), ""
    if len(text) > PREVIEW:
        text, more = text[:PREVIEW].rstrip(), f" [+{len(e['text']) - PREVIEW} chars: read {e['id']}]"
    reply = f" re #{e['re']}" if e.get("re") else ""
    return f"#{e['id']} {when(e['ts'])} {e['from']} > {e['to']}{reply}: {text}{more}"


def cmd_post(args):
    to = args.to.strip().lower()
    if to != "all" and not NAME_RE.fullmatch(to):
        raise Refused(f"{args.to!r} is not a board name; use an agent's name or all")
    event = {**base(args, "msg"), "to": to, "text": text_of(args.text, "text")}
    if args.re:
        event["re"] = args.re

    def guard(events, ev):
        if ev.get("re") and not any(e["type"] == "msg" and e["id"] == ev["re"] for e in events):
            raise Refused(f"there is no message #{ev['re']} to reply to")

    event, _ = append(event, guard)
    print(f"posted #{event['id']} to {to}")


def cmd_inbox(args):
    name = me(args)
    events = load()[0]
    if args.all:
        recent = [e for e in events if e["type"] == "msg" and e.get("to") in (name, "all") and e["from"] != name]
        print("\n".join(line(e) for e in recent[-INBOX_MAX:]) if recent else f"no messages for {name}")
        return
    waiting = unread(events, name)
    if not waiting:
        print("no unread messages")
        return
    shown = waiting[:INBOX_MAX]
    for e in shown:
        print(line(e))
    if len(waiting) > len(shown):
        print(f"{len(waiting) - len(shown)} more unread: run inbox again")
    if not args.peek:
        append({**base(args, "read"), "msgs": [e["id"] for e in shown]})


def cmd_read(args):
    name = me(args)
    events = load()[0]
    msg = next((e for e in events if e["type"] == "msg" and e["id"] == args.id), None)
    if not msg:
        raise Refused(f"there is no message #{args.id}")
    reply = f", reply to #{msg['re']}" if msg.get("re") else ""
    print(f"#{msg['id']} {when(msg['ts'])} from {msg['from']} ({msg.get('harness', '?')}) to {msg['to']}{reply}")
    print(msg["text"])
    replies = [e for e in events if e["type"] == "msg" and e.get("re") == msg["id"]]
    if replies:
        print("replies: " + ", ".join(f"#{e['id']} from {e['from']}" for e in replies))
    if msg in unread(events, name):
        append({**base(args, "read"), "msgs": [msg["id"]]})


def seconds(value: str) -> float:
    """A finite non-negative number of seconds."""
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("seconds must be finite and non-negative")
    return number


def interval_seconds(value: str) -> float:
    number = seconds(value)
    if number == 0:
        raise argparse.ArgumentTypeError("interval must be greater than zero")
    return number


def board_stamp(path: Path):
    """File identity and metadata; a missing board is a valid initial state."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def cmd_wait(args):
    name, directory = me(args), board_dir()
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    previous, first = None, True
    while True:
        try:
            stamp = board_stamp(directory / "board.jsonl")
            if first or stamp != previous:
                # Snapshot before loading, so an append during the read triggers another load.
                previous, first = stamp, False
                waiting = unread(load(directory)[0], name)
                if waiting:
                    for event in waiting:
                        read = shlex.join(["agentboard", "read", str(event["id"]), "--as", name])
                        print(f"#{event['id']} {event['ts']} {event['from']}: "
                              f"{flat(event['text'])[:WAIT_PREVIEW]} | {read}")
                    return 0
        except OSError as exc:
            raise Refused(f"cannot watch the board: {exc}") from exc
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return WAIT_TIMEOUT
        time.sleep(args.interval if remaining is None else min(args.interval, remaining))


def cmd_nudge(args):
    name = args.name.strip().lower()
    if not NAME_RE.fullmatch(name):
        raise Refused(f"{args.name!r} is not a board name")
    try:
        events = load()[0]
    except OSError as exc:
        raise Refused(f"cannot read the board: {exc}") from exc
    latest = next((event for event in reversed(events) if event["from"] == name), None)
    if latest is None:
        raise Refused(f"unknown board name {name!r}: no events from it")
    sid, harness_name = latest.get("session"), latest.get("harness", "?")
    print(f"{name}: session {flat(str(sid)) if sid else 'unknown'}, harness {flat(str(harness_name))}")
    if not isinstance(sid, str) or not SAFE_ID.fullmatch(sid) or sid.startswith("-") or re.fullmatch(r"pid\d+-\d*", sid):
        raise Refused(f"{name} has no resumable session id in its latest board record")
    waiting = unread(events, name)
    if not waiting:
        print(f"no unread messages for {name}; no nudge needed")
        return 0
    pointer = f"Board message #{waiting[-1]['id']} for {name}: agentboard read {waiting[-1]['id']} --as {name}"
    if harness_name == "codex":
        argv = ["codex", "resume", sid, pointer]
    elif harness_name == "kimi":
        argv = ["kimi", "--session", sid, "--prompt", pointer]
    elif harness_name in ("antigravity", "agy"):
        argv = ["agy", "--conversation", sid, "--prompt-interactive", pointer]
    elif harness_name in ("claude-code", "claude"):
        argv = ["claude", "--resume", sid, pointer]
    else:
        raise Refused(f"no verified resume syntax for harness {harness_name!r}")
    print(shlex.join(argv))
    if harness_name in ("claude-code", "claude"):
        print("A Claude session on this machine is reached with Claude Code's SendMessage; send only the board pointer.")


def cmd_status(args):
    event, _ = append({**base(args, "status"), "text": text_of(args.text, "status")})
    print(f"status set for {event['from']}")


def cmd_done(args):
    status = {**base(args, "status"), "text": text_of(args.text, "status")}
    directory = board_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "board.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        events, _ = load(directory)
        claims = [c for c in active_claims(events) if c["from"] == status["from"]]
        batch = [{**status, "type": "release", "claim": c["id"], "path": c["path"]} for c in claims]
        for release in batch:
            del release["text"]
        batch.append(status)
        first = max((e["id"] for e in events), default=0) + 1
        ts = now().isoformat(timespec="seconds")
        batch = [{"id": first + i, "ts": ts, **event} for i, event in enumerate(batch)]
        data = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in batch).encode()
        fd = os.open(directory / "board.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            if os.write(fd, data) != len(data):
                raise OSError("short write to the board")
            os.fsync(fd)
        finally:
            os.close(fd)
    for claim in claims:
        print(f"released #{claim['id']} {short(claim['path'])}")
    if not claims:
        print(f"no active claims for {status['from']}")
    print(f"status set for {status['from']}: {status['text']}")


def cmd_claim(args):
    path = canonical(args.path)
    if path == HOME_ROOT or HOME_ROOT.startswith(path.rstrip(os.sep) + os.sep):
        raise Refused(f"{path} is too broad to claim: claim the project folder you will write in")
    event = {**base(args, "claim"), "path": path}
    if args.note:
        event["note"] = text_of([args.note], "note")

    def guard(events, ev):
        for c in active_claims(events):
            if not overlaps(c["path"], ev["path"]):
                continue
            if c["from"] == ev["from"]:
                if c["path"] == ev["path"]:
                    return c
                continue
            raise Refused(f"{short(ev['path'])} overlaps {short(c['path'])}, claimed by {c['from']} "
                          f"(#{c['id']}, {when(c['ts'])}{noted(c)}); post to {c['from']} or choose another folder")

    event, written = append(event, guard)
    print(f"{'claimed' if written else 'already yours:'} #{event['id']} {short(event['path'])}")


def cmd_release(args):
    name = me(args)
    target = args.target.lstrip("#")
    if args.force and not args.reason:
        raise Refused("releasing another agent's claim needs --reason, which is recorded")

    def guard(events, ev):
        claims = active_claims(events)
        if target.isdigit():
            match = [c for c in claims if c["id"] == int(target)]
        else:
            match = [c for c in claims if c["path"] == canonical(target)]
            match = [c for c in match if c["from"] == name] or match
        if not match:
            raise Refused(f"no active claim matches {args.target}")
        c = match[0]
        if c["from"] != name and not args.force:
            raise Refused(f"#{c['id']} is held by {c['from']}; only its holder releases it, "
                          "or use --force --reason (recorded)")
        ev.update({"claim": c["id"], "path": c["path"]})
        if c["from"] != name:
            ev.update({"forced": True, "holder": c["from"], "reason": text_of([args.reason], "reason")})

    event, _ = append(base(args, "release"), guard)
    print(f"released #{event['claim']} {short(event['path'])}")


def cmd_check(args):
    name = (args.as_name or os.environ.get("AGENTBOARD_NAME", "")).strip().lower()
    path = canonical(args.path)
    hits = [c for c in active_claims(load()[0]) if overlaps(c["path"], path)]
    if not hits:
        print(f"free: no claim covers {short(path)}")
        return 0
    for c in hits:
        print(f"claimed by {c['from']} (#{c['id']}, {when(c['ts'])}): {short(c['path'])}{noted(c)}")
    return 0 if name and all(c["from"] == name for c in hits) else 1


def cmd_who(args):
    rows = agents(load()[0])
    if not rows:
        print("no agents on the board yet")
    for r in rows:
        quiet = ", QUIET" if r["quiet"] else ""
        claims = f", claims {len(r['claims'])}" if r["claims"] else ""
        twice = (f", {r['sessions']} sessions in the last hour ({', '.join(tag(s) for s in r['recent'])}): check only "
                 f"one acts as {r['name']}") if r["sessions"] > 1 else ""
        print(f"{r['name']} ({ident(r)}) seen {ago(r['last'])}{quiet}{claims}{twice}: "
              f"{r['status'] or 'no status'}")


def cmd_claims(args):
    claims = active_claims(load()[0])
    if not claims:
        print("no active claims")
    for c in claims:
        print(f"#{c['id']} {short(c['path'])}: {c['from']}, since {when(c['ts'])}{noted(c)}")


def cmd_log(args):
    events, skipped = load()
    for e in events[-args.n:]:
        print(json.dumps(e, ensure_ascii=False))
    if skipped:
        print(f"{skipped} torn or malformed lines skipped")


def older_than_day(ts: str, at: dt.datetime) -> bool:
    """Fold only timestamps strictly older than the display threshold."""
    return at - parse(ts) > FOLD_AFTER


def cmd_widget(args):
    events = load()[0]
    msgs = [e for e in events if e["type"] == "msg"][-args.lines:]
    out = [clip(f"Msg   {when(e['ts'])} {e['from']} > {e['to']}: {flat(e['text'])}") for e in reversed(msgs)] or ["Msg   none yet"]
    at = now()
    rows = agents(events)
    recent = [r for r in rows if not older_than_day(r["last"], at)]
    quiet_count = len(rows) - len(recent)
    for r in recent[: args.lines]:
        out.append(clip(f"Agent {r['name']} ({ident(r)}){' QUIET' if r['quiet'] else ''}: "
                        f"{flat(r['status']) or 'no status'}"))
    if quiet_count:
        out.append(clip(f"Agent quiet: {quiet_count}"))
    claims = active_claims(events)
    fresh = [c for c in claims if not older_than_day(c["ts"], at)]
    older_count = len(claims) - len(fresh)
    out += [clip(f"Owns  {c['from']}: {short(c['path'])}") for c in fresh[: args.lines]]
    if len(fresh) > args.lines:
        out.append(clip(f"Owns  {len(fresh) - args.lines} more: agentboard.py claims"))
    if older_count:
        out.append(clip(f"Owns  older claims: {older_count}"))
    print("\n".join(out))


def state(limit: int = 60) -> dict:
    events, skipped = load()
    at = now()
    msgs = [e for e in events if e["type"] == "msg"][-limit:]
    pick = lambda e, keys: {k: e[k] for k in keys if k in e}  # noqa: E731
    return {"generated": at.isoformat(timespec="seconds"),
            "folding": {"message_limit": PAGE_MESSAGE_LIMIT, "age_seconds": int(FOLD_AFTER.total_seconds())},
            "messages": [{**pick(e, ("id", "ts", "from", "to", "re", "text", "harness")), "session": tag(e.get("session"))}
                         for e in reversed(msgs)],
            "agents": [{**pick(r, ("name", "harness", "last", "status", "status_ts", "quiet", "claims", "sessions")),
                        "session": tag(r["session"]), "recent": [tag(s) for s in r["recent"]], "runs": runs(r),
                        "folded": older_than_day(r["last"], at)}
                       for r in agents(events)],
            "claims": [{**pick(c, ("id", "ts", "from", "path", "note")),
                        "folded": older_than_day(c["ts"], at),
                        "age_seconds": max(0, int((at - parse(c["ts"])).total_seconds()))}
                       for c in active_claims(events)],
            "skipped_lines": skipped}


PAGE = """<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent board</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header><h1>Agent board</h1><p id="updated">loading</p></header>
<div id="summary" class="summary" aria-label="Counts"></div>
<main>
<section class="panel" id="agents-panel"><h2>Agents</h2><ul id="agents"></ul>
<details id="older-agents" hidden><summary id="older-agents-summary">Quiet for over a day (0)</summary><ul id="older-agents-list"></ul></details></section>
<section class="panel" id="messages-panel"><h2>Messages <span class="hint">newest first</span></h2><ol id="messages"></ol>
<details id="older-messages" hidden><summary id="older-messages-summary">Older messages (0)</summary><ol id="older-messages-list"></ol></details></section>
<section class="panel" id="claims-panel"><h2>Claims</h2><ul id="claims"></ul>
<details id="older-claims" hidden><summary id="older-claims-summary">Claims older than a day (0)</summary><ul id="older-claims-list"></ul></details></section>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

SCRIPT = """const $ = id => document.getElementById(id);
const fmt = ts => {
  const d = new Date(ts);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'})
    : d.toLocaleString('en-GB', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
};
const homePrefix = __HOME_PREFIX__;
const place = path => path.startsWith(homePrefix) ? '~/' + path.slice(homePrefix.length) : path;
const age = seconds => seconds < 3600 ? Math.floor(seconds / 60) + ' min'
  : seconds < 86400 ? Math.floor(seconds / 3600) + ' h'
  : Math.floor(seconds / 86400) + ' d ' + Math.floor(seconds % 86400 / 3600) + ' h';
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function card(title, side, lines, extra) {
  const li = el('li', 'card' + (extra ? ' ' + extra : ''));
  const head = el('div', 'head');
  head.append(el('span', 'name', title), el('span', 'side', side));
  li.append(head);
  for (const [cls, text] of lines) if (text) li.append(el('div', cls, text));
  return li;
}
function fill(id, rows, empty) {
  $(id).replaceChildren(...(rows.length ? rows : empty ? [el('li', 'empty', empty)] : []));
}
function fold(id, rows, label) {
  $(id).hidden = rows.length === 0;
  $(id + '-summary').textContent = label + ' (' + rows.length + ')';
  fill(id + '-list', rows);
}
function tile(label, value, extra) {
  const t = el('div', 'tile' + (extra ? ' ' + extra : ''));
  t.append(el('span', 'value', String(value)), el('span', 'label', label));
  return t;
}
async function refresh() {
  try {
    const s = await (await fetch('/api/state', {cache: 'no-store'})).json();
    const active = s.agents.filter(a => !a.quiet), quiet = s.agents.filter(a => a.quiet);
    $('summary').replaceChildren(tile('active agents', active.length), tile('quiet', quiet.length, quiet.length ? 'warn' : ''),
      tile('messages', s.messages.length), tile('claims', s.claims.length));
    const agentCard = a => card(a.name, a.quiet ? 'quiet, last seen ' + fmt(a.last) : 'seen ' + fmt(a.last),
      [['meta', [a.harness, a.session, a.runs].filter(Boolean).join(' \\u00b7 ')],
       ['alert', a.sessions > 1 ? a.sessions + ' sessions used this name in the last hour: ' + a.recent.join(', ') : ''],
       ['text', a.status || 'no status']], a.quiet ? 'is-quiet' : '');
    const recentAgents = s.agents.filter(a => !a.folded);
    fill('agents', [...recentAgents.filter(a => !a.quiet), ...recentAgents.filter(a => a.quiet)].map(agentCard),
      s.agents.length ? '' : 'No agents yet');
    fold('older-agents', s.agents.filter(a => a.folded).map(agentCard), 'Quiet for over a day');
    const messageCard = m => card(m.from + ' \\u2192 ' + m.to, fmt(m.ts),
      [['meta', 'session ' + m.session + (m.re ? ' \\u00b7 reply to #' + m.re : '')], ['text', m.text]]);
    fill('messages', s.messages.slice(0, s.folding.message_limit).map(messageCard), 'No messages yet');
    fold('older-messages', s.messages.slice(s.folding.message_limit).map(messageCard), 'Older messages');
    const claimCard = c => card(c.from, age(c.age_seconds) + ' old, since ' + fmt(c.ts),
      [['path', place(c.path)], ['meta', c.note || '']]);
    fill('claims', s.claims.filter(c => !c.folded).map(claimCard), s.claims.length ? '' : 'No claims');
    fold('older-claims', s.claims.filter(c => c.folded).map(claimCard), 'Claims older than a day');
    $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString('en-GB') + ' \\u00b7 refreshes every 3 seconds';
    document.body.classList.remove('stale');
  } catch (e) {
    $('updated').textContent = 'Board not reachable';
    document.body.classList.add('stale');
  }
}
refresh();
setInterval(refresh, 3000);
""".replace("__HOME_PREFIX__", json.dumps(HOME_ROOT + os.sep))

STYLE = """:root { color-scheme: light; --bg: #f4f3ee; --panel: #ffffff; --fg: #1b1b19; --muted: #55554f; --line: #d9d8d0; --accent: #1f5f4f; --warn: #9a4a1f; --warn-bg: #fbece2; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --bg: #141413; --panel: #1e1e1c; --fg: #f0efea; --muted: #b6b5ae; --line: #3a3a35; --accent: #8bd3bb; --warn: #f2ab7f; --warn-bg: #3b261a; } }
* { box-sizing: border-box; }
[hidden] { display: none; }
body { margin: 0; padding: 24px clamp(16px, 3vw, 40px); background: var(--bg); color: var(--fg); font: 18px/1.55 system-ui, sans-serif; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 20px; margin-bottom: 16px; }
h1 { font-size: 30px; line-height: 1.2; margin: 0; }
#updated { margin: 0; color: var(--muted); font-size: 16px; }
.stale #updated { color: var(--warn); font-weight: 600; }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
.tile { display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 18px; }
.tile .value { font-size: 34px; font-weight: 700; line-height: 1.1; font-variant-numeric: tabular-nums; }
.tile .label { color: var(--muted); font-size: 16px; }
.tile.warn .value { color: var(--warn); }
main { display: grid; gap: 20px; grid-template-columns: minmax(0, 1fr); grid-template-areas: "agents" "messages" "claims"; align-items: start; }
@media (min-width: 1100px) { main { grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); grid-template-areas: "agents messages" "claims messages"; grid-template-rows: auto 1fr; align-items: start; } }
#agents-panel { grid-area: agents; }
#messages-panel { grid-area: messages; }
#claims-panel { grid-area: claims; }
.panel { min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; }
h2 { font-size: 22px; margin: 0 0 12px; }
h2 .hint { margin-left: 8px; color: var(--muted); font-size: 15px; font-weight: 400; }
ol, ul { display: grid; gap: 10px; list-style: none; margin: 0; padding: 0; }
details { margin-top: 12px; }
summary { cursor: pointer; color: var(--accent); overflow-wrap: anywhere; }
summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
details[open] > summary { margin-bottom: 10px; }
.card { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.card.is-quiet { opacity: .78; }
.head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; }
.name { color: var(--accent); font-weight: 700; overflow-wrap: anywhere; }
.side { color: var(--muted); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.is-quiet .side { color: var(--warn); font-weight: 600; }
.meta { color: var(--muted); font-size: 16px; overflow-wrap: anywhere; }
.text { margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
.path { margin-top: 4px; font-family: ui-monospace, monospace; font-size: 16px; overflow-wrap: anywhere; }
.alert { margin-top: 6px; padding: 2px 8px; border-radius: 6px; background: var(--warn-bg); color: var(--warn); font-size: 16px; }
.empty { color: var(--muted); }
"""


class Handler(BaseHTTPRequestHandler):
    """Read-only: GET and HEAD for the page and /api/state, from a loopback Host header only."""
    server_version = "agentboard"
    routes = {"/": ("text/html", PAGE), "/app.js": ("text/javascript", SCRIPT), "/style.css": ("text/css", STYLE)}

    def do_GET(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            return self.reply(403, "text/plain", b"forbidden\n")
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            return self.reply(200, "application/json", json.dumps(state(), ensure_ascii=False).encode())
        if path in self.routes:
            kind, body = self.routes[path]
            return self.reply(200, kind, body.encode())
        self.reply(404, "text/plain", b"not found\n")

    do_HEAD = do_GET

    def do_POST(self):
        self.reply(405, "text/plain", b"read-only\n")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def reply(self, code: int, kind: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", f"default-src 'self'; frame-ancestors 'self' {FRAME_ANCESTORS}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def make_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    return server


def cmd_serve(args):
    server = make_server(args.port)
    print(f"agent board on http://127.0.0.1:{server.server_address[1]}/ (read-only)", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="as_name", default="", help="your board name (or AGENTBOARD_NAME)")
    ap = argparse.ArgumentParser(prog="agentboard.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    def command(name, func, help_text):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.set_defaults(func=func)
        return p

    p = command("post", cmd_post, "message one agent by board name, or all")
    p.add_argument("to")
    p.add_argument("text", nargs="+")
    p.add_argument("--re", type=int, help="the message this replies to")
    p = command("inbox", cmd_inbox, "your unread messages")
    p.add_argument("--peek", action="store_true", help="leave them unread")
    p.add_argument("--all", action="store_true", help="the newest messages for you, read or not, without marking them; "
                   "for a session taking over a role")
    command("read", cmd_read, "one message in full").add_argument("id", type=lambda s: int(s.lstrip("#")))
    p = command("wait", cmd_wait, "wait without writing or marking read; timeout exits 3")
    p.description = "Wait for unread messages; exit 0 on messages, 3 on timeout with no output. Never writes."
    p.add_argument("--timeout", type=seconds, default=None, help="seconds to wait (default: indefinitely; 0: check once)")
    p.add_argument("--interval", type=interval_seconds, default=1.0, help="seconds between metadata checks (default: 1)")
    command("nudge", cmd_nudge, "print a resume command for NAME's newest unread message; never run it").add_argument("name")
    command("status", cmd_status, "what you are doing now").add_argument("text", nargs="+")
    command("done", cmd_done, "release all your claims and set your status").add_argument("text", nargs="+")
    p = command("claim", cmd_claim, "claim a folder or file before writing in it")
    p.add_argument("path")
    p.add_argument("--note", help="what you are doing there")
    p = command("release", cmd_release, "release a claim by path or id")
    p.add_argument("target")
    p.add_argument("--force", action="store_true", help="release another agent's claim (needs --reason)")
    p.add_argument("--reason", default="")
    command("check", cmd_check, "who holds a path").add_argument("path")
    command("who", cmd_who, "agents with their status")
    command("claims", cmd_claims, "active claims")
    command("log", cmd_log, "recent events").add_argument("-n", type=int, default=20)
    command("widget", cmd_widget, "short text for the desktop widget").add_argument("--lines", type=int, default=4)
    command("serve", cmd_serve, "read-only live page on 127.0.0.1").add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except Refused as exc:
        print(f"agentboard: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
