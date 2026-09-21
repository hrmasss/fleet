"""Nothing implements Adapter yet. This pins the shape so phase 2 cannot drift."""

import inspect

import pytest

from fleet.adapters import registry, runners
from fleet.adapters.base import Adapter, Capacity, Handle, State
from fleet.adapters.quota import Bucket


def test_the_contract_is_four_methods():
    methods = {
        n for n, _ in inspect.getmembers(Adapter, inspect.isfunction) if not n.startswith("_")
    }
    assert methods == {"capacity", "launch", "liveness", "nudge"}


def test_liveness_can_only_say_three_things():
    assert {s.value for s in State} == {"working", "idle", "gone"}


def test_a_handle_survives_losing_the_marker():
    h = Handle(runner="agy", session="ux-124", pid=4242, account="a2", log="/tmp/ux-124.log")
    assert h.pid and h.log, "pid plus log mtime is the liveness fallback"


def test_capacity_explains_itself_when_full():
    c = Capacity(runner="agy", per_account={"a3": 0}, reason="parked until 16:20")
    assert c.ceiling == 0 and c.reason


def test_capacity_is_a_ceiling_not_a_count_of_free_slots():
    """Only the scheduler knows both the quota and what is already running."""
    c = Capacity(runner="agy", per_account={"main": 2, "a2": 2, "a3": 0})
    assert c.ceiling == 4


def test_every_adapter_says_whether_the_queue_may_reach_for_it():
    """⚠ A new adapter that forgets this inherits the permissive answer from `getattr`,
    which is a policy hole rather than a bug you would notice. Pin it here instead."""
    for name, adapter in registry().items():
        assert isinstance(getattr(adapter, "auto", None), bool), f"{name} does not declare auto"


def test_agy_is_the_default_and_the_others_are_on_request():
    r = registry()
    assert r["agy"].auto is True
    assert r["claude"].auto is False
    assert r["cursor"].auto is False


@pytest.mark.parametrize(
    ("five_hour_left", "expected"),
    [(0.9, 2), (0.26, 2), (0.24, 0), (0.0, 0), (None, 2)],
)
def test_claude_stops_dispatching_before_its_window_runs_out(monkeypatch, five_hour_left, expected):
    """⚠ 2026-09-19: this ceiling was a constant, and `limit-guard.py` was trusted to
    catch the edge. It cannot — it is a PreToolUse hook, so it gates a tool call inside a
    session that is already running and can refuse no launch at all. fleet started two
    sessions into a spent five-hour window; both met Anthropic's wall three seconds in,
    before any tool call existed to block, and both burned an attempt having done nothing.

    A window with no reading yet does not stop dispatch. The statusline only writes the
    five-hour figure once Claude Code has reported one, so absent means unknown, and
    refusing all work on an unknown would idle the runner for the first session of every day.
    """
    monkeypatch.setattr(runners, "sh", lambda *a, **k: (0, "2.0.0"))
    monkeypatch.setattr(runners, "process_lines", lambda: [])
    monkeypatch.setattr(runners, "count_matching", lambda lines, needle, exclude=(): 0)
    monkeypatch.setattr(
        runners.quota,
        "claude",
        lambda: [
            Bucket(id="five_hour", label="5 hour", remaining=five_hour_left),
            Bucket(id="seven_day", label="7 day", remaining=0.8),
        ],
    )
    cap = runners.Claude().capacity()
    assert cap.per_account["default"] == expected
    if expected == 0:
        assert cap.reason and "5 hour" in cap.reason, "a zero ceiling has to say why"


@pytest.mark.parametrize(
    ("weekly", "five_hour", "expected"),
    [(0.80, 1.0, 2), (0.11, 1.0, 2), (0.09, 1.0, 0), (0.0077, 1.0, 0), (1.0, 0.05, 0)],
)
def test_agy_counts_the_weekly_bucket_not_only_the_five_hour(
    monkeypatch, weekly, five_hour, expected
):
    """⚠ 2026-09-20: the floor read the five-hour bucket alone. All three profiles had a
    full five-hour window and between 0.77% and 19% of the weekly left, and four sessions
    sat in `quota reached ... retrying` loops for fifteen hours while fleet counted them as
    working. The five-hour window refills six times a working day; the weekly runs out."""
    monkeypatch.setattr(
        runners, "sh", lambda *a, **k: (0, "  main   last 2h ago    gemini-5h 100% weekly 3%")
    )
    monkeypatch.setattr(runners, "process_lines", lambda: [])
    monkeypatch.setattr(runners, "count_matching", lambda lines, needle, exclude=(): 0)
    monkeypatch.setattr(
        runners.quota,
        "agy",
        lambda d: [
            Bucket(id="gemini-weekly", label="Gemini weekly", remaining=weekly),
            Bucket(id="gemini-5h", label="Gemini 5h", remaining=five_hour),
            Bucket(id="3p-weekly", label="Claude/GPT weekly", remaining=1.0),
        ],
    )
    assert runners.Agy().capacity().per_account["main"] == expected


