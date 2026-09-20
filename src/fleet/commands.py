"""What each queue verb does, kept out of `cli.py` so argument parsing stays readable.

Everything here goes through the ledger and the scheduler. Nothing reaches for a runner by
name, and nothing writes to a session file — that belongs to the agent working it.
"""

from __future__ import annotations

import json
import sys
import time

from fleet import gate, scheduler
from fleet import session as sessions
from fleet.adapters import registry
from fleet.ledger import SETTLED, Ledger, State
from fleet.paths import ledger_path, read_cap
from fleet.project import Project


def project() -> Project:
    """The configured project, resolved on use.

    ⚠ Not a module-level constant. A constant means importing anything under `fleet`
    reads the config file, so `fleet --help` on a machine that has not been set up yet
    dies with a config error instead of printing help.
    """
    return Project.load()


def _open() -> tuple[Ledger, scheduler.Scheduler]:
    """A ledger and a scheduler wired to the real world.

    The foreman is always constructed. Whether it is allowed to matter is the autonomy
    rung's business, and at `observe` — where it ships — it judges everything and moves
    nothing.
    """
    from fleet.foreman.judge import Foreman

    led = Ledger(ledger_path())
    return led, scheduler.Scheduler(project(), led, registry(), foreman=Foreman())


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = max(0, int(time.time() - ts))
    if d < 90:
        return f"{d}s"
    if d < 5400:
        return f"{d // 60}m"
    return f"{d // 3600}h{(d % 3600) // 60:02d}"


def add(
    ids: list[str],
    actor: str | None,
    runner: str | None,
    claimable: bool,
    model: str | None = None,
) -> int:
    """Queue work. This is the one thing only a human starts.

    The foreman may requeue, park and reorder what is here. It may never add to it, which
    is what keeps an autonomous loop from inventing its own backlog.
    """
    led, _ = _open()
    if claimable:
        ids = ids + _claimable_ids()
    if not ids:
        print("fleet add: nothing to queue", file=sys.stderr)
        return 2

    added, skipped = [], []
    for sid in dict.fromkeys(ids):
        try:
            path = sessions.find(project().sessions, sid)
            s = sessions.load(path)
        except (FileNotFoundError, ValueError, OSError) as e:
            print(f"fleet add: {sid}: {e}", file=sys.stderr)
            skipped.append(sid)
            continue
        if led.add(s.id, actor=actor, runner=runner or s.runner, model=model or s.model):
            added.append(s.id)
        else:
            skipped.append(s.id)

    for sid in added:
        print(f"queued   {sid}")
    for sid in skipped:
        print(f"skipped  {sid} (already known, or unreadable)")
    return 0 if added else 2


def _claimable_ids() -> list[str]:
    out = []
    for path in sorted(project().sessions.glob("*.md")):
        try:
            s = sessions.load(path)
        except (ValueError, OSError):
            continue
        if s.status.strip().lower() == "claimable":
            out.append(s.id)
    return out


def start(limit: int | None) -> int:
    """Drain the queue. `--parallel N` sets the standing concurrency cap, not a per-pass
    one, because the runner wrapper and the hourly net both call `tick` without it."""
    from fleet.paths import write_cap

    if limit is not None:
        write_cap(limit)
    led, sch = _open()
    out = sch.tick()
    _report(out)
    led.close()
    return 0


def tick(limit: int | None, dispatch: bool = True) -> int:
    led, sch = _open()
    out = sch.tick(limit=limit, dispatch=dispatch)
    _report(out)
    led.close()
    return 0


def _report(out: scheduler.Outcome) -> None:
    if out.halted:
        print(f"HALTED: {out.halted}")
    for label, items in (
        ("dispatched", out.dispatched),
        ("closed", out.closed),
        ("relaunched", out.relaunched),
        ("stalled", out.stalled),
        ("needs you", out.escalated),
    ):
        if items:
            print(f"{label:<11} {', '.join(items)}")
    if out.quiet and not out.halted:
        print("nothing to do")


