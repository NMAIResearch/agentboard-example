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
    agentboard.py ack ID WORD              acknowledge a message with seen, thanks or noted; wakes no one
    agentboard.py close ID [--note TEXT]   mark a thread closed; a reply after it reopens the thread
    agentboard.py clear FLAG --reason TEXT clear a model flag (a name in LEADS)
    agentboard.py switch --hook            record a model switch from Claude Code's PostModelSwitch hook (stdin)
    agentboard.py who | claims | log       agents with their status, active claims, recent events
    agentboard.py widget                   short text for the desktop widget
    agentboard.py serve [--port 7101]      read-only live page on 127.0.0.1

Give your board name with --as NAME or AGENTBOARD_NAME. Everything is one append-only file, board.jsonl in
AGENTBOARD_DIR (default ~/.local/state/agentboard), written under a lock and never rewritten. Names are self-declared,
not authenticated: a post is never a person's approval, and a claim is advisory.
Every event carries its session: the harness's own session id where the environment gives one, else pid<PID>-<start>
for the harness's process, so two sessions under one name can be told apart, and the model and effort where the
harness records them (session record, command line or configured default).
`post`, `status`, `done`, `claim` and `close` take --check: count the text against its limit and write nothing.
`inbox` also shows the pinned line from pinned.txt beside the board when it changes or once a day, acknowledgements
and closed threads for you, and model flags. Standard library only."""
import argparse
import datetime as dt
import fcntl
import hashlib
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
# The pinned line, acknowledgements, closed threads and the model flag
PINNED, PINNED_EVERY = "pinned.txt", dt.timedelta(hours=24)  # shown when it changes, else once a day per seat
ACK_WORDS = ("seen", "thanks", "noted")
LEADS = ("lead",)  # the names whose inbox shows every seat's model flags, and who clear them; set your own
INBOX_EXTRAS = 5  # acknowledgements, closes and flags shown per inbox, then a count
MODEL_READER = 2  # events whose model came from the record reader below; only these raise flags
DELIBERATE = ("command", "picker", "sdk", "resume")  # PostModelSwitch sources that raise no flag; `auto` raises one
SWITCH_SOURCES = DELIBERATE + ("auto",)
SYSTEM_FALLBACK = re.compile(r"fallback|refusal", re.IGNORECASE)  # Claude Code's system records of a fallback or refusal
SYSTEM_STOPPED = re.compile(r"safeguards? stopped", re.IGNORECASE)
FALLBACK_KEYS = ("originalModel", "fallbackModel", "trigger", "scope", "timestamp")
# A Claude Code seat's session file is read from where its previous board call
# stopped (`record_at`), so that a fallback or a stop more than the tail before the next call is still seen; at most SPAN_MAX
SESSION_TAIL, SPAN_MAX = 262144, 64 * 1024 * 1024


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
        with open((directory or board_dir()) / "board.jsonl", "rb") as f:
            for line in f:
                try:
                    event = json.loads(line.decode("utf-8")) if line.endswith(b"\n") else None
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


def read_span(path: Path, since: int | None = None) -> tuple[str, int]:
    """A Claude Code session file from `since`, where this session's previous board call stopped reading, or from its last
    SESSION_TAIL bytes, whichever starts earlier, but at most its last SPAN_MAX bytes; as text, with the offset just after its
    last whole line, which the event keeps as `record_at`, so that a line still being written is read whole next time. A
    `since` past the file's end (a file replaced or cut) is ignored. A span holding no whole line (one line longer than
    the span) gives its own start."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - SESSION_TAIL)
        if isinstance(since, int) and 0 <= since <= size:
            start = max(min(start, since), size - SPAN_MAX, 0)
        f.seek(start)
        data = f.read(size - start)
    return data.decode("utf-8", "replace"), start + data.rfind(b"\n") + 1


def last_record_at(events: list[dict], sid: str | None) -> int | None:
    """Where this session's previous board call stopped reading its session file, from the board; None before its first."""
    for e in reversed(events):
        at = e.get("record_at")
        if sid and e.get("session") == sid and isinstance(at, int) and not isinstance(at, bool):
            return at
    return None


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


def claude_records(text: str) -> dict:
    """What the board reads from a span of a Claude Code session file, record by record and fields only:
    - `model`: the last main-loop answer's served model, from a record of type `assistant` (or of no type), not a
      sidechain's, whose `message.model` is a string not starting with `<` (Claude Code's `<synthetic>` notes);
    - `requested`: that answer's `requestedModel`, only when it differs from the served model;
    - `fallback`: the last `system` record whose subtype names a fallback or refusal, with its string FALLBACK_KEYS;
    - `stopped`: the time of the last `system` record whose content says a model's safeguards stopped a response.
    Lines that do not parse, the torn first line of the tail among them, are skipped, and a line naming neither a model
    nor a system record is not parsed at all. A tool call's own `model` argument sits inside the message's content, so it
    is never read as the model. No conversation text is kept."""
    out = {}
    for row in text.splitlines():
        if '"model"' not in row and '"system"' not in row:
            continue
        try:
            rec = json.loads(row)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind, message = rec.get("type"), rec.get("message")
        if kind in ("assistant", None) and not rec.get("isSidechain") and isinstance(message, dict):
            model = message.get("model")
            if isinstance(model, str) and model.strip() and not model.startswith("<"):
                requested = rec.get("requestedModel")
                out["model"] = model
                if isinstance(requested, str) and requested.strip() and requested != model:
                    out["requested"] = requested[:80]
                else:
                    out.pop("requested", None)
        elif kind == "system":
            subtype, content = rec.get("subtype"), rec.get("content")
            if isinstance(subtype, str) and SYSTEM_FALLBACK.search(subtype):
                found = {k: rec[k][:80] for k in FALLBACK_KEYS if isinstance(rec.get(k), str)}
                if found:
                    out["fallback"] = found
            elif isinstance(content, str) and SYSTEM_STOPPED.search(content) and isinstance(rec.get("timestamp"), str):
                out["stopped"] = rec["timestamp"][:40]
    return out


def session_record(harness_name: str, sid: str | None, home: Path, env, extra=None,
                   since: int | None = None) -> tuple[str | None, str | None]:
    """Model and effort from the harness's own record of this session: Claude Code's transcript and the effort it
    passes to commands, or the last turn of a Codex rollout. For Claude Code, `extra` (a dict) also gets the record
    reader's `requested`, `fallback` and `stopped` where the span read has them, and `record_at`, where it stopped;
    `since` is where the previous board call stopped (read_span)."""
    effort = env.get("CLAUDE_EFFORT") or None if harness_name == "claude-code" else None
    if not sid or not SAFE_ID.fullmatch(sid):
        return None, effort
    if harness_name == "claude-code":
        for path in (home / ".claude" / "projects").glob(f"*/{sid}.jsonl"):
            text, record_at = read_span(path, since)
            found = claude_records(text)
            if extra is not None:
                extra.update({k: found[k] for k in ("requested", "fallback", "stopped") if k in found})
                extra["record_at"] = record_at
            return found.get("model"), effort
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