def test_each_agy_pool_is_read_on_its_own():
    """⚠ The two allowances are the same size and independent. Letting an empty Gemini
    weekly speak for the Claude one hides seven untouched accounts; letting a full Claude
    one speak for Gemini dispatches into a wall."""
    buckets = [
        Bucket(id="gemini-weekly", label="", remaining=0.01),
        Bucket(id="gemini-5h", label="", remaining=1.0),
        Bucket(id="3p-weekly", label="", remaining=1.0),
        Bucket(id="3p-5h", label="", remaining=1.0),
    ]
    assert runners.Agy._pool_floor(buckets, "gemini", 1.0) == 0.01
    assert runners.Agy._pool_floor(buckets, "3p", 1.0) == 1.0
    # No reading for that pool at all falls back to the status line's own figure.
    assert runners.Agy._pool_floor([], "3p", 0.42) == 0.42


@pytest.mark.parametrize(
    ("model", "pool"),
    [
        (None, "gemini"),
        ("gemini-3.8-flash-high", "gemini"),
        ("gemini-3.1-pro-high", "gemini"),
        ("claude-sonnet-4-6", "3p"),
        ("claude-opus-4-6-thinking", "3p"),
        ("gpt-oss-120b-medium", "3p"),
        ("something-google-ships-next-year", "3p"),
    ],
)
def test_the_model_prefix_decides_which_allowance_is_spent(model, pool):
    """⚠ Unrecognised counts as third-party, deliberately. A new Gemini model misread as
    third-party wastes a pool nothing is using; a new Claude model misread as Gemini
    dispatches against an allowance that is empty on three of the seven accounts."""
    assert runners.Agy.pool(model) == pool


@pytest.mark.parametrize(
    ("model", "expected"),
    [("gemini-3.8-flash-high", 0), ("claude-sonnet-4-6", 2), (None, 0)],
)
def test_an_account_out_of_gemini_can_still_take_a_claude_session(monkeypatch, model, expected):
    """The whole point of the two pools. a1 and a2 sat at 3% and 0.8% of their Gemini
    weekly on 2026-09-20 with their Claude allowance untouched at 100%."""
    monkeypatch.setattr(
        runners, "sh", lambda *a, **k: (0, "  a1   last 2h ago    gemini-5h 100% weekly 3%")
    )
    monkeypatch.setattr(runners, "process_lines", lambda: [])
    monkeypatch.setattr(runners, "count_matching", lambda lines, needle, exclude=(): 0)
    monkeypatch.setattr(
        runners.quota,
        "agy",
        lambda d: [
            Bucket(id="gemini-weekly", label="", remaining=0.03),
            Bucket(id="gemini-5h", label="", remaining=1.0),
            Bucket(id="3p-weekly", label="", remaining=1.0),
            Bucket(id="3p-5h", label="", remaining=1.0),
        ],
    )
    assert runners.Agy().capacity(model).per_account["a1"] == expected


def test_a1_still_answers_to_the_directory_it_had_as_main():
    """⚠ a1 was `main` at ~/.gemini until 2026-09-20. A session started before the rename
    keeps the old --gemini_dir on its command line for as long as it runs, so counting only
    the new path would drop it from the slot arithmetic and let fleet stack a whole ceiling
    on top of work already in flight. Nothing else gets a second path."""
    dirs = runners.Agy._dirs("a1")
    assert len(dirs) == 2
    assert dirs[0].endswith("/agy-accounts/a1")
    assert dirs[1].endswith("/.gemini")
    assert runners.Agy._dirs("a4") == [runners.Agy._dir("a4")]
    assert "main" not in runners.Agy._dir("a1")


