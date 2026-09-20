"""The evidence packet: everything the foreman is allowed to judge from.

⚠ **The session's own account of itself is not evidence.** It is in here, labelled as
claims, because knowing what a session believes is useful. But the executor's self-report
is the thing that has been wrong five times, and the packet is laid out so the two can
never be confused: `EVIDENCE` is what the world says, `CLAIMS` is what the session says.

Everything in the packet comes from a mechanical read — `gh`, `docker ps`, `git`, the log
on disk, the session file on disk. The foreman gets no tools and cannot go looking for
more, which is the point: a judge that can investigate is a judge that can be talked into
something.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fleet.gate import Result
from fleet.session import Session

LOG_TAIL_BYTES = 12000
"""Enough to carry a stack trace or a question, short enough that the packet stays cheap."""


@dataclass(frozen=True, slots=True)
class Packet:
    session: str
    brief: str
    claims: str
    evidence: str
    log_tail: str
    attempts: int
    previous_note: str | None

    def render(self) -> str:
        parts = [
            f"# Session {self.session}",
            f"\nThis is attempt {self.attempts}.",
        ]
        if self.previous_note:
            parts.append(
                "\n## What it was told last time\n\n"
                "It was relaunched with this instruction, so judge whether it did this:\n\n"
                f"{self.previous_note}"
            )
        parts += [
            "\n## The brief it was given\n\n" + self.brief,
            "\n## EVIDENCE — what the world says\n\n"
            "These are mechanical reads of GitHub, production and git. They are the only\n"
            "facts here.\n\n" + self.evidence,
            "\n## CLAIMS — what the session says about itself\n\n"
            "⚠ A ticked box proves nothing. This is the session's own account and it has\n"
            "been wrong before. Use it to understand intent, never as evidence.\n\n" + self.claims,
            "\n## The tail of its transcript\n\n```\n" + self.log_tail + "\n```",
        ]
        return "\n".join(parts)


def _tail(path: str | None) -> str:
    if not path:
        return "(no transcript recorded)"
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        return f"(transcript unreadable: {e})"
    if not data:
        return "(transcript is empty — for claude this is normal, print mode writes at the end)"
    return data[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")


def build(
    session: Session,
    result: Result,
    *,
    log: str | None,
    attempts: int,
    previous_note: str | None = None,
) -> Packet:
    claims = [f"status: {session.status}"]
    if session.criteria:
        claims.append("\nacceptance criteria as the session left them:")
        claims += [f"  [{'x' if c.ticked else ' '}] {c.text}" for c in session.criteria]
    else:
        claims.append("\n(no acceptance criteria found in the file)")

    evidence = [f"{c.name}: {c.mark} — {c.evidence}" for c in result.checks]
    evidence.append(
        f"\ngate verdict: {result.verdict.value if result.verdict else 'could not decide'}"
    )

    body = session.path.read_text(encoding="utf-8")
    return Packet(
        session=session.id,
        brief=body,
        claims="\n".join(claims),
        evidence="\n".join(evidence),
        log_tail=_tail(log),
        attempts=attempts,
        previous_note=previous_note,
    )