def runtime(harness_name: str, sid: str | None, env=None, procs=None, home=None, extra=None, since=None) -> dict:
    """The model and effort this agent runs, each from the first source that has it: `declared` (AGENTBOARD_MODEL,
    AGENTBOARD_EFFORT), `session` (the harness's own record of this session), `flags` (the harness's command line),
    then `config` (the harness's configured default). `model_from` names the model's source. Read, not authenticated.
    `extra` (a dict) gets the session record's `requested`, `fallback` and `stopped` only when the model came from
    the session, so they always describe the model shown, with `record_at`; `since` goes to session_record. The return
    itself is as before."""
    env = os.environ if env is None else env
    found = {}
    home = Path.home() if home is None else home
    procs = processes() if procs is None else procs
    argv = []
    for _, _, cmd in procs:
        hit = next((i for i, a in enumerate(cmd[:2]) if HARNESSES.get(os.path.basename(a)) == harness_name), None)
        if hit is not None:
            argv = cmd[hit + 1:]
            break
    sources = (("declared", lambda: (env.get("AGENTBOARD_MODEL"), env.get("AGENTBOARD_EFFORT"))),
               ("session", lambda: session_record(harness_name, sid, home, env, found, since)),
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
    if extra is not None and out.get("model_from") == "session":
        extra.update(found)
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
    """The fields every event of this command carries. `model_reader` 2 marks the events whose model the record
    reader gave, the only ones that raise model flags. A Claude Code seat's session file is read from where
    its previous board call stopped."""
    who, name, sid, extra = me(args), harness(), session(), {}
    since = last_record_at(load()[0], sid) if name == "claude-code" else None
    found = runtime(name, sid, extra=extra, since=since)
    return {"type": kind, "from": who, "harness": name, "session": sid, **found, **extra, "model_reader": MODEL_READER}


def text_of(words, field: str) -> str:
    text = " ".join(words).strip()
    if not text:
        raise Refused(f"the {field} is empty")
    if len(text) > LIMITS[field]:
        over = len(text) - LIMITS[field]
        raise Refused(f"the {field} has {len(text)} characters, more than {LIMITS[field]} by {over}: cut {over}, "
                      "or put the detail in a file and post its path")
    return text


def checked(words, field: str) -> int:
    """--check: count the text as its command would, print the count or raise the command's own refusal, and write
    nothing."""
    text = text_of(words, field)
    n, spare = len(text), LIMITS[field] - len(text)
    print(f"{n} character{'' if n == 1 else 's'}, {spare} to spare of {LIMITS[field]}")
    return 0


def no_note(command: str) -> int:
    print(f"no note given: {command} has nothing to count")
    return 0


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


def pinned() -> str | None:
    """The pinned line: the first non-empty line of pinned.txt beside the board, white space folded, cut at the
    status limit with " (cut)"; None when the file is missing, empty or unreadable. A person edits the file by hand."""
    try:
        text = (board_dir() / PINNED).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    first = next((flat(row) for row in text.splitlines() if row.strip()), None)
    if not first:
        return None
    return first if len(first) <= LIMITS["status"] else first[:LIMITS["status"]].rstrip() + " (cut)"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pinned_due(events: list[dict], name: str, said: str, at: dt.datetime) -> bool:
    """Whether a seat's inbox shows the pinned line: when it differs from the line that seat was last shown (its last `read`
    with a `note`), and otherwise once PINNED_EVERY has passed since then."""
    last = next((e for e in reversed(events) if e["type"] == "read" and e["from"] == name
                 and isinstance(e.get("note"), str)), None)
    if last is None or last["note"] != digest(said):
        return True
    try:
        return at - parse(last["ts"]) >= PINNED_EVERY
    except (TypeError, ValueError):
        return True


def root_of(by_id: dict, e: dict) -> dict:
    """The first message of `e`'s thread: follow `re` while it names a message on the board, stopping at a loop."""
    root, seen = e, set()
    while isinstance(root.get("re"), int) and root["re"] in by_id and root["id"] not in seen:
        seen.add(root["id"])
        root = by_id[root["re"]]
    return root


def closures(events: list[dict]) -> dict:
    """Each thread ever closed, by its root's id: `closed` ({by, at, note}) while closed, else None with `reopened_by`,
    the first message after the last close (a reply reopens a thread without a command)."""
    msgs = [e for e in events if e["type"] == "msg"]
    by_id = {e["id"]: e for e in msgs}
    out = {}
    for e in events:
        if e["type"] == "close" and isinstance(e.get("root"), int) and e["root"] in by_id:
            out[e["root"]] = {"closed": {"by": e["from"], "at": e["ts"], "note": e.get("note")}, "reopened_by": None,
                              "close": e["id"]}
        elif e["type"] == "msg" and out:
            state = out.get(root_of(by_id, e)["id"])
            if state and state["closed"] and e["id"] > state["close"]:
                state["closed"], state["reopened_by"] = None, e["id"]
    for state in out.values():
        del state["close"]
    return out


def after_start(ts, start: dt.datetime) -> bool:
    """Whether a session record's time is at or after the board's first record-reader event; a time that does not
    parse counts as after, so that it is shown rather than hidden."""
    try:
        t = parse(ts)
    except (TypeError, ValueError):
        return True
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)) >= start


def model_flags(events: list[dict]) -> list[dict]:
    """The model flags, derived in board order and never stored. Per board
    name and session, from the events the record reader wrote (`model_reader` 2) whose model the seat itself gave
    (`model_from` declared, session or flags; never config):
    - a model unlike that session's previous one from the same source raises a `change` flag, "after a safeguard
      stop" when the answer was served by another model than requested or the event's fallback was a refusal;
    - a fallback record not seen before raises a `change` flag from its original to its fallback model, unless an
      open flag of that session already ends at that model, which then takes the record;
    - a safeguard stop not seen before, with neither, raises the lighter `stopped` flag;
    - a fallback or stop timestamped before the board's first record-reader event raises nothing;
    - a `switch` (Claude Code's PostModelSwitch hook) starts the session's comparison afresh; from a source other
      than a deliberate one it raises a `change` flag at once.
    A flag's id is the id of the event that raised it; a `clear` from a LEADS name closes it. Every flag, oldest
    first, each with `cleared` ({by, at, reason}) or None."""
    first = next((e for e in events if e.get("model_reader") == MODEL_READER or e["type"] == "switch"), None)
    if first is None:
        return []
    try:
        start = parse(first["ts"])
        start = start if start.tzinfo else start.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        start = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    flags, by_id, prev, seen_fallback, seen_stop = [], {}, {}, {}, {}

    def new_flag(e, kind, was, now_model, safeguard, fallback=None, stopped=None):
        f = {"id": e["id"], "kind": kind, "name": e["from"], "session": e.get("session"), "from": was, "to": now_model,
             "since": e["ts"], "safeguard": safeguard, "fallback": fallback, "stopped": stopped, "cleared": None}
        flags.append(f)
        by_id[f["id"]] = f

    for e in events:
        key = (e["from"], e.get("session"))
        if e["type"] == "clear":
            f = by_id.get(e.get("flag"))
            if f and not f["cleared"] and e["from"] in LEADS:
                f["cleared"] = {"by": e["from"], "at": e["ts"], "reason": e.get("reason")}
            continue
        if e["type"] == "switch":
            prev.pop(key, None)
            if e.get("source") not in DELIBERATE and isinstance(e.get("now"), str):
                new_flag(e, "change", e.get("was"), e["now"], False)
            continue
        model, source = e.get("model"), e.get("model_from")
        if (e.get("model_reader") != MODEL_READER or source not in ("declared", "session", "flags")
                or not isinstance(model, str) or not model):
            continue
        fallback = e.get("fallback") if isinstance(e.get("fallback"), dict) and e.get("fallback") else None
        mark = fallback and (fallback.get("timestamp"), fallback.get("originalModel"), fallback.get("fallbackModel"))
        new_fallback = bool(fallback) and mark not in seen_fallback.setdefault(key, set()) \
            and after_start(fallback.get("timestamp"), start)
        if fallback:
            seen_fallback[key].add(mark)
        stop = e.get("stopped") if isinstance(e.get("stopped"), str) else None
        new_stop = bool(stop) and stop not in seen_stop.setdefault(key, set()) and after_start(stop, start)
        if stop:
            seen_stop[key].add(stop)
        refusal = bool(fallback) and fallback.get("trigger") == "refusal" and (new_fallback or fallback.get("fallbackModel") == model)
        safeguard = bool(e.get("requested")) or refusal
        last = prev.get(key)
        if last and last[1] == source and last[0] != model:
            new_flag(e, "change", last[0], model, safeguard, fallback if refusal or new_fallback else None,
                     stop if new_stop else None)
        elif new_fallback:
            same = next((f for f in reversed(flags) if (f["name"], f["session"]) == key and not f["cleared"]
                         and f["kind"] == "change" and f["to"] == fallback.get("fallbackModel")), None)
            if same:
                same.update({"fallback": fallback, "safeguard": same["safeguard"] or refusal})
                if new_stop:
                    same["stopped"] = stop
            else:
                new_flag(e, "change", fallback.get("originalModel"), fallback.get("fallbackModel"),
                         safeguard or fallback.get("trigger") == "refusal", fallback, stop if new_stop else None)
        elif new_stop:
            new_flag(e, "stopped", model, model, True, None, stop)
        prev[key] = (model, source)
    return flags


