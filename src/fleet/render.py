"""Turn an agent's NDJSON event stream into something a person can read.

⚠ This exists because the two obvious ways to run agy are both unwatchable, which three
rounds of guessing on 2026-09-21 established and no amount of reading would have.

`-i` never draws its interface in a detached tmux pane. Not a fleet problem: two panes
dispatched by hand, with no wrapper and no pipe anywhere near them, were equally blank at
the bottom. Attaching gives you scrollback and no prompt.

`-p` exits cleanly, which is what the slot accounting needs, but writes **nothing at all**
until the turn ends. Measured: zero bytes for ninety seconds, then 312 at once. That makes
the pane empty, the log empty, and the liveness text signal blind for the whole run.

`--output-format stream-json` is the one mode that both exits and speaks while it works —
1.4 KB by ten seconds, growing steadily. It is NDJSON, so it needs rendering, and that is
all this module does. It sits in the wrapper pipeline ahead of `tee`, so the pane, the log
and the liveness signal all get the same readable text.

⚠ It never raises and never swallows. A line it cannot parse is passed through unchanged,
because a renderer that eats the output when the format shifts is worse than no renderer.
"""

from __future__ import annotations

import json
import sys
from typing import IO

MAX_PARAM = 100
"""How much of a tool's argument to show. Enough to tell two `git grep`s apart."""

PREFERRED = ("CommandLine", "command", "path", "file_path", "query", "pattern", "url")
"""Parameter names worth printing, best first. A tool call is identified by its argument,
and `run_command` calling itself `run_command` says nothing."""


def _clip(text: str, limit: int = MAX_PARAM) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _argument(info: dict) -> str:
    params = info.get("parameters")
    if not isinstance(params, dict) or not params:
        return ""
    for key in PREFERRED:
        if params.get(key):
            return _clip(params[key])
    return _clip(", ".join(f"{k}={v}" for k, v in params.items()))


def line_for(event: dict) -> str | None:
    """One readable line, or None for an event not worth a line of its own.

    Text deltas are the exception: they are fragments of one sentence, so they are
    returned without a newline and joined by the caller.
    """
    kind = event.get("event")

    if kind == "init":
        cwd = (event.get("init") or {}).get("cwd") or ""
        return f"── {cwd}\n"

    if kind == "result":
        r = event.get("result") or {}
        status = r.get("status", "?")
        secs = r.get("duration_seconds")
        took = f" in {secs:.0f}s" if isinstance(secs, (int, float)) else ""
        return f"── {status}{took}\n"

    if kind != "step_update":
        return None

    step = event.get("step_update") or {}
    state, step_type = step.get("state"), step.get("step_type")

    # ⚠ ACTIVE only. Every step arrives twice, and printing DONE as well doubles the
    # transcript and puts each tool call on screen after it has already finished.
    if step_type == "tool" and state == "ACTIVE":
        name = step.get("tool_name") or "tool"
        arg = _argument(step.get("tool_info") or {})
        return f"● {name}({arg})\n" if arg else f"● {name}\n"

    if step_type == "agent_response" and state == "ACTIVE":
        return step.get("text_delta") or None

    return None


def render(source: IO[str], out: IO[str]) -> int:
    """Read NDJSON from `source`, write readable text to `out`, return a exit code.

    ⚠ Flushed every line. The whole point is that the pane fills while the agent works,
    and Python's default block buffering on a pipe would hold it all back to the end —
    which is the exact failure this replaces.

    ⚠ It tracks whether the last write finished its line, because prose arrives as
    fragments that rarely end in a newline. Without that, the next tool call lands on the
    end of the last sentence: `Completed listing.● run_command(date -u)`.
    """
    at_line_start = True

    def emit(text: str, structural: bool) -> None:
        nonlocal at_line_start
        if structural and not at_line_start:
            out.write("\n")
            at_line_start = True
        out.write(text)
        at_line_start = text.endswith("\n")
        out.flush()

    for raw in source:
        stripped = raw.strip()
        if not stripped:
            continue
        event = None
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            event = parsed if isinstance(parsed, dict) else None
        if event is None:
            # Not ours: agya's rotation notice, a traceback, anything at all. Passed
            # through rather than dropped.
            emit(raw if raw.endswith("\n") else raw + "\n", structural=True)
            continue
        text = line_for(event)
        if text:
            emit(text, structural=text.endswith("\n"))
    return 0


def main() -> int:
    return render(sys.stdin, sys.stdout)
