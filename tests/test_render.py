"""The renderer, which is the only reason a dispatched pane shows anything at all.

⚠ Every number in these docstrings was measured on hapl-aux on 2026-09-21, not reasoned
about. Three rounds of guessing at why attach was unusable produced three wrong answers —
the tee, the pane size, the resize — and the measurement produced the right one.
"""

from __future__ import annotations

import io
import json

from fleet.render import line_for, render


def ev(**kw):
    return json.dumps(kw)


def tool(state, name="run_command", **params):
    return ev(
        event="step_update",
        step_update={
            "state": state,
            "step_type": "tool",
            "tool_name": name,
            "tool_info": {"name": name, "parameters": params},
        },
    )


def test_a_tool_call_is_named_by_its_argument():
    """`run_command` calling itself run_command says nothing; the command line is the news."""
    out = line_for(json.loads(tool("ACTIVE", CommandLine="git -C code grep -n useCreateRedirect")))
    assert out == "● run_command(git -C code grep -n useCreateRedirect)\n"


def test_each_step_is_printed_once_not_twice():
    """⚠ Every step arrives as ACTIVE and again as DONE. Rendering both doubles the whole
    transcript and puts each tool call on screen only after it has already finished."""
    assert line_for(json.loads(tool("ACTIVE", CommandLine="ls"))) is not None
    assert line_for(json.loads(tool("DONE", CommandLine="ls"))) is None


def test_prose_arrives_in_fragments_and_is_not_broken_into_lines():
    deltas = ["### Step 1\nRan ", "`ls`", " and it worked"]
    src = io.StringIO(
        "\n".join(
            ev(
                event="step_update",
                step_update={"state": "ACTIVE", "step_type": "agent_response", "text_delta": d},
            )
            for d in deltas
        )
    )
    out = io.StringIO()
    render(src, out)
    assert out.getvalue() == "### Step 1\nRan `ls` and it worked"


def test_a_line_it_cannot_parse_survives():
    """⚠ agya announces the account it picked on stderr, and a traceback is not JSON. A
    renderer that eats what it does not recognise is worse than no renderer."""
    src = io.StringIO("agya: rotation picked 'a4'\n{not json at all}\n" + tool("ACTIVE", path="x"))
    out = io.StringIO()
    render(src, out)
    text = out.getvalue()
    assert "agya: rotation picked 'a4'" in text
    assert "{not json at all}" in text
    assert "● run_command(x)" in text


def test_nothing_is_held_back_until_the_end():
    """⚠ The entire point. Plain `-p` wrote zero bytes for ninety seconds and then 312 at
    once, which is what made the pane and the log useless. Block buffering on a pipe would
    reproduce that exactly, so every line is flushed."""

    class Recorder(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushes = 0

        def flush(self):
            self.flushes += 1

    out = Recorder()
    render(io.StringIO("\n".join(tool("ACTIVE", path=f"f{i}") for i in range(5))), out)
    assert out.flushes >= 5, "a line written and not flushed is a line nobody sees"


def test_the_result_says_how_it_ended():
    line = line_for(
        json.loads(ev(event="result", result={"status": "SUCCESS", "duration_seconds": 48.2}))
    )
    assert line is not None and "SUCCESS" in line and "48s" in line


def test_the_filter_sits_between_the_agent_and_the_tee(tmp_path):
    """⚠ Not inside runner_cmd. There, `2>&1` would bind to the filter instead of the
    agent, and the agent's stderr would reach the pane but never the log."""
    from fleet.adapters.shell import write_wrapper

    script = tmp_path / "run.sh"
    write_wrapper("ux-1", "agya a4 -p x", str(tmp_path / "ux-1.log"), str(script), env={})
    plain = script.read_text(encoding="utf-8")
    assert "agya a4 -p x 2>&1 | tee" in plain

    write_wrapper(
        "ux-1",
        "agya a4 -p x",
        str(tmp_path / "ux-1.log"),
        str(script),
        env={},
        filter_cmd="fleet render",
    )
    piped = script.read_text(encoding="utf-8")
    assert "agya a4 -p x 2>&1 | fleet render | tee" in piped
    assert "rc=${PIPESTATUS[0]}" in piped, "the agent is still the first element"


def test_a_tool_call_does_not_land_on_the_end_of_the_last_sentence():
    """⚠ Observed in the first live run: `Completed listing.● run_command(date -u)`.
    Prose arrives as fragments that rarely end in a newline, so a structural line has to
    close the one before it."""
    src = io.StringIO(
        "\n".join(
            [
                ev(
                    event="step_update",
                    step_update={
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "Completed listing.",
                    },
                ),
                tool("ACTIVE", CommandLine="date -u"),
            ]
        )
    )
    out = io.StringIO()
    render(src, out)
    assert out.getvalue() == "Completed listing.\n● run_command(date -u)\n"