def open_flags(events: list[dict]) -> list[dict]:
    return [f for f in model_flags(events) if not f["cleared"]]


def stamp(ts) -> str:
    """when() for a time from a session record, which may not parse."""
    try:
        return when(ts)
    except (TypeError, ValueError):
        return str(ts)[:19]


def flag_words(f: dict) -> str:
    """A flag as people read it: `model-a to model-b since 18:46, after a safeguard stop`."""
    if f["kind"] == "stopped":
        return f"stopped at {stamp(f['stopped'] or f['since'])}, model unchanged"
    return f"{f['from'] or '?'} to {f['to']} since {when(f['since'])}" + (", after a safeguard stop" if f["safeguard"] else "")


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
    flagged = {f["name"]: f for f in open_flags(events)}  # each name's newest open model flag
    for row in rows.values():
        row["flag"] = flagged.get(row["name"])
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
    if args.check:
        return checked(args.text, "text")
    event = {**base(args, "msg"), "to": to, "text": text_of(args.text, "text")}
    if args.re:
        event["re"] = args.re

    def guard(events, ev):
        if ev.get("re") and not any(e["type"] == "msg" and e["id"] == ev["re"] for e in events):
            raise Refused(f"there is no message #{ev['re']} to reply to")

    event, _ = append(event, guard)
    print(f"posted #{event['id']} to {to}")


def shown_before(events: list[dict], name: str, key: str) -> set:
    """The ids a seat's earlier inboxes recorded as shown under `key` (`acks`, `closes` or `flags`)."""
    return {i for e in events if e["type"] == "read" and e["from"] == name and isinstance(e.get(key), list)
            for i in e[key] if isinstance(i, int)}


def inbox_extras(events: list[dict], name: str) -> list[tuple[str, str, list]]:
    """What an inbox shows after the messages, each once: acknowledgements of this seat's messages, threads
    it started that another seat closed, and open model flags (a lead's: every seat's; any other seat's: its
    own). As (key, plural, [(id, line)]), oldest first."""
    acks = [(e["id"], f"acknowledged: #{e.get('msg')} {e.get('word')} by {e['from']}") for e in events
            if e["type"] == "ack" and e.get("to") == name and e["from"] != name]
    closes = [(e["id"], f"closed: thread #{e.get('root')} by {e['from']} {when(e['ts'])}{noted(e)}") for e in events
              if e["type"] == "close" and e.get("to") == name and e["from"] != name]
    flagged = [(f["id"], f"flag {f['id']}: {f['name']}'s model {flag_words(f)}" if f["kind"] == "change"
                else f"flag {f['id']}: a safeguard stopped {f['name']}'s response at {stamp(f['stopped'] or f['since'])}, "
                     "model unchanged") for f in open_flags(events)
               if name in LEADS or f["name"] == name]
    rows = []
    for key, plural, items in (("acks", "acknowledgements", acks), ("closes", "closed threads", closes),
                               ("flags", "model flags", flagged)):
        done = shown_before(events, name, key)
        rows.append((key, plural, [item for item in items if item[0] not in done]))
    return rows


def cmd_inbox(args):
    name = me(args)
    events = load()[0]
    said = pinned()
    if args.all:
        if said:
            print(f"Pinned: {said}")
        recent = [e for e in events if e["type"] == "msg" and e.get("to") in (name, "all") and e["from"] != name]
        print("\n".join(line(e) for e in recent[-INBOX_MAX:]) if recent else f"no messages for {name}")
        return
    due = said is not None and pinned_due(events, name, said, now())
    if due:
        print(f"Pinned: {said}")
    waiting = unread(events, name)
    shown = waiting[:INBOX_MAX]
    if not waiting:
        print("no unread messages")
    for e in shown:
        print(line(e))
    if len(waiting) > len(shown):
        print(f"{len(waiting) - len(shown)} more unread: run inbox again")
    record = {}
    for key, plural, items in inbox_extras(events, name):
        for _, text in items[:INBOX_EXTRAS]:
            print(text)
        if len(items) > INBOX_EXTRAS:
            print(f"{len(items) - INBOX_EXTRAS} more {plural}: run inbox again")
        if items:
            record[key] = [i for i, _ in items[:INBOX_EXTRAS]]
    if not args.peek and (shown or record or due):
        event = {**base(args, "read"), "msgs": [e["id"] for e in shown], **record}
        if due:
            event["note"] = digest(said)
        append(event)


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
    for e in events:
        if e["type"] == "ack" and e.get("msg") == msg["id"]:
            print(f"acknowledged: {e.get('word')} by {e['from']} {when(e['ts'])}")
    root = root_of({e["id"]: e for e in events if e["type"] == "msg"}, msg)["id"]
    thread = closures(events).get(root)
    if thread and thread["closed"]:
        c = thread["closed"]
        print(f"thread #{root} closed by {c['by']} {when(c['at'])}{noted(c)}")
    elif thread:
        print(f"thread #{root} reopened by #{thread['reopened_by']}")
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
    if args.check:
        return checked(args.text, "status")
    event, _ = append({**base(args, "status"), "text": text_of(args.text, "status")})
    print(f"status set for {event['from']}")


def cmd_done(args):
    if args.check:
        return checked(args.text, "status")
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
    if args.check:
        return checked([args.note], "note") if args.note else no_note("claim")
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


def cmd_ack(args):
    word = args.word.strip().lower()
    if word not in ACK_WORDS:
        raise Refused(f"{args.word!r} is not an acknowledgement: use {', '.join(ACK_WORDS[:-1])} or {ACK_WORDS[-1]}")
    event = {**base(args, "ack"), "msg": args.id, "word": word}

    def guard(events, ev):
        msg = next((e for e in events if e["type"] == "msg" and e["id"] == ev["msg"]), None)
        if not msg:
            raise Refused(f"there is no message #{ev['msg']}")
        if msg["from"] == ev["from"]:
            raise Refused(f"#{msg['id']} is your own message")
        if msg.get("to") not in (ev["from"], "all"):
            raise Refused(f"#{msg['id']} is to {msg.get('to')}, not to you or to all")
        done = next((e for e in events if e["type"] == "ack" and e.get("msg") == msg["id"] and e["from"] == ev["from"]), None)
        if done:
            raise Refused(f"you acknowledged #{msg['id']} as {done.get('word')} at {when(done['ts'])}")
        ev["to"] = msg["from"]

    event, _ = append(event, guard)
    print(f"acknowledged #{event['msg']} as {word}; {event['to']} sees it at its next inbox")


def cmd_close(args):
    if args.check:
        return checked([args.note], "note") if args.note else no_note("close")
    event = {**base(args, "close"), "msg": args.id}
    if args.note:
        event["note"] = text_of([args.note], "note")

    def guard(events, ev):
        msgs = [e for e in events if e["type"] == "msg"]
        by_id = {e["id"]: e for e in msgs}
        msg = by_id.get(ev["msg"])
        if not msg:
            raise Refused(f"there is no message #{ev['msg']}")
        root = root_of(by_id, msg)
        people = {who for e in msgs if root_of(by_id, e) is root for who in (e["from"], e.get("to")) if who}
        if ev["from"] not in people and "all" not in people:
            raise Refused(f"you are not in thread #{root['id']}: only its people close it")
        thread = closures(events).get(root["id"])
        if thread and thread["closed"]:
            c = thread["closed"]
            raise Refused(f"thread #{root['id']} is already closed, by {c['by']} at {when(c['at'])}, with no reply since")
        ev.update({"root": root["id"], "to": root["from"]})

    event, _ = append(event, guard)
    print(f"closed thread #{event['root']}; a reply reopens it")


