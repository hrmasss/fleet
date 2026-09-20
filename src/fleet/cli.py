"""The command surface.

Every verb is declared here from the start, including the ones that are not built yet,
because the surface is a design decision and reviewing it is cheaper than discovering it.
An unbuilt verb exits 3 and names the phase it arrives in rather than failing vaguely.

⚠ The permission wall lives in this file, not in the skill document. A skill document is a
request; a refusal in the CLI is a rule. Any session on the workstation reaches this binary over
`ssh the box fleet …`, and the flags listed in MUTATIONS refuse when that is how they
arrived, whatever the caller believes it is allowed to do. Raising the foreman's autonomy
or releasing a brake is a decision about how much the operator trusts the system, and a session
asking for more trust is not evidence that it deserves more. Reading is never refused.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from fleet import __version__

MUTATIONS: dict[str, tuple[str, ...]] = {
    "foreman": ("autonomy",),
    "brake": ("stop", "release"),
}
"""Flags that change what the system is permitted to do on its own. Refused over SSH.

⚠ The wall is on the mutation, never on the noun. Walling the whole verb was tried twice
and was wrong both times: it blocked reading the autonomy rung and reading the brake state
from the workstation, which are exactly the things a session there should be able to do, and it
bought no safety at all. Reading is never refused. Moving a rung or releasing a brake is
the operator's, at the box.
"""

LOCAL_ONLY: frozenset[str] = frozenset()
"""Verbs refused outright over SSH. Empty on purpose — see MUTATIONS."""


def walled(ns) -> str | None:
    """Why this exact invocation is refused over SSH, or None."""
    if not over_ssh():
        return None
    if ns.verb in LOCAL_ONLY:
        return f"'{ns.verb}' is refused over ssh"
    for flag in MUTATIONS.get(ns.verb, ()):
        if getattr(ns, flag, None):
            return f"'{ns.verb} --{flag}' changes what the system may do on its own"
    return None


BUILT: frozenset[str] = frozenset(
    {
        "gate",
        "add",
        "start",
        "pause",
        "drain",
        "park",
        "status",
        "show",
        "log",
        "nudge",
        "tick",
        "watch",
        "event",
        "foreman",
        "digest",
        "brake",
        "limits",
    }
)
"""Verbs with behaviour. Everything else exits 3 naming its phase."""

PHASES: dict[str, int] = {
    "gate": 1,
    "add": 2,
    "start": 2,
    "pause": 2,
    "drain": 2,
    "park": 2,
    "status": 2,
    "show": 2,
    "log": 2,
    "nudge": 2,
    "tick": 2,
    "watch": 2,
    "event": 2,
    "foreman": 3,
    "brake": 3,
    "digest": 4,
    "limits": 4,
}

VERBS: dict[str, str] = {
    "gate": "check whether a session truly shipped: merged, built, running, recorded",
    "add": "enqueue briefed sessions",
    "start": "drain the queue across every runner with capacity",
    "pause": "stop dispatching, leave what is running alone",
    "drain": "finish what is running, start nothing new",
    "park": "take a session out of the queue by hand",
    "status": "the board, as a table or --json",
    "show": "one session: state, attempts, last verdict, how to attach",
    "log": "tail a session's transcript",
    "nudge": "send a continuation into a live session",
    "tick": "reconcile, verify and dispatch once",
    "watch": "block on the event stream",
    "event": "record what a runner wrapper reports (called by the wrapper, not by you)",
    "foreman": "read or move the foreman's autonomy rung",
    "brake": "show or move the brakes: the kill file and the attempt cap",
    "digest": "compose the end-of-day summary",
    "limits": "what every account has left, and when it resets",
}


def over_ssh() -> bool:
    """True when this process was started by an incoming SSH command.

    Both variables are set by sshd for a remote command; neither is set for a login shell
    the operator is sitting in. That is a weaker check than an allowlist of keys and it is meant
    to be: this stops an agent reaching for a verb it should not, not an attacker.
    """
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fleet",
        description="Runner-agnostic dispatch queue for agy, claude and cursor sessions.",
    )
    p.add_argument("--version", action="version", version=f"fleet {__version__}")
    p.add_argument(
        "--actor",
        default=os.environ.get("FLEET_ACTOR"),
        help="who is calling, recorded in the ledger (default: $FLEET_ACTOR)",
    )
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    s: dict[str, argparse.ArgumentParser] = {}
    for verb, help_text in VERBS.items():
        s[verb] = sub.add_parser(verb, help=help_text)

    s["gate"].add_argument("sessions", nargs="+", metavar="SESSION")
    s["gate"].add_argument("--json", action="store_true")

    s["add"].add_argument("sessions", nargs="*", metavar="SESSION")
    s["add"].add_argument(
        "--claimable", action="store_true", help="every session file marked claimable"
    )
    s["add"].add_argument(
        "--runner", choices=("agy", "claude", "cursor"), help="pin these to one runner"
    )
    s["add"].add_argument(
        "--model",
        help="the model to run, which on agy also picks the allowance it spends "
        "(gemini-* draws the Gemini pool, claude-* and gpt-* the separate Claude/GPT one)",
    )

    s["start"].add_argument(
        "--parallel", type=int, default=None, metavar="N", help="cap how many to start this pass"
    )
    s["tick"].add_argument("--parallel", type=int, default=None, metavar="N")
    s["tick"].add_argument(
        "--reconcile", action="store_true", help="reconcile and verify only, start nothing"
    )

    s["status"].add_argument("--json", action="store_true")
    s["show"].add_argument("session", metavar="SESSION")
    s["log"].add_argument("session", metavar="SESSION")
    s["log"].add_argument("-n", type=int, default=40, help="lines from the end")

    s["nudge"].add_argument("session", metavar="SESSION")
    s["nudge"].add_argument("text", help="the continuation, in quotes")

    s["park"].add_argument("session", metavar="SESSION")
    s["park"].add_argument("--note", default="")

    s["watch"].add_argument(
        "--kinds", default=None, help="comma separated, e.g. exited,verdict,needs_you"
    )
    s["watch"].add_argument("--interval", type=float, default=2.0)

    s["foreman"].add_argument(
        "--autonomy",
        choices=("off", "observe", "act", "decide"),
        help="move the rung. Refused over ssh; run it at the box",
    )
    s["foreman"].add_argument(
        "--judge", metavar="SESSION", help="judge one session now and print the verdict"
    )
    s["foreman"].add_argument("--model", default=None)

    s["limits"].add_argument("--json", action="store_true")

    s["brake"].add_argument("--stop", metavar="WHY", help="halt all dispatch")
    s["brake"].add_argument("--release", action="store_true", help="resume dispatch")

    s["digest"].add_argument("--send", action="store_true", help="deliver it, rather than print it")

    s["digest"].add_argument(
        "--urgent", action="store_true", help="only the verdicts that cannot wait"
    )

    s["digest"].add_argument("--hours", type=float, default=24.0)

    s["event"].add_argument("kind", metavar="KIND")
    s["event"].add_argument("--session", required=True)
    s["event"].add_argument("--rc", type=int, default=None)

    for verb in ("pause", "drain"):
        s[verb].add_argument("--note", default="")
    for verb, parser in s.items():
        if verb not in BUILT:
            parser.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    return p


def _pause(note: str, drain: bool) -> int:
    """Pause writes the kill file; drain is the same brake with a softer name.

    Both stop new dispatch and neither touches what is running: killing live work to stop
    a queue would make the brake something nobody reaches for.
    """
    from fleet.scheduler import KILL_FILE

    KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    KILL_FILE.write_text(note or ("draining" if drain else "paused by hand"), encoding="utf-8")
    print(f"dispatch stopped. resume by removing {KILL_FILE}")
    return 0


def _log(session_id: str, lines: int) -> int:
    from fleet.ledger import Ledger
    from fleet.paths import ledger_path

    led = Ledger(ledger_path())
    it = led.get(session_id)
    led.close()
    if it is None or not it.log:
        print(f"fleet log: no transcript recorded for {session_id}", file=sys.stderr)
        return 2
    try:
        with open(it.log, encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except OSError as e:
        print(f"fleet log: {e}", file=sys.stderr)
        return 2
    sys.stdout.writelines(tail)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)

    if not ns.verb:
        parser.print_help()
        return 0

    refusal = walled(ns)
    if refusal:
        print(f"fleet: {refusal}. Run it at the box.", file=sys.stderr)
        return 4

    if ns.verb not in BUILT:
        print(
            f"fleet: '{ns.verb}' is not built yet, phase {PHASES[ns.verb]}. See docs/design.md.",
            file=sys.stderr,
        )
        return 3

    from fleet import commands

    match ns.verb:
        case "gate":
            return commands.check(ns.sessions, ns.json)
        case "add":
            return commands.add(ns.sessions, ns.actor, ns.runner, ns.claimable, ns.model)
        case "start":
            return commands.start(ns.parallel)
        case "tick":
            return commands.tick(ns.parallel, dispatch=not ns.reconcile)
        case "status":
            return commands.status(ns.json)
        case "show":
            return commands.show(ns.session)
        case "log":
            return _log(ns.session, ns.n)
        case "nudge":
            return commands.nudge(ns.session, ns.text)
        case "park":
            return commands.park(ns.session, ns.note)
        case "watch":
            return commands.watch(ns.kinds, ns.interval)
        case "event":
            return commands.event(ns.session, ns.kind, ns.rc)
        case "foreman":
            return commands.foreman(ns.autonomy, ns.judge, ns.model)
        case "digest":
            return commands.digest(ns.send, ns.urgent, ns.hours)
        case "brake":
            return commands.brake(ns.stop, ns.release)
        case "limits":
            return commands.limits(ns.json)
        case "pause" | "drain":
            return _pause(ns.note, drain=ns.verb == "drain")
    return 3
