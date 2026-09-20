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
    monkeypatch.setattr(runners, "count_matching", lambda lines, needle: 0)
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
    monkeypatch.setattr(runners, "count_matching", lambda lines, needle: 0)
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
    monkeypatch.setattr(runners, "count_matching", lambda lines, needle: 0)
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
