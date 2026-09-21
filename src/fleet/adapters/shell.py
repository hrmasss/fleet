"""Shared machinery every adapter needs, so each one is only its differences.

Three things are the same for all of them and belong here rather than three times over:

    tmux        every runner is launched detached in tmux, so a session outlives the
                dispatcher and the operator can attach to any of them the same way
    liveness    ccmux installs hooks into agy, claude and cursor, and all three write the
                same marker shape into one directory
    nudge       a continuation goes in as keystrokes, because none of the three has a
                documented way to push a message into a running session

⚠ **No single liveness signal works for all three runners**, which a live run proved and no
fixture would have. agy's ccmux hooks are configured and never fire, so it writes no marker
at all. claude's hooks only ever write `idle`, so a working claude session marks itself idle
for its entire run. And claude writes nothing to its log until the very end, because piping
stdout puts it in print mode.

So `liveness` combines three signals and lets working win, with CPU time from /proc as the
one that holds everywhere, because it asks the kernel instead of the agent.

⚠ **Neither the log nor the kernel can tell work from a terminal that is merely still
painted.** An agent CLI left at its prompt emits control sequences forever: the log grows,
the CPU counter moves, and nothing is happening. So the log signal counts visible
characters rather than bytes (`visible_len`), and a runner that shows a terminal interface
declares `tui = True`, which puts a floor under the CPU reading. Both came from five agy
sessions that finished their work and then held their slots overnight.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from fleet.adapters.base import Handle, State

MARKERS = Path(os.environ.get("CCMUX_HOME", str(Path.home() / ".config/ccmux"))) / "session-pids"
MARKER_STALE = 300.0
"""A marker older than this says nothing useful; fall back to pid and log."""

_ESC = "\x1b"
ANSI = re.compile(
    _ESC + r"\[[0-9;:?<>=]*[ -/]*[@-~]"  # CSI, including private forms like ESC[>4;2m
    "|" + _ESC + r"\][^\x07\x1b]*(?:\x07|" + _ESC + r"\\)?"  # OSC, terminated by BEL or ST
    "|" + _ESC + r"[()][@-~]"  # charset designation
    "|" + _ESC + r"[@-Z\\-_]"  # the remaining two-character escapes
)
"""Everything a terminal reads as instruction rather than as text.

⚠ The private-parameter forms are the whole reason this exists. A parked agy pane emits
`ESC[>4;2m` and nothing else, and a naive `\\x1b\\[[0-9;]*m` leaves every one of them in
place — which is how the first pass at this still counted a finished session as busy.
"""

IDLE_REPAINT_TICKS_PER_SEC = 1.0
"""CPU below this rate is indistinguishable from a TUI redrawing an idle prompt.

Measured on hapl-aux on 2026-09-21 against five agy panes that had been finished for
between sixteen and nineteen hours: each one burned 0.36 to 0.52 ticks per second and
wrote 450 bytes a minute, all of it escape codes and not one visible character. The floor
is set at roughly twice the worst of those, and it only ever applies to a runner that
declares itself a TUI.
"""


def sh(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or r.stderr).strip()


PANE_SIZE = os.environ.get("FLEET_PANE_SIZE", "120x30")
"""How big a dispatched pane is, as `WIDTHxHEIGHT`.