# Real `ps -eo args=` output from the box on 2026-09-20: one working session from
# `claude agents`, with the three helper processes Claude Code puts beside it.
CLAUDE_PS = [
    "/home/user/.local/bin/claude daemon run --origin transient"
    ' --spawned-by {"label":"claude --bg"}',
    "claude bg-pty-host --bg-pty-host /tmp/cc-daemon/pty/x.sock 200 50 -- "
    "/home/user/.local/share/claude/versions/2.1.278 --session-id abc",
    "/home/user/.local/share/claude/versions/2.1.278 --session-id abc -n markup-configurable "
    "--dangerously-skip-permissions",
    "claude bg-spare --bg-spare /tmp/cc-daemon/spare/y.claim.sock",
    "/usr/bin/zsh -c source /home/user/.claude/shell-snapshots/snapshot.sh",
]


def test_claude_counts_a_session_it_did_not_start(monkeypatch):
    """⚠ This count was always zero. The old needle was `claude --dangerously-skip-permissions`,
    which is what you type and not what runs: `claude` is a shim that execs the versioned
    binary, so those two words never appear together on any command line. On 2026-09-20 a
    session from `claude agents` had been working 47 minutes with a PR open while fleet
    reported the runner idle."""
    monkeypatch.setattr(runners, "sh", lambda *a, **k: (0, "2.0.0"))
    monkeypatch.setattr(runners, "process_lines", lambda: CLAUDE_PS)
    monkeypatch.setattr(runners.quota, "claude", lambda: [])
    assert runners.Claude().capacity().observed["default"] == 1


def test_the_helpers_beside_a_claude_session_are_not_sessions():
    """A daemon, a pty host and a spare all carry the same install path. Counting the path
    alone turns one session into four and the ceiling shuts on nothing."""
    from fleet.adapters.shell import count_matching

    c = runners.Claude()
    assert count_matching(CLAUDE_PS, c.PROCESS) == 2, "the pty host carries the path too"
    assert count_matching(CLAUDE_PS, c.PROCESS, c.NOT_A_SESSION) == 1


# ── the parked pane ──────────────────────────────────────────────────────────────────
# ⚠ On 2026-09-21 five agy sessions on hapl-aux had been finished for between sixteen and
# nineteen hours, work merged and deployed, and the board showed all five as working. An
# agy pane left at its prompt emits control sequences forever: 450 bytes a minute, all of
# it escape codes and not one visible character, plus enough CPU to keep the kernel
# counter moving. That defeated both of liveness's fallbacks at once, so it could not
# return IDLE for them and the stall watchdog could never fire. These pin the fix.

PARKED = "\x1b[>4;2m" * 200
"""What a finished agy pane writes to its log, verbatim and forever."""


def test_a_log_that_only_redraws_has_not_said_anything():
    from fleet.adapters.shell import ANSI, visible_len

    assert ANSI.sub("", PARKED) == "", "private-parameter CSI is the form that was surviving"
    assert visible_len is not None


def test_visible_length_ignores_a_redraw_and_counts_a_sentence(tmp_path):
    from fleet.adapters.shell import visible_len

    log = tmp_path / "fa-09.1.log"
    log.write_text("● Read(todo/CLAUDE.md)\n" + PARKED, encoding="utf-8")
    before = visible_len(log)

    log.write_text(log.read_text(encoding="utf-8") + PARKED, encoding="utf-8")
    assert visible_len(log) == before, "1600 more bytes, nothing more said"

    log.write_text(log.read_text(encoding="utf-8") + "● Bash(git push)\n", encoding="utf-8")
    assert visible_len(log) > before


def test_a_parked_session_goes_idle_and_a_working_one_does_not(tmp_path, monkeypatch):
    """The whole bug, end to end: same file growth, opposite verdicts."""
    from fleet.adapters import shell

    monkeypatch.setattr(shell, "pid_alive", lambda pid: True)
    monkeypatch.setattr(shell, "marker_state", lambda *a: None)
    monkeypatch.setattr(shell, "cpu_ticks", lambda pid: 0)

    log = tmp_path / "s.log"
    log.write_text(PARKED, encoding="utf-8")
    h = Handle(runner="agy", session="s", pid=1, log=str(log))

    assert shell.liveness("antigravity", h, state_dir=tmp_path) is State.WORKING, "first look"

    log.write_text(log.read_text(encoding="utf-8") + PARKED, encoding="utf-8")
    assert shell.liveness("antigravity", h, state_dir=tmp_path) is State.IDLE

    log.write_text(log.read_text(encoding="utf-8") + "● Edit(app.py)\n", encoding="utf-8")
    assert shell.liveness("antigravity", h, state_dir=tmp_path) is State.WORKING