def _titles() -> dict[str, str]:
    """Session id to its one-line title, for the board's preview column.

    Read from the session files rather than stored in the ledger: the file is the work
    record and its title can be edited there, and a copy in the ledger would start
    disagreeing with it the first time someone did.
    """
    out: dict[str, str] = {}
    try:
        paths = sorted(project().sessions.glob("*.md"))
    except OSError:
        return out
    for path in paths:
        try:
            session = sessions.load(path)
        except (ValueError, OSError):
            continue
        if session.title:
            out[session.id] = session.title
    return out


def _live_states(led: Ledger, sch: scheduler.Scheduler) -> dict[str, str]:
    """working or idle for each running session.

    ⚠ Read-only: `record=False` keeps this from moving the CPU baseline. The board polls
    every five seconds, and letting it sample would quietly replace the watchdog's
    thirty-minute window with a five-second one.
    """
    from fleet.adapters.base import Handle

    out: dict[str, str] = {}
    for it in led.in_state(State.RUNNING):
        adapter = sch.adapters.get(it.runner or "")
        if adapter is None:
            continue
        handle = Handle(
            it.runner or "",
            it.session,
            it.pid or 0,
            account=it.account,
            tmux=it.tmux,
            log=it.log,
        )
        try:
            out[it.session] = adapter.liveness(handle, record=False).value
        except TypeError:
            out[it.session] = adapter.liveness(handle).value
    return out


def status(as_json: bool) -> int:
    """The board's only source. The Go board reads exactly this and nothing else."""
    led, sch = _open()
    items = led.all()
    titles = _titles()
    live = _live_states(led, sch) if as_json else {}
    caps = {name: a.capacity() for name, a in sch.adapters.items()}
    running = led.running_by_account()

    if as_json:
        print(
            json.dumps(
                {
                    "halted": sch.halted(),
                    "runners": [
                        {
                            "name": name,
                            "reason": c.reason,
                            "accounts": [
                                {
                                    "name": acct,
                                    "ceiling": ceiling,
                                    "running": max(
                                        running.get((name, acct), 0), c.observed.get(acct, 0)
                                    ),
                                    "ours": running.get((name, acct), 0),
                                    "detail": c.detail.get(acct, ""),
                                    "quota": [
                                        {
                                            "id": b.id,
                                            "label": b.label,
                                            "group": b.group,
                                            "remaining": b.remaining,
                                            "resets_at": b.resets_at,
                                            "detail": b.detail,
                                            "brief": b.brief,
                                        }
                                        for b in c.quota.get(acct, [])
                                    ],
                                }
                                for acct, ceiling in c.per_account.items()
                            ],
                        }
                        for name, c in caps.items()
                    ],
                    "items": [
                        {
                            "session": i.session,
                            "title": titles.get(i.session, ""),
                            "state": i.state.value,
                            "activity": live.get(i.session, ""),
                            "runner": i.runner,
                            "wanted": i.wanted,
                            "model": i.model,
                            "account": i.account,
                            "attempts": i.attempts,
                            "verdict": i.verdict,
                            "note": i.note,
                            "tmux": i.tmux,
                            "log": i.log,
                            "queued_at": i.queued_at,
                            "started_at": i.started_at,
                            "ended_at": i.ended_at,
                        }
                        for i in items
                    ],
                },
                indent=2,
            )
        )
        led.close()
        return 0

    halt = sch.halted()
    if halt:
        print(f"HALTED  {halt}\n")
    limit = read_cap()
    if limit is not None:
        live = len([i for i in items if i.live])
        print(f"cap     {live}/{limit} running")
    for name, c in caps.items():
        if c.reason:
            print(f"{name:<7} unavailable: {c.reason}")
            continue
        for line in _strip(name, c, running):
            print(line)
    print()
    if not items:
        print("queue empty")
    for i in items:
        where = f"{i.runner or i.wanted or '-'}/{i.account or '-'}"
        age = _ago(i.started_at or i.queued_at)
        line = f"{i.state.value:<10} {i.session:<10} {where:<14} {age:>6}"
        if i.attempts > 1:
            line += f"  attempt {i.attempts}"
        if i.verdict:
            line += f"  {i.verdict}"
        print(line)
    led.close()
    return 0


