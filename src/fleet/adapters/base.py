"""The runner contract.

Everything above this file is runner-agnostic. The queue counts free slots and hands a
session to an adapter; which binary that adapter starts is its own business. Nothing outside
this package may branch on `agy` or `claude` or `cursor` — if it does, the coupling this
design exists to remove has come back.

Four methods, because that is what dispatch actually needs:

    launch    start the work, return a handle we can find again
    capacity  how many more this runner can take right now, and on which account
    liveness  working, idle, or gone
    nudge     put a continuation into a session that is still alive

`liveness` is the one with a shared implementation. ccmux installs hooks into all three
agents today and every one of them writes the same marker shape into a single directory:
`agent_type`, `pid`, `session_id`, `state`, `state_timestamp`. So an adapter gets liveness
for free as long as ccmux knows about its agent.

⚠ The marker is an optimisation, never a dependency. As of 2026-09-19 eight agy processes
were live on the box and not one had ever written a marker, while claude and cursor wrote
theirs correctly — all three agy profiles declare the hooks and point at scripts that exist,
and the cause has not been chased. So `liveness` must always be able to answer from pid
liveness plus the tee log's mtime alone, which is how stalls were judged by hand before any
of this existed.

`nudge` may legitimately fail. A runner that cannot take a continuation returns False rather
than pretending, and the queue relaunches instead. Do not paper over this: a nudge that
silently does nothing is how a session sits idle for an hour looking busy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from fleet.adapters.quota import Bucket


class State(StrEnum):
    """What a launched session is doing, as far as we can tell from outside it."""

    WORKING = "working"
    IDLE = "idle"
    GONE = "gone"


@dataclass(frozen=True, slots=True)
class Handle:
    """Everything needed to find a launched session again after we stop watching it.

    `account` is the runner's own notion of which credential is in use — an agy profile
    name, or None for a runner with only one. `log` is the tee'd transcript, which is the
    liveness fallback when no marker turns up.
    """

    runner: str
    session: str
    pid: int
    account: str | None = None
    tmux: str | None = None
    log: str | None = None


@dataclass(frozen=True, slots=True)
class Capacity:
    """How many concurrent sessions each account will take right now.

    This is a ceiling, not a count of free slots: the adapter knows about quota and
    parking, the ledger knows what is already running, and only the scheduler knows both.
    An adapter that tried to report free slots would have to read the ledger, which is the
    coupling the protocol exists to avoid.

    Zero means the account is parked, spent, or sick. `detail` carries a line per account
    for the board, and `reason` explains a runner that can take nothing at all, so a parked
    runner says so rather than silently looking busy.
    """

    runner: str
    per_account: dict[str, int]
    detail: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    quota: dict[str, list[Bucket]] = field(default_factory=dict)
    """What each account has left, normalised across runners. Empty for a runner that
    exposes nothing — `reason` says why rather than showing an empty tank as a full one."""

    observed: dict[str, int] = field(default_factory=dict)
    """Sessions actually running on each account right now, counted from the OS.

    ⚠ fleet is not the only thing that starts agents on this box. On 2026-09-19 eight agy
    sessions were live that fleet had never heard of, dispatched by hand and by Hermes. A
    scheduler that counts only its own would have stacked its whole ceiling on top of them.

    The scheduler takes `max(observed, what the ledger says fleet started)`, which counts
    fleet's own sessions once and everybody else's as well.
    """

    @property
    def ceiling(self) -> int:
        return sum(self.per_account.values())


@runtime_checkable
class Adapter(Protocol):
    """One implementation per agent binary. Register it in `adapters/__init__.py`."""

    name: str

    auto: bool
    """May the queue send this runner a session that did not ask for it?

    agy is the default and the only one that auto-claims. claude and cursor run on request:
    a session reaches them only by naming one, and an unnamed session waits for agy however
    long agy is busy. That is a deliberate asymmetry, not a capacity decision. The three
    accounts behind agy exist to be spent; the claude and cursor allowances are the ones
    the operator also works in, and a queue that drains them has taken their own tools away.

    ⚠ This lives on the adapter so the scheduler never has to know which runner is which.
    A filter on the runner's name in `_pick` would be the same rule and the wrong place.
    """

    def capacity(self, model: str | None = None) -> Capacity:
        """Free slots right now, from the runner's own quota, never from a cached guess.

        `model` is not a preference, it is which allowance the work would spend. agy
        meters Gemini and the Claude/GPT models as two separate pools per account, both
        full-sized, so an account with an empty Gemini weekly can still take a Claude
        session. The ceiling therefore depends on what is about to run, and the mapping
        from a model id to a pool is the adapter's business — nothing above this file
        knows that `claude-sonnet-4-6` is not a Gemini model.

        None means the runner's default model.
        """
        ...

    def launch(self, session: str, brief: str, workspace: str, model: str | None = None) -> Handle:
        """Start the work detached and return immediately. Raise if it did not start.

        ⚠ A launch that produces a process but never a prompt is not a launch. Two agy
        sessions died that way on account a3 on 2026-09-19, CPU frozen at 28 seconds with
        the input box never drawn, and a dispatcher that cannot tell that from a slow start
        will feed a sick account forever. Implementations wait for a readiness signal and
        raise on timeout.
        """
        ...

    def liveness(self, handle: Handle, record: bool = True) -> State:
        """Marker if present and fresh, else pid liveness plus log mtime.

        `record=False` is for a caller that is only looking, such as the board. Recording
        moves the CPU baseline, and a five-second poll doing that would quietly replace the
        watchdog's thirty-minute window with a five-second one.
        """
        ...

    def nudge(self, handle: Handle, text: str) -> bool:
        """Deliver a continuation. False means this runner cannot, so relaunch instead."""
        ...
