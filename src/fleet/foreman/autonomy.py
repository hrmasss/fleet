"""How much the foreman is allowed to act on its own verdicts.

⚠ Autonomy is earned, not configured. The foreman ships at `observe`: it judges every
session and writes the verdict to the ledger and the board, and acts on nothing. It moves
up only when a week of its verdicts agrees with the operator's own reading.

`fleet foreman --autonomy` refuses over SSH, which is enforced in `cli.py`. A session
asking for more trust is not evidence that it deserves more.
"""

from __future__ import annotations

from enum import StrEnum

from fleet.foreman.verdicts import RULES, Authority, Verdict
from fleet.paths import STATE


class Rung(StrEnum):
    OFF = "off"
    """No foreman at all. The gate decides what it can and everything else reaches the operator."""

    OBSERVE = "observe"
    """Judge everything, record it, act on nothing. Where it ships."""

    ACT = "act"
    """Act on the mechanical verdicts. Decisions and divergence still escalate."""

    DECIDE = "decide"
    """Also answer questions the spec settles, every answer cited and logged. Last."""


DEFAULT = Rung.OBSERVE


def path():
    return STATE / "autonomy"


def read() -> Rung:
    try:
        return Rung(path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return DEFAULT


def write(rung: Rung) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(rung.value, encoding="utf-8")


def may_act(rung: Rung, verdict: Verdict) -> bool:
    """Whether the queue may act on this verdict at this rung, without a human.

    `human` authority never becomes automatic at any rung. That is the floor: divergence
    and a genuine external block are the operator's, and no dial changes it.
    """
    authority = RULES[verdict].authority
    if authority is Authority.HUMAN:
        return False
    if rung is Rung.DECIDE:
        return True
    if rung is Rung.ACT:
        return authority is Authority.AUTO
    return False
