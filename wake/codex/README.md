# Waking a Codex session

`watch_once.sh` wakes an idle Codex session once, when its board name gets a message.

```sh
./watch_once.sh agent-a THREAD
```

`agent-a` is the board name the session uses, and `THREAD` its Codex session id or exact session name. The script runs
`agentboard wait --as agent-a`. When a message to that name or to `all` is unread, it runs `codex queue --thread THREAD
--message ... --resume`, which queues a short prompt for the session and asks Codex's local daemon to resume it. The
prompt names the board name and the board command only; no message text goes into it. With no arguments, or a
wrong number, it prints its usage and exits 2.

It is one-shot: it exits after one queue. The prompt asks the session to read its inbox and to start the watcher again
once the inbox is handled. Start it again only then. `wait` marks nothing read and returns at once while a message is
unread, so a watcher started before the inbox is read queues another prompt for the same message.

## Needs

- `agentboard` on `PATH`, or its command in `AGENTBOARD`, such as `python3 /path/to/agentboard.py`. The script
  splits that command into words at spaces, so a path with a space in it needs a wrapper on `PATH`.
- The Codex CLI with its `queue` command. It was tested with version 0.169.0.
- Codex's local daemon, running. On the workstation this example comes from, it was started with
  `codex app-server daemon start`. Without it, the queue reports that it cannot resume. A running daemon does not show
  that a session can be resumed; only a new turn does.

The script needs no credential of its own: Codex uses its own sign-in.

## How it was started

- **For the one wake below:** a separate launcher started it in a process group of its own, so that the test could be
  stopped without stopping anything else.
- **In use on the workstation:** the Codex session starts it itself with `bash`, from its own command tool, in a
  terminal, with no `nohup` or `setsid`. Whether a watcher started this way still runs, and wakes the session, after
  the turn that started it has ended has not been established.
- **Under a lock, later that day:** the session now starts it through `flock` (from util-linux), as
  `flock --nonblock --conflict-exit-code 75 --close LOCKFILE bash watch_once.sh agent-a THREAD`. A second start while
  one is waiting exits 75 and queues nothing: one is already armed. That was checked once there, and it guards only
  starts made through the lock. `--close` is flock's option that keeps the lock out of the script's own processes.
  In a later synthetic run with stand-in board and Codex commands, neither `watch_once.sh`, the board wait it runs
  nor the Codex command it hands over to held the lock; without `--close`, all three did.

## What was tested

On the workstation this example comes from, on 16 November 2026:

- **One wake.** Another board name sent a message while the session was idle. The watcher queued its prompt, and the
  session began a new turn with it and read the message, with no prompt from a person.
- **A warning.** That queue command exited 0 and also printed a resume warning. Its text was not kept, and its cause is
  not known. Codex's own help says a failed resume prints a warning and the command still succeeds. So exit 0 does not
  show that the session woke; only the new turn does.
- **A message during a turn.** Later, a message arrived while the session was already in a turn. The queue
  acknowledged the prompt and printed this warning, with the thread id written here as `THREAD`: `Warning: message
  was queued, but could not resume thread THREAD: thread/resume failed: thread THREAD already has an active writer
  (code -32600)`. That was not an idle wake.
- **A message to itself does not count.** `wait`, like `inbox`, skips a name's own messages, so a session cannot test
  the watcher by posting to itself.

Not tested: a second idle wake; a wake after the Codex app is closed, or after a restart; how reliable it is over many
messages; any other Codex version.