def show(session_id: str) -> int:
    led, _ = _open()
    it = led.get(session_id)
    if it is None:
        print(f"fleet show: {session_id} is not in the queue", file=sys.stderr)
        led.close()
        return 2
    print(f"{it.session}  {it.state.value}")
    print(f"  runner    {it.runner or '-'}/{it.account or '-'}")
    print(
        f"  asked for {it.wanted or 'wherever it fits'}" + (f" on {it.model}" if it.model else "")
    )
    print(f"  attempts  {it.attempts}")
    print(f"  queued    {_ago(it.queued_at)} ago")
    if it.started_at:
        print(f"  started   {_ago(it.started_at)} ago")
    if it.verdict:
        print(f"  verdict   {it.verdict}")
    if it.tmux:
        print(f"  attach    tmux attach -t {it.tmux}")
    if it.log:
        print(f"  log       {it.log}")
    if it.note:
        print(f"\n{it.note}")
    led.close()
    return 0


def nudge(session_id: str, text: str) -> int:
    """Put a continuation into a live session.

    A nudge that silently does nothing is worse than no nudge, so a runner that cannot take
    one says so and the caller is told to relaunch instead.
    """
    led, sch = _open()
    it = led.get(session_id)
    if it is None or not it.live:
        print(f"fleet nudge: {session_id} is not running", file=sys.stderr)
        led.close()
        return 2
    adapter = sch.adapters.get(it.runner or "")
    if adapter is None:
        print(f"fleet nudge: no adapter for {it.runner!r}", file=sys.stderr)
        led.close()
        return 2
    from fleet.adapters.base import Handle

    handle = Handle(
        it.runner or "", it.session, it.pid or 0, account=it.account, tmux=it.tmux, log=it.log
    )
    ok = adapter.nudge(handle, text)
    led.record(session_id, "nudged" if ok else "nudge_failed", text[:200])
    led.close()
    if not ok:
        print(f"fleet nudge: {it.runner} would not take it — relaunch instead", file=sys.stderr)
        return 1
    print(f"nudged {session_id}")
    return 0


def park(session_id: str, note: str) -> int:
    led, _ = _open()
    if led.get(session_id) is None:
        print(f"fleet park: {session_id} is not in the queue", file=sys.stderr)
        led.close()
        return 2
    led.transition(session_id, State.PARKED, note=note or "parked by hand")
    led.close()
    print(f"parked {session_id}")
    return 0


