"""One message a day, and two interruptions that cannot wait.

⚠ Sessions do not message WhatsApp at all any more. That line comes out of the brief
template for every runner. Thirty sessions each writing to one thread, none of them aware
of what the others said, is why the thread stopped being read — the fix is the cause, not
the volume.

Only the foreman speaks, and it speaks twice:

    immediately   `diverged` or `blocked-external` only. One line, the exact ask.
    end of day    what shipped, what is running, what needs him, every decision made on
                  their behalf with its citation, and the findings-register delta.

The digest runs as a `hermes cron` job. That scheduler is already live on the box.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from fleet.foreman.verdicts import WHATSAPP_IMMEDIATELY, Verdict
from fleet.ledger import Ledger, State
from fleet.project import Project

DAY = 86400.0


def send(text: str, subject: str = "[fleet]", target: str = "whatsapp") -> bool:
    """One message. Failing to send is reported, never swallowed: a digest that silently
    did not arrive is worse than no digest, because it looks like a quiet day."""
    try:
        r = subprocess.run(
            ["hermes", "send", "--to", target, "-s", subject],
            input=text,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return r.returncode == 0


@dataclass(frozen=True, slots=True)
class Register:
    open_rows: int
    unwalked: int

    @classmethod
    def read(cls, project: Project) -> Register:
        """The two numbers the operator is told every session whether they asked or not.

        Open findings are live on the client's storefront in front of real visitors, and a
        pass nobody walked is a flow nobody has checked.
        """
        try:
            text = (project.workspace / "todo" / "FINDINGS.md").read_text(encoding="utf-8")
        except OSError:
            return cls(-1, -1)
        rows = sum(1 for line in text.splitlines() if line.startswith("|") and "open" in line)
        walked = {line.split()[1] for line in text.splitlines() if line.startswith("## fa-")}
        every = {f"fa-{n:02d}" for n in range(1, 19)}
        return cls(rows, len(every - walked))


def compose(led: Ledger, project: Project, since: float | None = None) -> str:
    """The end-of-day message. Short enough to read on a phone, complete enough to act on."""
    since = since if since is not None else time.time() - DAY
    items = led.all()

    shipped = [i for i in items if i.state is State.DONE and (i.ended_at or 0) >= since]
    running = [i for i in items if i.state in (State.RUNNING, State.VERIFYING)]
    waiting = [i for i in items if i.state in (State.NEEDS_YOU, State.PARKED)]
    queued = [i for i in items if i.state is State.QUEUED]

    decisions = [
        e
        for e in led.events(limit=2000)
        if e["kind"] == "judged"
        and e["ts"] >= since
        and Verdict.NEEDS_DECISION.value in (e["detail"] or "")
    ]

    reg = Register.read(project)
    lines: list[str] = []

    if shipped:
        lines.append(f"Shipped ({len(shipped)}): " + ", ".join(i.session for i in shipped))
    else:
        lines.append("Shipped: nothing today.")

    if running:
        lines.append(
            "Still running: " + ", ".join(f"{i.session} on {i.runner or '?'}" for i in running)
        )

    if waiting:
        lines.append("")
        lines.append(f"Needs you ({len(waiting)}):")
        for i in waiting:
            why = i.verdict or i.state.value
            note = (i.note or "").strip().split("\n")[0][:110]
            lines.append(f"  {i.session} — {why}. {note}")

    if queued:
        lines.append("")
        lines.append(
            f"Queued for tomorrow ({len(queued)}): " + ", ".join(i.session for i in queued)
        )

    if decisions:
        lines.append("")
        lines.append(f"Decided for you ({len(decisions)}):")
        for e in decisions:
            lines.append(f"  {e['session']} — {e['detail'][:150]}")

    lines.append("")
    if reg.open_rows >= 0:
        lines.append(
            f"Register: {reg.open_rows} findings open, "
            f"{reg.unwalked} passes never walked. All of it is live on "
            f"the production site."
        )
    return "\n".join(lines)


def urgent(led: Ledger, since: float) -> list[tuple[str, str]]:
    """The only two verdicts that interrupt him, as (session, one line)."""
    out: list[tuple[str, str]] = []
    wanted = {v.value for v in WHATSAPP_IMMEDIATELY}
    for i in led.all():
        if i.state is not State.NEEDS_YOU or not i.verdict:
            continue
        if i.verdict not in wanted or (i.ended_at or 0) < since:
            continue
        note = (i.note or "").strip().split("\n")[0][:160]
        out.append((i.session, f"{i.session} — {i.verdict}. {note}"))
    return out
