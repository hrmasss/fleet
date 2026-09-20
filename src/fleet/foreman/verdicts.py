"""The verdict vocabulary, and the single action each one maps to.

This is the contract between the foreman and the queue. The foreman is an LLM and it
returns one of these names and nothing else; a free-text opinion cannot drive a state
machine, and the moment a verdict means "it depends" the queue has to start guessing.

Every verdict here is a failure that actually happened on this project between
2026-09-15 and 2026-09-19. None of them is speculative, and none of them names a runner.

The `authority` column is what makes the loop safe to leave running. Six verdicts resolve
without a human. Two always reach the operator. One is gated on the foreman's autonomy rung,
because answering a question on their behalf is the last thing to trust it with.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Authority(StrEnum):
    """Who is allowed to act on a verdict."""

    AUTO = "auto"
    """The queue acts on it unattended."""

    GATED = "gated"
    """Automatic only at foreman autonomy rung 3, and every answer is cited and logged."""

    HUMAN = "human"
    """Always reaches the operator. The queue parks the session and does not retry."""


class Action(StrEnum):
    """What the queue does. One per verdict, no branching on anything else."""

    CLOSE = "close"
    RELAUNCH = "relaunch"
    REDEPLOY = "redeploy"
    REBUILD = "rebuild"
    REQUEUE = "requeue"
    PARK_ACCOUNT = "park_account"
    ANSWER_AND_RELAUNCH = "answer_and_relaunch"
    ESCALATE = "escalate"


class Verdict(StrEnum):
    SHIPPED = "shipped"
    MERGED_NOT_DEPLOYED = "merged-not-deployed"
    BUILD_CANCELLED = "build-cancelled"
    INCOMPLETE = "incomplete"
    QUOTA_EXHAUSTED = "quota-exhausted"
    STALLED = "stalled"
    NEEDS_DECISION = "needs-decision"
    DIVERGED = "diverged"
    BLOCKED_EXTERNAL = "blocked-external"


@dataclass(frozen=True, slots=True)
class Rule:
    action: Action
    authority: Authority
    saw: str
    """What the evidence looked like. Goes into the ledger verbatim so a verdict can be
    argued with later."""


RULES: dict[Verdict, Rule] = {
    Verdict.SHIPPED: Rule(
        Action.CLOSE,
        Authority.AUTO,
        "merged, built, running tag matches, and every criterion carries evidence",
    ),
    Verdict.MERGED_NOT_DEPLOYED: Rule(
        Action.REDEPLOY,
        Authority.AUTO,
        "PR merged but the running production image tag does not contain the merge sha",
    ),
    Verdict.BUILD_CANCELLED: Rule(
        Action.REBUILD,
        Authority.AUTO,
        "the images run was cancelled by the next merge, so the tag moved forward over "
        "stale code and the deploy reported success anyway",
    ),
    Verdict.INCOMPLETE: Rule(
        Action.RELAUNCH,
        Authority.AUTO,
        "acceptance criteria unticked, or ticked with nothing behind them",
    ),
    Verdict.QUOTA_EXHAUSTED: Rule(
        Action.PARK_ACCOUNT,
        Authority.AUTO,
        "the transcript carries a provider limit error",
    ),
    Verdict.STALLED: Rule(
        Action.REQUEUE,
        Authority.AUTO,
        "idle past the threshold with no question asked and no progress on disk",
    ),
    Verdict.NEEDS_DECISION: Rule(
        Action.ANSWER_AND_RELAUNCH,
        Authority.GATED,
        "the session asked something the brief or the spec already settles",
    ),
    Verdict.DIVERGED: Rule(
        Action.ESCALATE,
        Authority.HUMAN,
        "it shipped something other than what was asked",
    ),
    Verdict.BLOCKED_EXTERNAL: Rule(
        Action.ESCALATE,
        Authority.HUMAN,
        "it needs a credential, a client answer, or a decision no document contains",
    ),
}

WHATSAPP_IMMEDIATELY: frozenset[Verdict] = frozenset({Verdict.DIVERGED, Verdict.BLOCKED_EXTERNAL})
"""The only two verdicts that interrupt the operator. Everything else waits for the digest.

Sessions do not message him at all any more — that line comes out of the brief template for
every runner. Thirty sessions each writing to one thread, none of them knowing what the
others said, is why the thread stopped being read.
"""


def rule(verdict: Verdict) -> Rule:
    return RULES[verdict]