def cmd_clear(args):
    name = me(args)
    if name not in LEADS:
        raise Refused(f"only {' or '.join(LEADS)} clears a model flag")
    reason = text_of([args.reason], "reason")
    event = {**base(args, "clear"), "flag": args.flag, "reason": reason}

    def guard(events, ev):
        f = next((f for f in model_flags(events) if f["id"] == ev["flag"]), None)
        if f is None:
            raise Refused(f"there is no model flag {ev['flag']}")
        if f["cleared"]:
            raise Refused(f"flag {f['id']} was cleared by {f['cleared']['by']} at {when(f['cleared']['at'])}")

    event, _ = append(event, guard)
    print(f"cleared flag {event['flag']}")


def cmd_switch(args):
    """Claude Code's PostModelSwitch settings hook: its input on stdin (session_id, from_model, to_model, source),
    stored for the board name that last used that session. It never fails the hook: a missing or unusable input, or a
    session no board name has used, writes nothing and exits 0 with a line saying why."""
    if not args.hook:
        raise Refused("switch reads Claude Code's PostModelSwitch hook input on stdin: run it as switch --hook")
    try:
        data = json.loads(sys.stdin.read(65536) or "null")
    except (ValueError, UnicodeError):
        data = None
    sid = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(sid, str) or not SAFE_ID.fullmatch(sid):
        print("agentboard switch: no usable session_id in the hook input; nothing written")
        return 0
    text = lambda k, n: str(data[k])[:n] if isinstance(data.get(k), str) and data[k].strip() else None  # noqa: E731
    event = {"type": "switch", "session": sid, "was": text("from_model", 80), "now": text("to_model", 80),
             "source": text("source", 40) or "unknown"}
    if not any(e.get("session") == sid for e in load()[0]):
        print(f"agentboard switch: no board name has used session {tag(sid)}; nothing written")
        return 0

    def guard(events, ev):
        last = next((e for e in reversed(events) if e.get("session") == sid), None)
        if last is None:
            raise LookupError
        ev.update({"from": last["from"], "harness": last.get("harness", "claude-code")})

    try:
        event, _ = append(event, guard)
    except LookupError:
        print(f"agentboard switch: no board name has used session {tag(sid)}; nothing written")
        return 0
    print(f"switch recorded for {event['from']}: {event['was']} to {event['now']} ({event['source']})")
    return 0