⚠ Small on purpose. A pinned pane does not shrink to fit the terminal you attach from, so
one bigger than that terminal is clipped, while one smaller is merely padded. 120x30 fits
inside anything you would attach from; raise it only if every client you use is larger.
"""


def pane_size() -> tuple[int, int]:
    try:
        w, h = PANE_SIZE.lower().split("x")
        return max(int(w), 20), max(int(h), 10)
    except ValueError:
        return 120, 30


def tmux_spawn(name: str, command: str, fixed: bool = False) -> int:
    """Start a detached tmux session and return the pane's pid.

    The pid is the shell running the wrapper, not the agent binary. That is deliberate:
    the wrapper is what reports the exit, so its death is the event worth watching.

    ⚠ The size is given explicitly because a detached `new-session` otherwise gets tmux's
    `default-size`, which is 80x24. Every log was being wrapped at eighty columns and
    every pane was drawn for a terminal nobody uses.

    ⚠ `fixed` pins the window so attaching does not resize it. tmux defaults to
    `window-size latest`, so attaching a 156x35 client to an 80x24 pane resizes the pane
    and sends the agent a SIGWINCH. agy does not act on one: resizing a live pane from
    200x50 to 120x30 on 2026-09-21 reflowed not a single character, so what you attach to
    is a frame laid out for a width that is no longer there, with no prompt in it. Only a
    runner whose log is a terminal needs this — plain text has no frame to break.
    """
    w, h = pane_size()
    rc, out = sh(["tmux", "new-session", "-d", "-s", name, "-x", str(w), "-y", str(h), command])
    if rc != 0:
        raise RuntimeError(f"tmux refused to start {name}: {out}")
    if fixed:
        # Not fatal if it fails: the session is already running, and an unpinned pane is
        # awkward to attach to rather than broken.
        sh(["tmux", "setw", "-t", name, "window-size", "manual"])
    rc, out = sh(["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"])
    if rc != 0 or not out.strip().isdigit():
        raise RuntimeError(f"{name} started but has no pane pid: {out}")
    return int(out.strip().splitlines()[0])


def tmux_alive(name: str) -> bool:
    return sh(["tmux", "has-session", "-t", name])[0] == 0


def tmux_send(name: str, text: str) -> bool:
    """Literal text, then Enter as a separate call.

    Sending both together has been observed to submit the line before the pane finished
    reading it, which drops the continuation and leaves the session looking busy.
    """
    if sh(["tmux", "send-keys", "-t", name, "-l", text])[0] != 0:
        return False
    time.sleep(0.2)
    return sh(["tmux", "send-keys", "-t", name, "Enter"])[0] == 0


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def marker_state(agent_type: str, pid: int | None) -> tuple[str, float] | None:
    """ccmux's own view of a session, found by pid because we do not know its session id."""
    if not pid or not MARKERS.is_dir():
        return None
    for f in MARKERS.glob(f"{agent_type}-*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("pid") == pid and d.get("state"):
            return str(d["state"]), float(d.get("state_timestamp") or 0.0)
    return None


def _children() -> dict[int, list[int]]:
    """ppid -> children, from one pass over /proc."""
    kids: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return kids
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8") as fh:
                fields = fh.read().rsplit(") ", 1)[-1].split()
            ppid = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(name))
    return kids


