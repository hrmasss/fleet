"""The foreman. Called like a function, returns one verdict, exits.

It never holds the loop. The queue calls it with an evidence packet, gets a verdict back,
and the process is gone. It owns no state, so a crash mid-judgement costs a retry and
nothing else. An earlier design where an LLM carried queue state in its context died twice
on session restart and took the queue with it.

It gets no tools. `claude -p` with a deny list, one turn, the whole packet inlined. A judge
that can go and look for more is a judge that can be talked into something, and the packet
is deliberately everything it is allowed to know.

Model routing is two-tier for one reason: `diverged` and `needs-decision` are the two
verdicts that either reach the operator or act on their behalf, so those get a second opinion from
a stronger model. Everything else is a cheap classification of mechanical facts.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from fleet.foreman.packet import Packet
from fleet.foreman.verdicts import RULES, Verdict

SYSTEM = """\
You are the foreman of an automated dispatch queue. You judge whether a coding agent's
session actually finished its work. You are a substitute for one person's judgement, not an
engineer.

You MUST answer with a single JSON object and nothing else:

  {"verdict": "<one of the verdicts>", "note": "<one or two sentences>",
   "citation": "<source, only for needs-decision>", "answer": "<only for needs-decision>"}

The verdicts, and what each one means:

%s

Rules you do not break:

1. A ticked acceptance criterion is not evidence. The section marked CLAIMS is the
   session's own account of itself and it has been wrong repeatedly. Judge from the section
   marked EVIDENCE, which is mechanical reads of GitHub, production and git.
2. You never write code, never open or merge a pull request, never deploy.
3. `needs-decision` means the session asked something the brief or the project's documents
   already settle. You may answer it ONLY by quoting the source in `citation`. If you
   cannot cite a source, the verdict is `blocked-external` instead. Never invent a product
   decision.
4. `diverged` means it shipped something other than what was asked. Use it when the gap
   cannot be described as "you did not finish X" — that one is `incomplete`.
5. If the evidence does not support any verdict confidently, answer `blocked-external` and
   say what a human needs to look at. Guessing is worse than escalating.
6. A check marked `pass` in EVIDENCE is a settled fact. Do not re-derive it and do not
   overturn it. Your job is only what the gate could not decide. In particular, production
   running a *later* commit than the merge is still deployed, because a later deploy
   carries the earlier commit — the gate has already tested that, so never read a tag that
   differs from the merge sha as a failure.
7. `note` is for a human to read. Say what you saw, not what you inferred.
"""


def system_prompt() -> str:
    lines = [f"  {v.value}: {RULES[v].saw}" for v in Verdict]
    return SYSTEM % "\n".join(lines)


SECOND_OPINION: frozenset[Verdict] = frozenset({Verdict.DIVERGED, Verdict.NEEDS_DECISION})
"""Verdicts worth paying a stronger model to confirm: one reaches the operator, the other acts
on their behalf."""

DENIED = (
    "Bash",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
)
"""The foreman reads a packet and answers. It does not go looking."""


@dataclass(frozen=True, slots=True)
class Judgement:
    verdict: Verdict | None
    note: str
    citation: str | None = None
    answer: str | None = None
    model: str = ""
    raw: str = ""

    @property
    def usable(self) -> bool:
        return self.verdict is not None


Runner = Callable[[str, str, str], tuple[int, str]]
"""(model, system, prompt) -> (returncode, stdout). Injected so tests need no model."""


def claude_runner(model: str, system: str, prompt: str, timeout: int = 300) -> tuple[int, str]:
    """One call, no tools, packet on stdin.

    ⚠ The packet goes in on stdin, not as an argument, for two reasons found live.
    `--disallowed-tools` is variadic, so a positional prompt after it is swallowed as one
    more tool name and claude then refuses with "Input must be provided". And a packet
    carries a whole session file, which is exactly the size that starts running into argv
    limits — the command line was never the right place for it.
    """
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--system-prompt",
        system,
        "--disallowed-tools",
        *DENIED,
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 127, str(e)
    return r.returncode, r.stdout or r.stderr


JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse(raw: str) -> Judgement:
    """Strictly. An answer we cannot read is not a verdict, and pretending otherwise is how
    a state machine ends up acting on a sentence."""
    text = raw.strip()
    try:
        outer = json.loads(text)
        if isinstance(outer, dict) and "result" in outer:
            text = str(outer["result"])
    except json.JSONDecodeError:
        pass

    m = JSON_BLOCK.search(text)
    if not m:
        return Judgement(None, f"foreman did not answer with JSON: {text[:200]}", raw=raw)
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return Judgement(None, f"foreman's JSON did not parse: {e}", raw=raw)

    name = str(d.get("verdict", "")).strip()
    try:
        verdict = Verdict(name)
    except ValueError:
        return Judgement(None, f"foreman returned an unknown verdict {name!r}", raw=raw)

    note = str(d.get("note") or "").strip()
    citation = (str(d["citation"]).strip() or None) if d.get("citation") else None
    answer = (str(d["answer"]).strip() or None) if d.get("answer") else None

    if verdict is Verdict.NEEDS_DECISION and not citation:
        # Rule 3 exists because a foreman that answers uncited is a foreman inventing
        # product decisions. Downgrade rather than trust it.
        return Judgement(
            Verdict.BLOCKED_EXTERNAL,
            f"foreman wanted to decide this but cited nothing: {note}",
            raw=raw,
        )

    return Judgement(verdict, note, citation, answer, raw=raw)


class Foreman:
    def __init__(
        self, model: str = "sonnet", strong_model: str = "opus", runner: Runner | None = None
    ) -> None:
        self.model = model
        self.strong_model = strong_model
        self.runner = runner or claude_runner

    def judge(self, packet: Packet) -> Judgement:
        system = system_prompt()
        rendered = packet.render()

        rc, out = self.runner(self.model, system, rendered)
        if rc != 0:
            return Judgement(None, f"foreman call failed (rc={rc}): {out[:200]}", raw=out)
        first = parse(out)
        first = Judgement(
            first.verdict, first.note, first.citation, first.answer, model=self.model, raw=first.raw
        )

        if first.verdict not in SECOND_OPINION:
            return first

        rc, out = self.runner(self.strong_model, system, rendered)
        if rc != 0:
            # The cheap answer stands, but say the confirmation never happened: a verdict
            # that reaches the operator should not quietly carry less weight than it looks like.
            return Judgement(
                first.verdict,
                f"{first.note} (unconfirmed: {out[:120]})",
                first.citation,
                first.answer,
                model=self.model,
                raw=first.raw,
            )
        second = parse(out)
        if second.verdict is None:
            return first
        if second.verdict is not first.verdict:
            return Judgement(
                second.verdict,
                f"{second.note} (first pass on {self.model} said "
                f"{first.verdict.value if first.verdict else 'nothing'})",
                second.citation,
                second.answer,
                model=self.strong_model,
                raw=second.raw,
            )
        return Judgement(
            second.verdict,
            second.note,
            second.citation,
            second.answer,
            model=self.strong_model,
            raw=second.raw,
        )