def watch(kinds: str | None, interval: float) -> int:
    """Block on the event stream. This is the inbound half of the workstation handshake.

    Polling the ledger rather than waiting on a signal is deliberate: SQLite has no
    notification and a poll of a WAL file costs nothing next to what the agents cost.
    """
    led, _ = _open()
    wanted = {k.strip() for k in kinds.split(",")} if kinds else None
    last = max((e["id"] for e in led.events(limit=1000)), default=0)
    try:
        while True:
            for e in led.events(since_id=last):
                last = e["id"]
                if wanted and e["kind"] not in wanted:
                    continue
                stamp = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
                print(f"{stamp}  {e['session']:<10} {e['kind']:<12} {e['detail'][:80]}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        led.close()


def event(session_id: str, kind: str, rc: int | None) -> int:
    """Called by the runner wrapper. This is the push half of the handshake.

    An exit moves the session to `verifying` rather than judging it here: the wrapper runs
    as the agent's own shell and must return fast, and judging is the next tick's job.
    """
    led, _ = _open()
    it = led.get(session_id)
    if it is None:
        # ⚠ Loud on purpose. A wrapper reporting a session this ledger has never heard of
        # means it is talking to the wrong ledger, which is a misconfiguration that looks
        # exactly like a session that quietly never finished. Returning 0 here hid that
        # bug through the whole first live test of the handshake.
        led.record(session_id, f"orphan_{kind}", f"rc={rc}")
        print(
            f"fleet event: {session_id} is not in this ledger ({led.path}). "
            f"The wrapper is reporting somewhere the dispatcher is not reading.",
            file=sys.stderr,
        )
        led.close()
        return 2
    if kind == "exited":
        led.transition(session_id, State.VERIFYING, kind="exited", note=f"exit code {rc}")
    else:
        led.record(session_id, kind, f"rc={rc}" if rc is not None else "")
    led.close()
    return 0


def check(ids: list[str], as_json: bool) -> int:
    """`fleet gate`, kept here so the CLI has one shape for every verb."""
    probe = gate.ShellProbe(project())
    results, unreadable = [], []
    for sid in ids:
        try:
            s = sessions.load(sessions.find(project().sessions, sid))
        except (FileNotFoundError, ValueError, OSError) as e:
            unreadable.append(f"{sid}: {e}")
            continue
        results.append(gate.check(s, probe))

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "session": r.session,
                        "shipped": r.shipped,
                        "verdict": r.verdict.value if r.verdict else None,
                        "checks": [
                            {"name": c.name, "ok": c.ok, "evidence": c.evidence} for c in r.checks
                        ],
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        for r in results:
            print(f"{r.session}  {r.verdict.value if r.verdict else 'no call, foreman s'}")
            for c in r.checks:
                print(f"  {c.mark:>4}  {c.name:<9} {c.evidence}")
            print()

    for problem in unreadable:
        print(f"fleet gate: {problem}", file=sys.stderr)
    if any(not r.shipped and not r.unknown for r in results):
        return 1
    if unreadable or not results:
        return 2
    return 0 if all(r.shipped for r in results) else 2


def foreman(rung: str | None, judge_session: str | None, model: str | None) -> int:
    """Read or move the foreman's rung, or judge one session by hand.

    ⚠ Moving the rung is refused over ssh (see `cli.py`). Autonomy is earned by a week of
    verdicts the operator has read, not granted by whatever is calling.
    """
    from fleet.foreman import autonomy
    from fleet.foreman.judge import Foreman
    from fleet.foreman.packet import build as build_packet

    if rung:
        autonomy.write(autonomy.Rung(rung))
        print(f"foreman autonomy is now {rung}")
        if not judge_session:
            return 0

    if not judge_session:
        current = autonomy.read()
        print(f"autonomy   {current.value}")
        print(f"state      {autonomy.path()}")
        blurb = {
            autonomy.Rung.OFF: "no foreman; the gate decides what it can",
            autonomy.Rung.OBSERVE: "judge everything, record it, act on nothing",
            autonomy.Rung.ACT: "act on the mechanical verdicts",
            autonomy.Rung.DECIDE: "also answer questions the spec settles, cited",
        }
        for r in autonomy.Rung:
            mark = "->" if r is current else "  "
            print(f"  {mark} {r.value:<8} {blurb[r]}")
        return 0

    led, sch = _open()
    try:
        s = sessions.load(sessions.find(project().sessions, judge_session))
    except (FileNotFoundError, ValueError, OSError) as e:
        print(f"fleet foreman: {e}", file=sys.stderr)
        led.close()
        return 2

    it = led.get(judge_session)
    result = gate.check(s, gate.ShellProbe(project()))
    packet = build_packet(
        s,
        result,
        log=it.log if it else None,
        attempts=it.attempts if it else 1,
        previous_note=it.note if it else None,
    )
    f = Foreman(model=model) if model else Foreman()
    j = f.judge(packet)
    led.record(
        judge_session,
        "judged",
        f"{j.verdict.value if j.verdict else 'unreadable'} via {j.model}: {j.note}"[:400],
    )
    led.close()

    print(f"{judge_session}  {j.verdict.value if j.verdict else 'UNREADABLE'}  ({j.model})")
    print(f"  {j.note}")
    if j.citation:
        print(f"  cited: {j.citation}")
    if j.answer:
        print(f"  answer: {j.answer}")
    print()
    print(f"(autonomy is {autonomy.read().value}; this command acted on nothing)")
    return 0 if j.usable else 1


def digest(deliver: bool, urgent_only: bool, hours: float) -> int:
    """Compose the end-of-day message, or the two verdicts that cannot wait.

    Printing is the default. A digest that sends itself every time you look at it is a
    digest that trains you to ignore it.
    """
    from fleet import digest as d

    led, _ = _open()
    since = time.time() - hours * 3600

    if urgent_only:
        rows = d.urgent(led, since)
        led.close()
        if not rows:
            print("nothing that cannot wait")
            return 0
        for _sid, line in rows:
            print(line)
            if deliver and not d.send(line, subject="[fleet] needs you"):
                print("fleet digest: hermes would not send", file=sys.stderr)
                return 1
        return 0

    text = d.compose(led, project(), since=since)
    led.close()
    print(text)
    if deliver and not d.send(text, subject="[fleet] end of day"):
        print("fleet digest: hermes would not send", file=sys.stderr)
        return 1
    return 0


def brake(stop: str | None, release: bool) -> int:
    """Show or move the brakes.

    ⚠ There is no deploy cap, and the plan said there would be. fleet never deploys —
    agents do — so a cap here would have been a number that stopped nothing. What actually
    bounds the damage is the attempt cap, which limits how many times fleet can tell an
    agent to go and deploy, and the kill file, which stops dispatch entirely.

    Refused over ssh. Releasing a brake is the operator's, at the box.
    """
    from fleet.scheduler import IDLE_SECONDS, KILL_FILE, MAX_ATTEMPTS

    if release:
        KILL_FILE.unlink(missing_ok=True)
        print("dispatch released")
    elif stop is not None:
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(stop or "stopped by hand", encoding="utf-8")
        print(f"dispatch halted: {stop or 'stopped by hand'}")

    halted = KILL_FILE.read_text(encoding="utf-8").strip() if KILL_FILE.exists() else None
    print(f"dispatch   {'HALTED — ' + halted if halted else 'running'}")
    print(f"kill file  {KILL_FILE}")
    print(f"attempts   {MAX_ATTEMPTS} per session, then it parks")
    print(f"idle       {int(IDLE_SECONDS // 60)}m with no exit counts as stalled")
    print("deploys    not capped here; fleet never deploys, agents do")
    return 0


def _until(ts: float | None) -> str:
    """How long until it comes back. Relative, because a clock time makes you do the sum."""
    if not ts:
        return ""
    d = int(ts - time.time())
    if d <= 0:
        return "due"
    if d < 3600:
        return f"in {d // 60}m"
    if d < 86400:
        return f"in {d // 3600}h{(d % 3600) // 60:02d}"
    return f"in {d // 86400}d{(d % 86400) // 3600:02d}h"


SHORT = {
    "gemini-5h": "gem 5h",
    "gemini-weekly": "gem wk",
    "3p-5h": "3p 5h",
    "3p-weekly": "3p wk",
    "five_hour": "5h",
    "seven_day": "7d",
    "on_demand": "on-dem",
}
"""Column heads. The full label belongs in `fleet limits`; here every account has to fit
on one line."""


def _cell(b) -> str:
    """What a bucket reads. A runner that metered nothing says so rather than showing a
    blank, because a blank and a full tank must never look the same."""
    if b.remaining is None:
        return (b.brief or "—").strip()
    return f"{b.remaining * 100:.0f}%"


def _soonest(buckets) -> str:
    """The one reset worth printing: the emptiest bucket that is not already full. A full
    tank's refill time tells you nothing, and four of them per row is a wall of text."""
    live = [b for b in buckets if b.remaining is not None and b.remaining < 1 and b.resets_at]
    if not live:
        return ""
    b = min(live, key=lambda b: b.remaining)
    return f"{SHORT.get(b.id, b.label or b.id)} in {_until(b.resets_at)}"


def _strip(name: str, cap, running: dict) -> list[str]:
    """One small table per runner: slots used, what each allowance has left, next reset.

    ⚠ The same numbers the TUI shows. `fleet status` used to print slots alone, so the
    text board and the TUI disagreed about what you were looking at — and what gates the
    next dispatch is what an account has left, which makes it part of the status rather
    than a separate question.

    Widths come from the widest thing that must sit in each column, so the figures line up
    down the page and can be scanned without being read.
    """
    accounts = list(cap.per_account.items())
    if not accounts:
        return [f"{name:<7} no accounts"]

    label_w = max([4] + [len(a) for a, _ in accounts if a != "default"])
    cols = cap.quota.get(accounts[0][0], [])
    heads = [SHORT.get(b.id, b.label or b.id) for b in cols]
    widths = list(map(len, heads))
    for _, buckets in cap.quota.items():
        for i, b in enumerate(buckets):
            if i < len(widths):
                widths[i] = max(widths[i], len(_cell(b)))

    indent, slot_w, gap = 2, 5, 3
    out = []
    if heads:
        pad = " " * (indent + label_w + 1 + slot_w + gap - len(name) - indent)
        out.append(
            " " * indent
            + name
            + pad
            + "  ".join(h.rjust(w) for h, w in zip(heads, widths, strict=False))
        )
    else:
        out.append(" " * indent + name)

    for account, ceiling in accounts:
        # Show what dispatch actually counts, not just fleet's own. A board reading 0/2
        # while eight sessions are live is a board that gets ignored.
        busy = max(running.get((name, account), 0), cap.observed.get(account, 0))
        label = "" if account == "default" else account
        row = " " * indent + f"{label:<{label_w}} " + f"{busy}/{ceiling}".rjust(slot_w) + " " * gap
        buckets = cap.quota.get(account, [])
        if not buckets:
            out.append(row + (cap.detail.get(account) or "no usage reported"))
            continue
        row += "  ".join(_cell(b).rjust(w) for b, w in zip(buckets, widths, strict=False))
        if note := _soonest(buckets):
            row += "   " + note
        out.append(row)
    return out


def clear(sessions: list[str], everything: bool, older_than: int) -> int:
    """Drop settled sessions from the board.

    ⚠ Only settled ones. A running or verifying session is never dropped, whatever is
    asked: the row is what the wrapper reports back into and what the watchdog reconciles
    against, so removing it mid-flight orphans a live agent that nothing is counting.

    Clearing is cosmetic and local. The work record is the session file in the workspace
    repo, which fleet never writes, so nothing here loses anything — a cleared session can
    be re-added and it starts over from its file.
    """
    led, _ = _open()
    cutoff = time.time() - older_than * 3600 if older_than else None
    states = set(SETTLED) if everything else {State.DONE}

    dropped, refused = [], []
    for it in led.all():
        if sessions and it.session not in sessions:
            continue
        if it.state not in states:
            if sessions:
                refused.append(f"{it.session} is {it.state.value}")
            continue
        if cutoff is not None and (it.ended_at or it.queued_at) > cutoff:
            continue
        led.forget(it.session)
        dropped.append(it.session)
    led.close()

    for r in refused:
        print(f"kept: {r}", file=sys.stderr)
    print(f"cleared {len(dropped)}" + (": " + ", ".join(dropped) if dropped else ""))
    return 0


def limits(as_json: bool) -> int:
    """What every account has left, and when it comes back.

    Each runner meters differently — agy has a Gemini pair and a Claude-and-GPT pair,
    claude has a five-hour and a seven-day window, cursor meters included/auto/api and
    exposes none of it. A runner that reports nothing says so rather than showing an empty
    reading as a full tank.
    """
    led, sch = _open()
    caps = {name: a.capacity() for name, a in sch.adapters.items()}
    led.close()

    if as_json:
        print(
            json.dumps(
                {
                    name: {
                        acct: [
                            {
                                "id": b.id,
                                "label": b.label,
                                "group": b.group,
                                "remaining": b.remaining,
                                "resets_at": b.resets_at,
                            }
                            for b in buckets
                        ]
                        for acct, buckets in c.quota.items()
                    }
                    for name, c in caps.items()
                },
                indent=2,
            )
        )
        return 0

    for name, c in caps.items():
        for acct, buckets in c.quota.items():
            head = f"{name}/{acct}" if acct != "default" else name
            if not buckets:
                why = c.reason or c.detail.get(acct) or "nothing reported"
                print(f"{head:<12} {why}")
                continue
            print(head)
            group = None
            for b in buckets:
                if b.group and b.group != group:
                    group = b.group
                    print(f"  {group}")
                if b.pct is None:
                    note = b.detail or "not reported yet"
                    if b.detail and b.resets_at:
                        note += f", resets {_until(b.resets_at)}"
                    print(f"    {b.label:<18}       {'':<10}  {note}")
                    continue
                bar = "#" * (b.pct // 10) + "." * (10 - b.pct // 10)
                tail = _until(b.resets_at)
                if b.detail:
                    tail = f"{b.detail}  {tail}".strip()
                print(f"    {b.label:<18} {b.pct:>3}%  {bar}  {tail}")
        print()
    return 0