def _own_ticks(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            fields = fh.read().rsplit(") ", 1)[-1].split()
        return int(fields[11]) + int(fields[12])  # utime + stime
    except (OSError, IndexError, ValueError):
        return 0


def cpu_ticks(pid: int | None) -> int | None:
    """CPU burned by this process **and everything under it**.

    ⚠ The whole tree, not the one pid, and the difference is the whole point. The handle's
    pid is the tmux pane's shell — the wrapper — because the wrapper is what reports the
    exit. But the wrapper does nothing at all while the agent works, so its own CPU never
    advances. Sampling only that made every claude session look idle within the hour, and
    on the first real run the watchdog reaped one that had burned 32 seconds of CPU and was
    still going.
    """
    if not pid:
        return None
    if not os.path.isdir(f"/proc/{pid}"):
        return None
    kids = _children()
    total, stack, seen = 0, [pid], set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        total += _own_ticks(current)
        stack.extend(kids.get(current, ()))
    return total


def reap(pid: int | None, tmux_name: str | None) -> None:
    """Stop a session and everything it started.

    ⚠ Requeueing without this leaves the old agent alive: it keeps burning quota, it may
    still be writing to the same worktree, and the relaunch collides on the tmux name and
    fails. That happened on the first real run — `tmux refused to start: duplicate session`
    — and the session landed in needs_you for a reason that was entirely our own doing.

    tmux first so the pane cannot respawn, then the pid, because killing the session alone
    has been observed to leave an orphan agent behind.
    """
    if tmux_name:
        sh(["tmux", "kill-session", "-t", tmux_name])
    if pid:
        kids = _children()
        stack, seen = [pid], set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(kids.get(current, ()))
        for victim in seen:
            with contextlib.suppress(OSError):
                os.kill(victim, 15)


def _sample(
    state_dir: Path, kind: str, session: str, now: int, record: bool
) -> tuple[int, float] | None:
    """Read the last reading of `kind` for this session, then optionally store `now`.

    Returns the previous value and the wall time it was taken at, or None on the first
    look. Readings are written as `<value> <timestamp>`; a bare integer is the older
    format and is treated as no baseline rather than guessed at, which costs one window.

    ⚠ `record=False` for a caller that is only looking. The board polls every five
    seconds; letting it move the baseline would quietly replace the watchdog's
    thirty-minute window with a five-second one.
    """
    path = state_dir / kind / f"{session}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    before: tuple[int, float] | None = None
    try:
        parts = path.read_text(encoding="utf-8").split()
        if len(parts) == 2:
            before = (int(parts[0]), float(parts[1]))
    except (OSError, ValueError):
        before = None
    if record:
        with contextlib.suppress(OSError):
            path.write_text(f"{now} {time.time()}", encoding="utf-8")
    return before


def visible_len(path: str | Path) -> int | None:
    """How many characters of actual text this log holds, escape codes and spacing removed.

    ⚠ File size is not this number, and the gap between them is where a finished session
    hid for nineteen hours. An agent CLI left at its prompt keeps the terminal alive with
    control sequences, so the log grows forever while saying nothing. Stripping them and
    dropping whitespace leaves only what the agent actually said, which is the thing worth
    asking whether it changed.

    Whitespace goes because a redraw repositions and repads without adding a word.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return len(re.sub(r"\s+", "", ANSI.sub("", raw)))


def text_advanced(
    session: str, log: str | None, state_dir: Path, record: bool = True
) -> bool | None:
    """Has the agent said anything new since the last time we looked?

    This replaced the log's mtime, which asked whether the file had been *written to* and
    so answered yes for a pane that was only redrawing itself. None means no baseline yet.
    """
    if not log:
        return None
    now = visible_len(log)
    if now is None:
        return None
    before = _sample(state_dir, "text", session, now, record)
    return None if before is None else now > before[0]


def cpu_advanced(
    session: str,
    pid: int | None,
    state_dir: Path,
    record: bool = True,
    floor: float = 0.0,
) -> bool | None:
    """Has this process used CPU since the last time we looked?

    The one activity signal that holds for every runner, because it asks the kernel rather
    than the agent. Judging remote sessions by CPU time is what worked by hand long before
    any of this existed.

    ⚠ `floor` exists because "any CPU at all" is not evidence of work when the process is
    a TUI. A pane parked at its prompt still redraws, and that redraw alone kept five
    finished sessions marked working overnight. Above the floor the rate is work; at it,
    the kernel is only telling us the terminal is still painted. Non-TUI runners pass 0
    and keep the original any-advance reading, which is what a claude session in print
    mode needs — it writes nothing until the end, so CPU is all it has.
    """
    now = cpu_ticks(pid)
    if now is None:
        return None
    before = _sample(state_dir, "cpu", session, now, record)
    if before is None:
        return None
    if floor <= 0:
        return now > before[0]
    elapsed = time.time() - before[1]
    if elapsed <= 0:
        return None
    return (now - before[0]) / elapsed > floor


def liveness(
    agent_type: str,
    handle: Handle,
    idle_after: float = 600.0,
    state_dir: Path | None = None,
    record: bool = True,
    tui: bool = False,
) -> State:
    """working, idle or gone. Three signals, and working wins.

    ⚠ **A marker that says `idle` proves nothing.** Claude Code's ccmux hooks fire on
    session start, session end and notifications — none of them writes `working` — so a
    claude session marks itself idle and stays that way for its entire run. Observed live
    on 2026-09-19 while the session was demonstrably working. agy has the mirror problem:
    its hooks would say `working` but do not fire at all.

    So a marker is only ever evidence *for* working, never against it.

    The log is the second signal. It asks whether the agent said anything new, not whether
    the file grew — see `visible_len`. It still fails for claude specifically, which writes
    nothing at all until the very end because piping stdout puts it in print mode.

    CPU time is the third and the only one that holds when a runner is silent by design.

    ⚠ `tui` is for a runner whose log carries a terminal interface rather than plain text.
    Such a session never stops emitting, so both of the last two signals read as work
    forever once it finishes: the file keeps growing and the kernel keeps counting. On
    2026-09-21 that held five finished agy panes at `working` for up to nineteen hours and
    silently disabled the stall watchdog, because `liveness` could not return IDLE for
    them at all. Declaring the runner a TUI puts a floor under the CPU reading; the text
    signal needs no flag, since stripping escape codes from a log that has none is free.

    Idle means every signal came back negative.
    """
    if not (pid_alive(handle.pid) or (handle.tmux and tmux_alive(handle.tmux))):
        return State.GONE

    m = marker_state(agent_type, handle.pid)
    if m and m[0] == "working" and (time.time() - m[1]) < MARKER_STALE:
        return State.WORKING

    if state_dir is None:
        # No place to keep a baseline, so the best available reading is the old one: the
        # log was touched recently. Wrong for a TUI, but better than calling it gone.
        if handle.log:
            with contextlib.suppress(OSError):
                if (time.time() - Path(handle.log).stat().st_mtime) < idle_after:
                    return State.WORKING
        return State.IDLE

    spoke = text_advanced(handle.session, handle.log, state_dir, record=record)
    if spoke:
        return State.WORKING

    # ⚠ None from a session that has a log is the first look at it, not silence — the
    # baseline was written by the call we are inside. None from one with no log at all is
    # a different thing: that runner has only CPU to speak for it, and CPU gets the last
    # word. Collapsing the two reaps a fresh dispatch or keeps a dead one alive.
    first_look = bool(handle.log) and spoke is None

    floor = IDLE_REPAINT_TICKS_PER_SEC if tui else 0.0
    burned = cpu_advanced(handle.session, handle.pid, state_dir, record=record, floor=floor)
    if burned is not False:
        # True is working; None means we have no baseline yet, and refusing to call a
        # session idle on the first look is what stops a fresh dispatch being reaped.
        return State.WORKING

    return State.WORKING if first_look else State.IDLE


def process_lines() -> list[str]:
    """Every running command line on the box. One call, because three greps is three forks."""
    rc, out = sh(["ps", "-eo", "args="])
    return out.splitlines() if rc == 0 else []


def count_matching(lines: list[str], needle: str, exclude: tuple[str, ...] = ()) -> int:
    """How many live processes carry `needle` and none of `exclude` in their command line.

    ⚠ This counts sessions fleet did not start, which is the point. A box runs agents
    dispatched by hand and by other tooling, and a scheduler that only counts its own would
    stack its whole ceiling on top of theirs.

    ⚠ `exclude` exists because an agent CLI is not one process. Claude Code runs a session
    as the versioned binary and puts a daemon, a pty host and a spare beside it, all with
    the same install path on their command line. Counting the path alone turns one session
    into four and the ceiling shuts.
    """
    return sum(1 for line in lines if needle in line and not any(x in line for x in exclude))


def count_by_env(var: str, value: str, needle: str) -> int:
    """Live processes matching `needle` that were started with `var=value`.

    ⚠ cursor-agent takes no flag naming its account. A cursor profile is an
    XDG_CONFIG_HOME overlay, so two accounts produce byte-identical command lines and
    `count_matching` would put every running session on whichever profile it checked
    first. /proc is the only place the answer exists.

    A pid that vanishes or cannot be read is skipped rather than guessed at. Under-counting
    makes fleet start a session it did not need to; guessing makes it start one on an
    account already at its ceiling.
    """
    want = f"{var}={value}".encode()
    n = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if needle.encode() not in entry.joinpath("cmdline").read_bytes():
                continue
            if want in entry.joinpath("environ").read_bytes().split(bytes(1)):
                n += 1
        except OSError:
            continue
    return n


def fleet_env() -> dict[str, str]:
    """The fleet configuration the wrapper must carry with it.

    ⚠ A tmux session inherits the tmux **server's** environment, not the environment of the
    shell that spawned it. When a server is already running — and on this box one always is
    — every FLEET_* variable the dispatcher resolved is simply absent inside the pane. The
    first live test of the handshake failed exactly here: the command ran, the session
    exited, and the wrapper reported into a different ledger than the one dispatching it.

    So the values travel in the command itself. Nothing about the wrapper may depend on
    what happens to be exported where it lands.
    """
    return {k: v for k, v in os.environ.items() if k.startswith("FLEET_")}


WRAPPER = """#!/usr/bin/env bash
# fleet wrapper for {session}. Written by fleet on dispatch; do not edit.
#
# This is the whole handshake. The agent exits, this records it, then this calls the tick
# itself, so the next session starts without anything polling.
set -o pipefail
{exports}
{runner_cmd} 2>&1 | tee {log}
rc=${{PIPESTATUS[0]}}
{fleet} event exited --session {session_q} --rc "$rc"
{fleet} tick
"""


def write_wrapper(
    session: str,
    runner_cmd: str,
    log: str,
    script: str,
    fleet_bin: str = "fleet",
    env: dict[str, str] | None = None,
) -> str:
    """Write the wrapper as a bash script and return the command tmux should run.

    ⚠ It is a script, and it is explicitly bash, for two reasons found the hard way on the
    box.

    tmux starts a command with the user's login shell, which here is zsh — and zsh has no
    `PIPESTATUS`, it has `$pipestatus`. So the exit code arrived empty, `fleet event` was
    called with no `--rc`, argparse rejected it, and the session vanished from the ledger's
    point of view while looking like it had simply never finished.

    A tmux session also inherits the tmux **server's** environment rather than the spawning
    shell's, so every FLEET_* variable the dispatcher resolved was absent inside the pane.
    They are exported by the script instead.

    Writing a file rather than a one-liner removes the quoting risk as well: a brief is
    thousands of words of prose, and the command line was never the right place for it.
    """
    q = shlex.quote
    pairs = (env if env is not None else fleet_env()).items()
    exports = "\n".join(f"export {k}={q(v)}" for k, v in sorted(pairs))
    body = WRAPPER.format(
        session=session,
        session_q=q(session),
        exports=exports,
        runner_cmd=runner_cmd,
        log=q(log),
        fleet=q(fleet_bin),
    )
    path = Path(script)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return f"bash {q(str(path))}"