def cmd_who(args):
    rows = agents(load()[0])
    if not rows:
        print("no agents on the board yet")
    for r in rows:
        quiet = ", QUIET" if r["quiet"] else ""
        claims = f", claims {len(r['claims'])}" if r["claims"] else ""
        twice = (f", {r['sessions']} sessions in the last hour ({', '.join(tag(s) for s in r['recent'])}): check only "
                 f"one acts as {r['name']}") if r["sessions"] > 1 else ""
        flag = f", MODEL {flag_words(r['flag'])} (flag {r['flag']['id']})," if r["flag"] else ""
        print(f"{r['name']} ({ident(r)}){flag} seen {ago(r['last'])}{quiet}{claims}{twice}: "
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


NOTICE_START = re.compile(r"(EVERYTHING STOPS|QUIET WINDOW|HOLD|RESUME|RELEASE)(?![A-Za-z])")
NOTICE_OLD_END = re.compile(r"hold released(?![A-Za-z])", re.IGNORECASE)
NOTICE_KINDS = {"EVERYTHING STOPS": "stop", "QUIET WINDOW": "quiet", "HOLD": "hold", "RESUME": "resume", "RELEASE": "resume"}
NOTICES_SHOWN, NOTICE_TEXT = 6, 400
ACTIVITY_HOURS, TIMELINE_SLOTS, SLOT_MINUTES = 12, 36, 10
THREADS_SHOWN, THREAD_TAIL = 25, 5


def notice_kind(text) -> str | None:
    """How the page reads a message to all, from its first words: "hold", "quiet" or "stop" while one is in force, and
    "resume" for an end (RESUME, RELEASE or "Hold released"), which ends whatever is in force. It is a convention,
    not a permission: a message to all from any name counts."""
    text = text if isinstance(text, str) else ""
    if NOTICE_OLD_END.match(text):
        return "resume"
    found = NOTICE_START.match(text)
    return NOTICE_KINDS[found.group(1)] if found else None


def notices(msgs: list[dict]) -> list[dict]:
    """Every notice to all, oldest first."""
    rows = []
    for e in msgs:
        kind = notice_kind(e.get("text")) if e.get("to") == "all" else None
        if kind:
            rows.append({"kind": kind, "id": e["id"], "ts": e["ts"], "from": e["from"], "text": e["text"][:NOTICE_TEXT]})
    return rows


def threads(msgs: list[dict]) -> list[dict]:
    """Messages by reply chain, the thread with the newest message first (THREADS_SHOWN of them). Each keeps its first
    message and its last THREAD_TAIL; `count` is the whole thread's length and `people` everyone in it, in order."""
    by_id = {e["id"]: e for e in msgs}
    groups = {}
    for order, e in enumerate(msgs):
        root = root_of(by_id, e)
        t = groups.setdefault(root["id"], {"root": root["id"], "from": root["from"], "to": root.get("to"), "ts": root["ts"],
                                           "count": 0, "people": [], "msgs": []})
        t["count"], t["last"], t["order"] = t["count"] + 1, e["ts"], order
        for who in (e["from"], e.get("to")):
            if who and who not in t["people"]:
                t["people"].append(who)
        t["msgs"].append({k: e[k] for k in ("id", "ts", "from", "to", "re", "text") if k in e})
    newest = sorted(groups.values(), key=lambda t: t["order"], reverse=True)[:THREADS_SHOWN]
    for t in newest:
        del t["order"]
        if len(t["msgs"]) > THREAD_TAIL + 1:
            t["msgs"] = t["msgs"][:1] + t["msgs"][-THREAD_TAIL:]
    return newest


def activity(events: list[dict], at: dt.datetime) -> dict:
    """The page's counts over time: each seat's events per hour over the last ACTIVITY_HOURS, the board's events per
    SLOT_MINUTES by type over the last TIMELINE_SLOTS, and the messages in the last hour, a message exactly an hour old
    included. An event stamped after `at` counts as at `at`; one whose time does not parse is left out of these, though
    `events` counts every event given."""
    per_seat, per_type, last_hour = {}, {}, 0
    for e in events:
        try:
            age = max(0.0, (at - parse(e["ts"])).total_seconds())
        except (TypeError, ValueError):
            continue
        if age < ACTIVITY_HOURS * 3600:
            per_seat.setdefault(e["from"], [0] * ACTIVITY_HOURS)[ACTIVITY_HOURS - 1 - int(age // 3600)] += 1
        if age < TIMELINE_SLOTS * SLOT_MINUTES * 60:
            per_type.setdefault(e["type"], [0] * TIMELINE_SLOTS)[TIMELINE_SLOTS - 1 - int(age // (SLOT_MINUTES * 60))] += 1
        if e["type"] == "msg" and age <= RECENT.total_seconds():
            last_hour += 1
    return {"activity": per_seat, "activity_hours": ACTIVITY_HOURS, "timeline": per_type, "timeline_slots": TIMELINE_SLOTS,
            "timeline_slot_minutes": SLOT_MINUTES, "counts": {"messages_last_hour": last_hour, "events": len(events)}}


def public_flag(f: dict | None) -> dict | None:
    """A model flag as the page gets it: the session as people read it, and no clearing (the page shows open ones)."""
    if not f:
        return None
    return {**{k: v for k, v in f.items() if k != "cleared"}, "session": tag(f["session"])}


def state(limit: int = 60) -> dict:
    """What the page shows. The keys up to `skipped_lines` are those of earlier versions and keep their meaning;
    the rest feed the page's banner, notices, threads and counts, then the acknowledgements and closed threads on the
    messages and threads, each seat's open model flag, the open flags newest first, and the pinned line."""
    events, skipped = load()
    at = now()
    every = [e for e in events if e["type"] == "msg"]
    msgs = every[-limit:]
    pick = lambda e, keys: {k: e[k] for k in keys if k in e}  # noqa: E731
    told = notices(every)
    acks, closed = {}, closures(events)
    for e in events:
        if e["type"] == "ack":
            acks.setdefault(e.get("msg"), []).append({"by": e["from"], "word": e.get("word"), "at": e["ts"]})
    threads_now = threads(every)
    for t in threads_now:
        t.update({"closed": None, "reopened_by": None, **{k: v for k, v in closed.get(t["root"], {}).items()}})
    return {"generated": at.isoformat(timespec="seconds"),
            "folding": {"message_limit": PAGE_MESSAGE_LIMIT, "age_seconds": int(FOLD_AFTER.total_seconds())},
            "messages": [{**pick(e, ("id", "ts", "from", "to", "re", "text", "harness")), "session": tag(e.get("session")),
                          "acks": acks.get(e["id"], []), "thread_closed": closed.get(e["id"], {}).get("closed")}
                         for e in reversed(msgs)],
            "agents": [{**pick(r, ("name", "harness", "last", "status", "status_ts", "quiet", "claims", "sessions")),
                        "session": tag(r["session"]), "recent": [tag(s) for s in r["recent"]], "runs": runs(r),
                        "folded": older_than_day(r["last"], at), "flag": public_flag(r["flag"])}
                       for r in agents(events)],
            "claims": [{**pick(c, ("id", "ts", "from", "path", "note")),
                        "folded": older_than_day(c["ts"], at),
                        "age_seconds": max(0, int((at - parse(c["ts"])).total_seconds()))}
                       for c in active_claims(events)],
            "skipped_lines": skipped,
            "now_epoch": at.timestamp(),
            "mode": told[-1] if told and told[-1]["kind"] != "resume" else None,
            "notices": told[-NOTICES_SHOWN:][::-1],
            "threads": threads_now,
            **activity(events, at),
            "flags": [public_flag(f) for f in reversed(open_flags(events))],
            "pinned": pinned()}


PAGE = """<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent board</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header class="top"><div class="brand"><span class="mark" aria-hidden="true"></span><h1>Agent board</h1></div><p id="updated" aria-live="polite">loading</p></header>
<p id="pinned" class="pinned" hidden></p>
<div id="mode" class="mode" role="status" hidden></div>
<div id="summary" class="summary" aria-label="Counts"></div>
<main>
<section class="panel" id="agents-panel" aria-labelledby="h-agents"><div class="ph"><h2 id="h-agents">Seats</h2><span class="hint" id="agents-hint"></span></div>
<div id="agents"></div>
<details id="older-agents" class="lane-quiet" hidden><summary id="older-agents-summary">Quiet for over a day (0)</summary><ul id="older-agents-list"></ul></details></section>
<section class="panel" id="messages-panel" aria-labelledby="h-messages"><div class="ph"><h2 id="h-messages">Conversation</h2>
<div class="tools"><div class="views" role="group" aria-label="View"><button type="button" class="chip on" id="view-latest" aria-pressed="true">Latest</button><button type="button" class="chip" id="view-threads" aria-pressed="false">Threads</button></div>
<input id="q" type="search" placeholder="Search messages or #number" aria-label="Search messages"></div></div>
<div id="chips" class="chips" role="group" aria-label="Show one seat"></div>
<div id="latest"><ol id="messages"></ol>
<details id="older-messages" hidden><summary id="older-messages-summary">Older messages (0)</summary><ol id="older-messages-list"></ol></details></div>
<ol id="threads" hidden></ol></section>
<div class="aside">
<section class="panel" id="activity-panel" aria-labelledby="h-activity"><div class="ph"><h2 id="h-activity">Activity</h2><span class="hint">last 6 hours, per 10 minutes</span></div>
<div id="timeline"></div><div id="legend" class="legend"></div></section>
<section class="panel" id="claims-panel" aria-labelledby="h-claims"><div class="ph"><h2 id="h-claims">Claims</h2><span class="hint" id="claims-hint"></span></div><ul id="claims"></ul>
<details id="older-claims" hidden><summary id="older-claims-summary">Claims older than a day (0)</summary><ul id="older-claims-list"></ul></details></section>
<section class="panel" id="notices-panel" aria-labelledby="h-notices"><div class="ph"><h2 id="h-notices">Notices to all seats</h2></div><ol id="notices" class="notices"></ol></section>
</div>
</main>
<footer>Read-only: this page never writes to the board.</footer>
<script src="/app.js"></script>
</body>
</html>
"""

SCRIPT = r"""const $ = id => document.getElementById(id);
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
const SVG = 'http://www.w3.org/2000/svg';
function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}
const fmt = ts => {
  const d = new Date(ts);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'})
    : d.toLocaleString('en-GB', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
};
const clock = epoch => new Date(epoch * 1000).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
const homePrefix = __HOME_PREFIX__;
const place = path => path.startsWith(homePrefix) ? '~/' + path.slice(homePrefix.length) : path;
const age = seconds => seconds < 3600 ? Math.floor(seconds / 60) + ' min'
  : seconds < 86400 ? Math.floor(seconds / 3600) + ' h'
  : Math.floor(seconds / 86400) + ' d ' + Math.floor(seconds % 86400 / 3600) + ' h';
const since = seconds => seconds < 90 ? Math.round(seconds) + ' s' : age(seconds);
const TYPES = ['msg', 'read', 'status', 'claim', 'release'];
const LABEL = {msg: 'messages', read: 'reads', status: 'status lines', claim: 'claims', release: 'releases',
  ack: 'acknowledgements', close: 'thread closes', clear: 'flag clears', switch: 'model switches'};
const MODE = {hold: 'HOLD', quiet: 'QUIET WINDOW', stop: 'EVERYTHING STOPS', resume: 'RESUME'};
const theme = new URLSearchParams(location.search).get('theme');
if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
let S = null, FILTER = 'all', QUERY = '', VIEW = 'latest';
try { if (localStorage.getItem('agentboard-view') === 'threads') VIEW = 'threads'; } catch (e) { /* storage blocked: keep the default view */ }

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
const quietFor = a => Math.max(0, S.now_epoch - Date.parse(a.last) / 1000);
const hit = (m, q) => (m.text || '').toLowerCase().includes(q) || ('#' + m.id).includes(q);
function view() {
  const q = QUERY.trim().toLowerCase();
  const seat = m => FILTER === 'all' || m.from === FILTER || m.to === FILTER;
  return {...S, messages: S.messages.filter(m => seat(m) && (!q || hit(m, q))),
    claims: [...S.claims].sort((a, b) => a.age_seconds - b.age_seconds)};
}
function spark(values) {
  const w = 120, h = 28, top = Math.max(1, ...values), step = w / Math.max(1, values.length - 1);
  const pts = values.map((v, i) => (i * step).toFixed(1) + ',' + (h - 2 - v / top * (h - 4)).toFixed(1));
  const s = svg('svg', {width: w, height: h, viewBox: '0 0 ' + w + ' ' + h, class: 'spark', role: 'img',
    'aria-label': 'events per hour over the last ' + values.length + ' hours: ' + values.join(', ')});
  s.append(svg('path', {d: 'M0,' + h + ' L' + pts.join(' L') + ' L' + w + ',' + h + ' Z', class: 'spark-area'}),
    svg('polyline', {points: pts.join(' '), class: 'spark-line'}));
  return s;
}
function seatCard(a) {
  const on = FILTER === a.name;
  const b = el('button', 'seat' + (on ? ' on' : ''));
  b.type = 'button';
  b.dataset.key = 'seat:' + a.name;
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  b.title = on ? 'Show every seat' : 'Show only ' + a.name;
  const head = el('div', 'head');
  head.append(el('span', 'name', a.name), el('span', 'side', since(quietFor(a)) + ' ago'));
  b.append(head, el('div', 'meta', [a.harness, a.session, a.runs].filter(Boolean).join(' · ')));
  if (a.sessions > 1) b.append(el('div', 'alert', a.sessions + ' sessions used this name in the last hour: ' + a.recent.join(', ')));
  if (a.flag) b.append(el('div', 'alert flag', flagText(a.flag)));
  b.append(el('div', 'text', a.status || 'no status'));
  const n = Array.isArray(a.claims) ? a.claims.length : 0;
  const foot = el('div', 'foot');
  foot.append(el('span', n ? 'pill' : 'pill none', n ? n + (n === 1 ? ' claim' : ' claims') : 'no claims'),
    spark(S.activity[a.name] || Array(S.activity_hours).fill(0)));
  b.append(foot);
  b.onclick = () => { FILTER = on ? 'all' : a.name; render(); };
  const li = el('li');
  li.append(b);
  return li;
}
function lanes(s) {
  const recent = s.agents.filter(a => !a.folded);
  const box = $('agents');
  box.replaceChildren();
  for (const [title, list, cls] of [
    ['Active in the last 15 minutes', recent.filter(a => !a.quiet && quietFor(a) < 900), 'lane'],
    ['Seen in the last 2 hours', recent.filter(a => !a.quiet && quietFor(a) >= 900), 'lane'],
    ['Quiet for over 2 hours', recent.filter(a => a.quiet), 'lane lane-quiet']]) {
    if (!list.length) continue;
    const lane = el('div', cls);
    const ul = el('ul');
    ul.append(...list.map(seatCard));
    lane.append(el('h3', '', title + ' · ' + list.length), ul);
    box.append(lane);
  }
  if (!s.agents.length) box.append(el('p', 'empty', 'No seats yet'));
  fold('older-agents', s.agents.filter(a => a.folded).map(seatCard), 'Quiet for over a day');
  $('agents-hint').textContent = s.agents.length + (s.agents.length === 1 ? ' seat' : ' seats');
}
function chips(s) {
  const names = s.agents.filter(a => !a.folded).map(a => a.name);
  if (FILTER !== 'all' && !names.includes(FILTER)) names.push(FILTER);
  $('chips').replaceChildren(...['all', ...names].map(name => {
    const c = el('button', 'chip' + (FILTER === name ? ' on' : ''), name === 'all' ? 'All seats' : name);
    c.type = 'button';
    c.dataset.key = 'chip:' + name;
    c.setAttribute('aria-pressed', FILTER === name ? 'true' : 'false');
    c.onclick = () => { FILTER = name; render(); };
    return c;
  }));
}
function messageBody(m) {
  const d = el('div', 'msg');
  const head = el('div', 'head');
  const who = el('span', 'who');
  who.append(el('span', 'name', m.from), document.createTextNode(' → ' + m.to));
  head.append(who, el('span', 'side', fmt(m.ts) + ' · #' + m.id));
  d.append(head);
  const acked = (Array.isArray(m.acks) ? m.acks : []).map(a => a.word + ' by ' + a.by);
  const meta = [m.session ? 'session ' + m.session : '', m.re ? 'reply to #' + m.re : '', ...acked,
    m.thread_closed ? 'thread closed' : ''].filter(Boolean).join(' · ');
  if (meta) d.append(el('div', 'meta', meta));
  d.append(el('div', 'text', m.text));
  return d;
}
const messageCard = m => { const li = el('li', 'card'); li.append(messageBody(m)); return li; };
function latest(s) {
  const none = S.messages.length ? 'No messages match' : 'No messages yet';
  fill('messages', s.messages.slice(0, s.folding.message_limit).map(messageCard), none);
  fold('older-messages', s.messages.slice(s.folding.message_limit).map(messageCard), 'Older messages');
}
function threadList() {
  const q = QUERY.trim().toLowerCase();
  const list = S.threads.filter(t => (FILTER === 'all' || t.people.includes(FILTER)) && (!q || t.msgs.some(m => hit(m, q))));
  fill('threads', list.map(t => {
    const li = el('li', 'card thread');
    t.msgs.forEach((m, i) => {
      li.append(messageBody(m));
      const between = t.count - t.msgs.length;
      if (i === 0 && between > 0) li.append(el('div', 'more', '… ' + between + (between === 1 ? ' reply' : ' replies') + ' between, not shown'));
    });
    if (t.closed) {
      const pill = el('div', 'closed-pill', 'closed by ' + t.closed.by + ' ' + fmt(t.closed.at) + (t.closed.note ? ': ' + t.closed.note : ''));
      li.append(pill);
    } else if (t.reopened_by) li.append(el('div', 'more', 'reopened by #' + t.reopened_by));
    return li;
  }), S.threads.length ? 'No threads match' : 'No messages yet');
}
function timeline() {
  const n = S.timeline_slots, minutes = S.timeline_slot_minutes, w = 360, h = 96, bw = w / n, gap = 1.5;
  const types = [...TYPES, ...Object.keys(S.timeline).filter(k => !TYPES.includes(k)).sort()];
  const totals = Array(n).fill(0);
  for (const k of types) (S.timeline[k] || []).forEach((v, i) => { totals[i] += v; });
  const top = Math.max(1, ...totals), all = totals.reduce((a, b) => a + b, 0);
  const s = svg('svg', {viewBox: '0 0 ' + w + ' ' + (h + 16), width: '100%', role: 'img',
    'aria-label': 'Board events per ' + minutes + ' minutes over the last ' + (n * minutes / 60) + ' hours: ' + all + ' in all'});
  s.append(svg('line', {x1: 0, x2: w, y1: h + 0.5, y2: h + 0.5, class: 'axis'}));
  const start = i => S.now_epoch - (n - i) * minutes * 60;
  const base = Array(n).fill(0);
  for (const k of types) {
    (S.timeline[k] || []).forEach((v, i) => {
      if (!v) return;
      const bh = v / top * h, lift = base[i] ? gap : 0;
      const r = svg('rect', {x: i * bw + 1, y: h - base[i] - bh, width: Math.max(1, bw - 2), height: Math.max(1, bh - lift), rx: 1.5,
        class: 'c-' + (TYPES.includes(k) ? k : 'other')});
      const tip = svg('title', {});
      tip.textContent = clock(start(i)) + ' to ' + clock(start(i + 1)) + ': ' + v + ' ' + (LABEL[k] || k);
      r.append(tip);
      s.append(r);
      base[i] += bh;
    });
  }
  for (const [x, label, anchor] of [[0, (n * minutes / 60) + ' h ago', 'start'], [w / 2, (n * minutes / 120) + ' h', 'middle'], [w, 'now', 'end']]) {
    const t = svg('text', {x: x, y: h + 13, class: 'tick', 'text-anchor': anchor});
    t.textContent = label;
    s.append(t);
  }
  $('timeline').replaceChildren(s);
  const keys = types.filter(k => (S.timeline[k] || []).some(v => v));
  $('legend').replaceChildren(...(keys.length ? keys.map(k => {
    const item = el('span');
    item.append(el('i', 'sw c-' + (TYPES.includes(k) ? k : 'other')),
      document.createTextNode((LABEL[k] || k) + ' ' + S.timeline[k].reduce((a, b) => a + b, 0)));
    return item;
  }) : [el('span', 'empty', 'No board events in this time')]));
}
function claims(s) {
  const claimCard = c => {
    const li = el('li', 'card');
    const head = el('div', 'head');
    head.append(el('span', 'name', c.from), el('span', 'side', age(c.age_seconds) + ' old, since ' + fmt(c.ts) + ' · #' + c.id));
    const bar = el('div', 'bar');
    const part = el('i');
    part.style.width = Math.max(3, Math.min(1, c.age_seconds / 86400) * 100) + '%';
    bar.append(part);
    li.append(head, el('div', 'path', place(c.path)), bar);
    if (c.note) li.append(el('div', 'meta', c.note));
    return li;
  };
  fill('claims', s.claims.filter(c => !c.folded).map(claimCard), s.claims.length ? '' : 'No claims');
  fold('older-claims', s.claims.filter(c => c.folded).map(claimCard), 'Claims older than a day');
  $('claims-hint').textContent = s.claims.length + ' open · the bar fills over a day';
}
function notices() {
  fill('notices', S.notices.map(n => {
    const li = el('li', n.kind);
    li.append(el('b', '', MODE[n.kind] + ' '), document.createTextNode(fmt(n.ts) + ' · #' + n.id + ' · ' + n.text.slice(0, 200)));
    return li;
  }), 'No notices to all seats');
}
function banner() {
  const m = $('mode');
  m.hidden = !S.mode;
  if (!S.mode) return;
  m.className = 'mode ' + S.mode.kind;
  m.replaceChildren(el('b', '', MODE[S.mode.kind]), el('span', 'when', 'since ' + fmt(S.mode.ts) + ' · #' + S.mode.id + ' from ' + S.mode.from),
    el('span', 'what', S.mode.text));
}
function flagText(f) {
  if (f.kind === 'stopped') return 'Stopped by a safeguard at ' + fmt(f.stopped || f.since) + ', model unchanged · flag ' + f.id;
  return 'Model changed: ' + (f.from || '?') + ' to ' + f.to + ' since ' + fmt(f.since) + (f.safeguard ? ', after a safeguard stop' : '') + ' · flag ' + f.id;
}
function pinnedLine() {
  const said = $('pinned');
  said.hidden = !S.pinned;
  said.textContent = S.pinned ? 'Pinned: ' + S.pinned : '';
}
function summary() {
  const recent = S.agents.filter(a => !a.quiet);
  const quiet = S.agents.filter(a => a.quiet && !a.folded).length;
  const oldest = Math.max(0, ...S.claims.map(c => c.age_seconds));
  $('summary').replaceChildren(tile('seats active now', recent.filter(a => quietFor(a) < 900).length),
    tile('quiet seats', quiet, quiet ? 'warn' : ''), tile('messages in the last hour', S.counts.messages_last_hour),
    tile('open claims', S.claims.length), tile('oldest claim', S.claims.length ? age(oldest) : 'none'),
    ...(S.flags && S.flags.length ? [tile(S.flags.length === 1 ? 'model flag' : 'model flags', S.flags.length, 'warn')] : []),
    ...(S.skipped_lines ? [tile('torn board lines', S.skipped_lines, 'warn')] : []));
}
function render() {
  if (!S) return;
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : '';
  const s = view();
  banner(); summary(); lanes(s); chips(s); latest(s); threadList(); timeline(); claims(s); notices(); pinnedLine();
  $('latest').hidden = VIEW !== 'latest';
  $('threads').hidden = VIEW !== 'threads';
  for (const v of ['latest', 'threads']) {
    $('view-' + v).classList.toggle('on', VIEW === v);
    $('view-' + v).setAttribute('aria-pressed', VIEW === v ? 'true' : 'false');
  }
  if (focused) {
    const again = [...document.querySelectorAll('[data-key]')].find(node => node.dataset.key === focused);
    if (again) again.focus();
  }
}
function setView(v) {
  VIEW = v;
  try { localStorage.setItem('agentboard-view', v); } catch (e) { /* storage blocked: the choice lasts until the page reloads */ }
  render();
}
async function refresh() {
  try {
    S = await (await fetch('/api/state', {cache: 'no-store'})).json();
    render();
    $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString('en-GB') + ' · refreshes every 3 seconds';
    document.body.classList.remove('stale');
  } catch (e) {
    $('updated').textContent = 'Board not reachable';
    document.body.classList.add('stale');
  }
}
$('q').addEventListener('input', e => { QUERY = e.target.value; render(); });
$('view-latest').addEventListener('click', () => setView('latest'));
$('view-threads').addEventListener('click', () => setView('threads'));
refresh();
setInterval(refresh, 3000);
""".replace("__HOME_PREFIX__", json.dumps(HOME_ROOT + os.sep))

# The page's colours, light first; the dark set follows the system setting unless the address asks for ?theme=light,
# and ?theme=dark asks for it outright (for a dark page around the frame, such as a dashboard). The chart's five series
# use a categorical palette checked for colour-vision separation in both sets. Against the dark panel each series
# reaches 3:1; against the light panel, status lines, claims and releases do not, so the legend's counts and each bar's
# hover title carry them.
LIGHT_COLOURS = ("--bg: #f4f3ee; --panel: #ffffff; --panel2: #f8f7f3; --fg: #1b1b19; --muted: #55554f; --line: #d9d8d0; "
                 "--field: #85847c; --accent: #1f5f4f; --accent-bg: #e3efea; --warn: #9a4a1f; --warn-bg: #fbece2; "
                 "--info: #1f4f8a; --info-bg: #e3ecf8; --stop: #a3262b; --stop-bg: #fbe4e4; --c-msg: #2a78d6; --c-read: #eb6834; "
                 "--c-status: #1baf7a; --c-claim: #eda100; --c-release: #e87ba4; --c-other: #8f8f88;")
DARK_COLOURS = ("--bg: #141413; --panel: #1e1e1c; --panel2: #252522; --fg: #f0efea; --muted: #b6b5ae; --line: #3a3a35; "
                "--field: #7c7b74; --accent: #8bd3bb; --accent-bg: #1d3029; --warn: #f2ab7f; --warn-bg: #3b261a; "
                "--info: #9cc1f3; --info-bg: #1b2a3d; --stop: #ff9a9e; --stop-bg: #3d1c1e; --c-msg: #3987e5; --c-read: #d95926; "
                "--c-status: #199e70; --c-claim: #c98500; --c-release: #d55181; --c-other: #8a8a82;")

STYLE = (":root { color-scheme: light; " + LIGHT_COLOURS + " }\n"
         "@media (prefers-color-scheme: dark) { :root:not([data-theme=\"light\"]) { color-scheme: dark; " + DARK_COLOURS + " } }\n"
         ":root[data-theme=\"dark\"] { color-scheme: dark; " + DARK_COLOURS + " }\n" + """* { box-sizing: border-box; }
[hidden] { display: none; }
body { margin: 0; padding: 24px clamp(16px, 3vw, 40px); background: var(--bg); color: var(--fg); font: 18px/1.5 system-ui, sans-serif; }
.top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 20px; margin-bottom: 16px; }
.brand { display: flex; align-items: center; gap: 12px; }
.mark { width: 32px; height: 32px; border-radius: 10px; background: linear-gradient(135deg, var(--accent), var(--c-msg)); }
h1 { font-size: 30px; line-height: 1.2; margin: 0; }
#updated { margin: 0; color: var(--muted); font-size: 16px; font-variant-numeric: tabular-nums; }
.stale #updated { color: var(--warn); font-weight: 600; }
.mode:not([hidden]) { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-bottom: 16px; padding: 12px 18px; border: 2px solid; border-radius: 12px; }
.mode b { letter-spacing: .04em; }
.mode .when { font-size: 16px; font-variant-numeric: tabular-nums; }
.mode .what { flex-basis: 100%; overflow-wrap: anywhere; }
.mode.hold { background: var(--warn-bg); color: var(--warn); }
.mode.quiet { background: var(--info-bg); color: var(--info); }
.mode.stop { background: var(--stop-bg); color: var(--stop); }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
.tile { display: flex; flex-direction: column; padding: 12px 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }
.tile .value { font-size: 30px; font-weight: 700; line-height: 1.15; font-variant-numeric: tabular-nums; }
.tile .label { color: var(--muted); font-size: 16px; }
.tile.warn .value { color: var(--warn); }
main { display: grid; gap: 20px; grid-template-columns: minmax(0, 1fr); align-items: start; }
.aside { display: grid; gap: 20px; align-content: start; min-width: 0; }
@media (min-width: 1000px) { main { grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr); } .aside { grid-column: 1 / -1; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); } }
@media (min-width: 1500px) { main { grid-template-columns: minmax(0, 1fr) minmax(0, 1.35fr) minmax(0, .95fr); } .aside { grid-column: auto; grid-template-columns: minmax(0, 1fr); } }
.panel { min-width: 0; padding: 16px 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; }
.ph { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 12px; margin-bottom: 12px; }
h2 { font-size: 22px; margin: 0; }
h3 { margin: 16px 0 8px; color: var(--muted); font-size: 15px; letter-spacing: .05em; text-transform: uppercase; }
.lane:first-child h3 { margin-top: 0; }
.hint { color: var(--muted); font-size: 16px; }
ol, ul { display: grid; gap: 10px; margin: 0; padding: 0; list-style: none; }
details { margin-top: 12px; }
summary { cursor: pointer; color: var(--accent); overflow-wrap: anywhere; }
summary:focus-visible, .seat:focus-visible, .chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
details[open] > summary { margin-bottom: 10px; }
.card { padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; }
.seat { display: block; width: 100%; padding: 10px 14px; color: inherit; font: inherit; text-align: left; cursor: pointer; background: var(--panel2); border: 1px solid var(--line); border-radius: 10px; }
.seat:hover { border-color: var(--accent); }
.seat.on { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.lane-quiet .seat { background: transparent; border-style: dashed; }
.lane-quiet .seat .name { color: var(--muted); }
.head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; }
.name { color: var(--accent); font-weight: 700; overflow-wrap: anywhere; }
.side { color: var(--muted); font-size: 16px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 16px; overflow-wrap: anywhere; }
.text { margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
.seat .text { display: -webkit-box; overflow: hidden; -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
.alert { margin-top: 6px; padding: 2px 8px; color: var(--warn); font-size: 16px; background: var(--warn-bg); border-radius: 6px; }
.alert.flag { color: var(--stop); background: var(--stop-bg); }
.foot { display: flex; align-items: end; justify-content: space-between; gap: 10px; margin-top: 8px; }
.pill { padding: 1px 10px; color: var(--accent); font-size: 15px; white-space: nowrap; background: var(--accent-bg); border-radius: 999px; }
.pill.none { padding-left: 0; color: var(--muted); background: none; }
.spark { display: block; flex: none; }
.spark-area { fill: var(--accent-bg); }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 2; }
.tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.views { display: flex; gap: 6px; }
input[type=search] { width: min(280px, 100%); min-width: 0; padding: 6px 12px; color: var(--fg); font: inherit; font-size: 16px; background: var(--panel2); border: 1px solid var(--field); border-radius: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.chip { padding: 3px 12px; color: var(--fg); font: inherit; font-size: 15px; cursor: pointer; background: var(--panel2); border: 1px solid var(--field); border-radius: 999px; }
.chip.on { color: var(--panel); background: var(--accent); border-color: var(--accent); }
.thread .msg + .msg { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); }
.more { margin-top: 8px; color: var(--muted); font-size: 16px; }
.path { margin-top: 4px; font-family: ui-monospace, monospace; font-size: 16px; overflow-wrap: anywhere; }
.bar { height: 6px; margin-top: 8px; overflow: hidden; background: var(--line); border-radius: 3px; }
.bar > i { display: block; height: 100%; background: var(--c-claim); }
.axis { stroke: var(--line); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 8px; color: var(--muted); font-size: 15px; }
.sw { display: inline-block; width: 12px; height: 12px; margin-right: 6px; vertical-align: -1px; border-radius: 3px; }
.c-msg { fill: var(--c-msg); background: var(--c-msg); }
.c-read { fill: var(--c-read); background: var(--c-read); }
.c-status { fill: var(--c-status); background: var(--c-status); }
.c-claim { fill: var(--c-claim); background: var(--c-claim); }
.c-release { fill: var(--c-release); background: var(--c-release); }
.c-other { fill: var(--c-other); background: var(--c-other); }
.notices li { padding: 2px 0 2px 12px; font-size: 16px; overflow-wrap: anywhere; border-left: 4px solid var(--line); }
.notices li.hold { border-color: var(--warn); }
.notices li.quiet { border-color: var(--info); }
.notices li.stop { border-color: var(--stop); }
.notices li.resume { border-color: var(--accent); }
.empty { color: var(--muted); }
footer { margin-top: 20px; color: var(--muted); font-size: 16px; }
.pinned:not([hidden]) { margin: 0 0 16px; padding: 10px 16px; color: var(--fg); background: var(--accent-bg); border-left: 4px solid var(--accent); border-radius: 8px; overflow-wrap: anywhere; }
.closed-pill { display: inline-block; margin-top: 10px; padding: 1px 10px; color: var(--accent); font-size: 15px; background: var(--accent-bg); border-radius: 999px; overflow-wrap: anywhere; }
""")


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


def number(text: str) -> int:
    """A message or flag id, with or without its #."""
    return int(text.lstrip("#"))


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
    p.add_argument("--check", action="store_true", help="count the text against its limit and write nothing")
    p = command("inbox", cmd_inbox, "your unread messages")
    p.add_argument("--peek", action="store_true", help="leave them unread")
    p.add_argument("--all", action="store_true", help="the newest messages for you, read or not, without marking them; "
                   "for a session taking over a role")
    command("read", cmd_read, "one message in full").add_argument("id", type=number)
    p = command("wait", cmd_wait, "wait without writing or marking read; timeout exits 3")
    p.description = "Wait for unread messages; exit 0 on messages, 3 on timeout with no output. Never writes."
    p.add_argument("--timeout", type=seconds, default=None, help="seconds to wait (default: indefinitely; 0: check once)")
    p.add_argument("--interval", type=interval_seconds, default=1.0, help="seconds between metadata checks (default: 1)")
    command("nudge", cmd_nudge, "print a resume command for NAME's newest unread message; never run it").add_argument("name")
    for name, func, help_text in (("status", cmd_status, "what you are doing now"),
                                  ("done", cmd_done, "release all your claims and set your status")):
        p = command(name, func, help_text)
        p.add_argument("text", nargs="+")
        p.add_argument("--check", action="store_true", help="count the text against its limit and write nothing")
    p = command("claim", cmd_claim, "claim a folder or file before writing in it")
    p.add_argument("path")
    p.add_argument("--note", help="what you are doing there")
    p.add_argument("--check", action="store_true", help="count the note against its limit and write nothing")
    p = command("release", cmd_release, "release a claim by path or id")
    p.add_argument("target")
    p.add_argument("--force", action="store_true", help="release another agent's claim (needs --reason)")
    p.add_argument("--reason", default="")
    command("check", cmd_check, "who holds a path").add_argument("path")
    p = command("ack", cmd_ack, "acknowledge a message with seen, thanks or noted; wakes no one")
    p.add_argument("id", type=number)
    p.add_argument("word", help="seen, thanks or noted")
    p = command("close", cmd_close, "mark a thread closed; a reply after it reopens the thread")
    p.add_argument("id", type=number, help="any message of the thread")
    p.add_argument("--note", help="how it ended")
    p.add_argument("--check", action="store_true", help="count the note against its limit and write nothing")
    p = command("clear", cmd_clear, "clear a model flag (a name in LEADS)")
    p.add_argument("flag", type=number)
    p.add_argument("--reason", required=True, help="why it is cleared, recorded")
    p = command("switch", cmd_switch, "record a model switch from Claude Code's PostModelSwitch hook (stdin)")
    p.add_argument("--hook", action="store_true", help="read the hook's input on stdin")
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