def test_cpu_alone_still_speaks_for_a_runner_that_prints_nothing(tmp_path, monkeypatch):
    """⚠ Claude Code in print mode writes to its log only at the very end, so the text
    signal is silent for its entire run and CPU is the only thing holding it alive. The
    redraw floor must not be applied to it."""
    from fleet.adapters import shell

    monkeypatch.setattr(shell, "pid_alive", lambda pid: True)
    monkeypatch.setattr(shell, "marker_state", lambda *a: None)

    log = tmp_path / "c.log"
    log.write_text("", encoding="utf-8")
    h = Handle(runner="claude", session="c", pid=1, log=str(log))

    ticks = [100]
    monkeypatch.setattr(shell, "cpu_ticks", lambda pid: ticks[0])
    shell.liveness("claude", h, state_dir=tmp_path)

    ticks[0] = 101
    assert shell.liveness("claude", h, state_dir=tmp_path) is State.WORKING
    ticks[0] = 101
    assert shell.liveness("claude", h, state_dir=tmp_path) is State.IDLE


def test_every_adapter_says_whether_its_log_is_a_terminal():
    """⚠ A new adapter that forgets this inherits `False` from `getattr` and its sessions
    become unreapable the moment it is launched interactively — which is not a visible
    bug, it is a watchdog that quietly cannot fire."""
    for name, adapter in registry().items():
        assert isinstance(getattr(adapter, "tui", None), bool), f"{name} does not declare tui"


def test_agy_exits_when_its_turn_ends():
    """`-i` holds the pane open after the work is done and the slot never comes back."""
    src = inspect.getsource(runners.Agy._start)
    assert '-p "$(cat' in src and "-i " not in src


def test_a_printing_runner_refuses_a_continuation():
    """base.py: a runner that cannot take one says so rather than pretending."""
    h = Handle(runner="agy", session="s", pid=1, tmux="s-a4")
    assert runners.Agy().nudge(h, "carry on") is False


# ── attaching to what was dispatched ─────────────────────────────────────────────────


def test_a_dispatched_pane_is_not_eighty_by_twenty_four(monkeypatch):
    """⚠ A detached new-session takes tmux's default-size, so every pane fleet started was
    80x24 and every log wrapped at eighty columns."""
    from fleet.adapters import shell

    calls = []

    def fake_sh(cmd, timeout=30):
        calls.append(cmd)
        return (0, "4242") if "list-panes" in cmd else (0, "")

    monkeypatch.setattr(shell, "sh", fake_sh)
    shell.tmux_spawn("ux-1-a4", "bash /tmp/run.sh")

    new = next(c for c in calls if "new-session" in c)
    w, h = shell.pane_size()
    assert ["-x", str(w), "-y", str(h)] == new[new.index("-x") : new.index("-x") + 4]


def test_only_a_terminal_pane_is_pinned_against_the_resize_on_attach(monkeypatch):
    """⚠ tmux defaults to window-size latest, so attaching resizes the pane and the agent
    gets a SIGWINCH. agy does not redraw on one — a live pane resized from 200x50 to
    120x30 reflowed nothing — so you attach to a frame drawn for a width that is gone."""
    from fleet.adapters import shell

    def run(fixed):
        calls = []

        def fake_sh(cmd, timeout=30):
            calls.append(cmd)
            return (0, "4242") if "list-panes" in cmd else (0, "")

        monkeypatch.setattr(shell, "sh", fake_sh)
        shell.tmux_spawn("ux-1-a4", "bash /tmp/run.sh", fixed=fixed)
        return any("window-size" in c for c in calls)

    assert run(True), "a TUI pane must not be resized under the agent"
    assert not run(False), "plain text has no frame to break; leave it tracking the client"


def test_the_runner_that_shows_a_terminal_is_the_one_that_gets_pinned():
    """Same flag drives both: what liveness must discount, and what attach must not resize."""
    pinned = {n for n, a in registry().items() if getattr(a, "tui", False)}
    assert pinned == {"cursor"}, "agy prints with -p now; claude was never interactive"
