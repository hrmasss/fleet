"""The tick, against a fake world.

The scheduler is the part that can do damage, so these tests are mostly about what it
refuses to do: dispatch while halted, relaunch past the cap, act on an unknown, or start a
session on an account that is already full.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fleet import scheduler
from fleet.adapters.base import Capacity, Handle
from fleet.adapters.base import State as Live
from fleet.gate import PullRequest, Run
from fleet.ledger import Ledger, State
from fleet.project import Project

SESSION_MD = """---
id: {sid}
status: {status}
prs: {prs}
---

# {sid}

## Acceptance criteria

{criteria}
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    sessions = tmp_path / "todo" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(scheduler, "KILL_FILE", tmp_path / "STOP")
    project = Project(
        repo="x/y",
        workspace=tmp_path,
        code_repo=tmp_path,
        prod_host="nowhere",
        prod_containers=("web",),
        build_workflow="images",
        deploy_workflow="deploy",
    )
    return project, sessions


def write_session(sessions: Path, sid: str, *, status="done", prs="[1]", criteria="- [x] one"):
    (sessions / f"{sid}-x.md").write_text(
        SESSION_MD.format(sid=sid, status=status, prs=prs, criteria=criteria),
        encoding="utf-8",
    )


class FakeAdapter:
    def __init__(self, name="agy", accounts=None, state=Live.WORKING, fail=False, auto=True):
        self.name = name
        self.auto = auto
        self.accounts = accounts if accounts is not None else {"main": 1}
        self.state = state
        self.fail = fail
        self.observed: dict[str, int] = {}
        self.launched: list[tuple[str, str]] = []
        self.models: list[str | None] = []
        self.asked: list[str | None] = []
        self.pools: dict[str | None, dict[str, int]] = {}
        self.nudges: list[str] = []

    def capacity(self, model=None):
        # A fake with two pools: anything the test puts in `pools` overrides the default
        # ceilings for that model, which is how the agy shape is exercised from here.
        accounts = self.pools.get(model, self.accounts)
        self.asked.append(model)
        return Capacity(self.name, dict(accounts), observed=dict(self.observed))

    def launch(self, session, brief, workspace, model=None):
        return self.launch_on(session, brief, workspace, next(iter(self.accounts)), model)

    def launch_on(self, session, brief, workspace, account, model=None):
        if self.fail:
            raise RuntimeError("no prompt drawn within timeout")
        self.launched.append((session, account))
        self.models.append(model)
        return Handle(
            self.name, session, 4242, account=account, tmux=f"{session}-{account}", log=None
        )

    def liveness(self, handle):
        return self.state

    def nudge(self, handle, text):
        self.nudges.append(text)
        return True


class FakeProbe:
    def __init__(self, *, state="MERGED", runs=("success",), ancestor=True):
        self.state, self.runs, self.ancestor = state, runs, ancestor

    def pull_request(self, number):
        return PullRequest(number, self.state, "a" * 40 if self.state == "MERGED" else None)

    def build_runs(self, sha):
        return [Run(conclusion=c) for c in self.runs]

    def running_tags(self):
        return {"web": "sha-aaaaaaa"}

    def resolve(self, short):
        return short + "0" * (40 - len(short))

    def is_ancestor(self, a, b):
        return self.ancestor


def build(world, adapters=None, probe=None):
    project, sessions = world
    led = Ledger(project.workspace / "ledger.db")
    sch = scheduler.Scheduler(
        project,
        led,
        adapters or {"agy": FakeAdapter()},
        probe=probe or FakeProbe(),
        workspace=str(project.workspace),
    )
    return led, sch, sessions


# --- dispatch ---------------------------------------------------------------------------


def test_a_queued_session_is_dispatched(world):
    led, sch, sessions = build(world)
    write_session(sessions, "ux-1")
    led.add("ux-1")
    out = sch.tick()
    assert out.dispatched == ["ux-1"]
    assert led.get("ux-1").state is State.RUNNING


def test_an_account_is_not_oversubscribed(world):
    """One slot, two sessions: the second waits."""
    led, sch, sessions = build(world, {"agy": FakeAdapter(accounts={"main": 1})})
    for sid in ("ux-1", "ux-2"):
        write_session(sessions, sid)
        led.add(sid)
    out = sch.tick()
    assert len(out.dispatched) == 1
    assert len(led.in_state(State.QUEUED)) == 1


def test_work_spreads_across_accounts_rather_than_filling_one(world):
    led, sch, sessions = build(world, {"agy": FakeAdapter(accounts={"main": 2, "a2": 2})})
    for sid in ("ux-1", "ux-2"):
        write_session(sessions, sid)
        led.add(sid)
    sch.tick()
    used = {a for _, a in sch.adapters["agy"].launched}
    assert used == {"main", "a2"}


def test_a_session_that_names_a_runner_waits_for_it(world):
    agy = FakeAdapter("agy", accounts={"main": 2})
    claude = FakeAdapter("claude", accounts={})
    led, sch, sessions = build(world, {"agy": agy, "claude": claude})
    write_session(sessions, "ux-1")
    led.add("ux-1", runner="claude")
    out = sch.tick()
    assert out.dispatched == []
    assert agy.launched == [], "it must not fall back to a runner it did not ask for"


def test_an_unnamed_session_never_lands_on_a_request_only_runner(world):
    """agy is the default and the only runner the queue reaches for on its own.

    ⚠ claude and cursor are the operator's own tools. A queue that spills onto them to save
    some waiting has taken their terminal away, so an unnamed session waits for agy instead.
    """
    agy = FakeAdapter("agy", accounts={"main": 0})
    claude = FakeAdapter("claude", accounts={"default": 2}, auto=False)
    led, sch, sessions = build(world, {"agy": agy, "claude": claude})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    assert sch.tick().dispatched == []
    assert claude.launched == [], "two free claude slots, and it still waits for agy"


def test_naming_a_request_only_runner_is_how_you_reach_it(world):
    agy = FakeAdapter("agy", accounts={"main": 2})
    claude = FakeAdapter("claude", accounts={"default": 1}, auto=False)
    led, sch, sessions = build(world, {"agy": agy, "claude": claude})
    write_session(sessions, "ux-1")
    led.add("ux-1", runner="claude")
    assert sch.tick().dispatched == ["ux-1"]
    assert claude.launched == [("ux-1", "default")]
    assert agy.launched == [], "naming one runner is not a preference, it is the whole set"


def test_a_requeued_request_only_session_stays_on_its_runner(world):
    """A continuation must not migrate. The session asked for claude once."""
    agy = FakeAdapter("agy", accounts={"main": 2})
    claude = FakeAdapter("claude", accounts={"default": 1}, auto=False)
    led, sch, sessions = build(world, {"agy": agy, "claude": claude})
    write_session(sessions, "ux-1")
    led.add("ux-1", runner="claude")
    sch.tick()
    led.requeue("ux-1", "not finished")
    claude.launched.clear()
    sch.tick()
    assert claude.launched == [("ux-1", "default")]
    assert agy.launched == []


def test_the_cap_gives_its_room_to_the_auto_runner_first(world, monkeypatch, tmp_path):
    """⚠ The trim used to go by size alone, so a cap could hand its whole remainder to a
    runner only a named session can use and leave the unnamed ones nowhere to go."""
    from fleet import paths

    monkeypatch.setattr(paths, "STATE", tmp_path)
    paths.write_cap(2)

    agy = FakeAdapter("agy", accounts={"main": 2})
    claude = FakeAdapter("claude", accounts={"default": 2}, auto=False)
    led, sch, sessions = build(world, {"agy": agy, "claude": claude})
    for sid in ("ux-1", "ux-2"):
        write_session(sessions, sid)
        led.add(sid)
    assert len(sch.tick().dispatched) == 2
    assert len(agy.launched) == 2, "the room went to the runner an unnamed session can use"


def test_where_a_session_ran_never_becomes_where_it_must_run(world):
    """⚠ `launched()` overwrites `runner` with whatever the session landed on. Reading
    dispatch policy off that field meant an unnamed session which once ran on claude was
    pinned to claude for every attempt after it — arriving there uninvited, then never
    leaving. The operator's request lives in `wanted` and nothing but `add` writes it."""
    agy = FakeAdapter("agy", accounts={"main": 2})
    claude = FakeAdapter("claude", accounts={"default": 2}, auto=False)
    led, sch, sessions = build(world, {"agy": agy, "claude": claude})
    write_session(sessions, "ux-1")
    led.add("ux-1")

    # However it got there — a hand dispatch, an older policy — it has run on claude.
    led.claim("ux-1")
    led.launched("ux-1", "claude", "default", 4242, "ux-1-claude", None)
    assert led.get("ux-1").runner == "claude"
    assert led.get("ux-1").wanted is None, "running somewhere is not asking for it"

    led.requeue("ux-1", "not finished")
    sch.tick()
    assert agy.launched == [("ux-1", "main")], "it goes back to the default runner"
    assert claude.launched == []


def test_a_ledger_written_before_wanted_existed_still_opens(world, tmp_path):
    """The ledger on the box predates the column. An old row means nobody asked for a
    runner, which is exactly what NULL says — copying `runner` across would pin every
    session that had ever touched claude."""
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE items (session TEXT PRIMARY KEY, state TEXT NOT NULL, runner TEXT, "
        "account TEXT, pid INTEGER, tmux TEXT, log TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
        "verdict TEXT, note TEXT, actor TEXT, queued_at REAL NOT NULL, started_at REAL, "
        "ended_at REAL);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "session TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);"
        "INSERT INTO items (session, state, runner, queued_at) "
        "VALUES ('fa-17', 'parked', 'claude', 1.0);"
    )
    db.commit()
    db.close()

    led = Ledger(path)
    it = led.get("fa-17")
    assert it.runner == "claude" and it.wanted is None
    led.close()


def test_an_empty_pool_does_not_close_an_account_to_the_other_one(world):
    """⚠ The two agy allowances are independent and the same size. On 2026-09-20 a1 and a2
    were at 3% and 0.8% of their Gemini weekly with their Claude pool untouched, so reading
    one ceiling for both would have idled seven accounts that had work left in them."""
    agy = FakeAdapter("agy", accounts={"a1": 0})
    agy.pools = {"claude-sonnet-4-6": {"a1": 2}, None: {"a1": 0}}
    led, sch, sessions = build(world, {"agy": agy})
    for sid in ("ux-1", "ux-2"):
        write_session(sessions, sid)
    led.add("ux-1")
    led.add("ux-2", model="claude-sonnet-4-6")

    assert sch.tick().dispatched == ["ux-2"], "the gemini one waits, the claude one goes"
    assert agy.models == ["claude-sonnet-4-6"], "the model reaches the runner"


def test_one_seat_is_not_handed_out_twice_across_pools(world):
    """A slot is one pane on one account. Which allowance it bills is a different question
    from whether it is occupied, so spending it has to register in every pool's view."""
    agy = FakeAdapter("agy", accounts={"a1": 1})
    agy.pools = {"claude-sonnet-4-6": {"a1": 1}, None: {"a1": 1}}
    led, sch, sessions = build(world, {"agy": agy})
    for sid in ("ux-1", "ux-2"):
        write_session(sessions, sid)
    led.add("ux-1", model="claude-sonnet-4-6")
    led.add("ux-2")

    assert len(sch.tick().dispatched) == 1, "one seat, one session, whatever it bills"


def test_capacity_is_read_once_per_distinct_model(world):
    """Not once per queued session. Each read shells out to agy-next and takes a second."""
    agy = FakeAdapter("agy", accounts={"a1": 4})
    led, sch, sessions = build(world, {"agy": agy})
    for sid, model in (("ux-1", None), ("ux-2", None), ("ux-3", "claude-sonnet-4-6")):
        write_session(sessions, sid)
        led.add(sid, model=model)
    sch.tick()
    assert sorted(m or "" for m in agy.asked) == ["", "claude-sonnet-4-6"]


def test_a_parked_account_takes_nothing(world):
    led, sch, sessions = build(world, {"agy": FakeAdapter(accounts={"a3": 0})})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    assert sch.tick().dispatched == []


def test_a_launch_that_never_draws_a_prompt_escalates(world):
    """Two agy sessions died this way on a3, CPU frozen with no input box."""
    led, sch, sessions = build(world, {"agy": FakeAdapter(fail=True)})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    out = sch.tick()
    assert out.escalated == ["ux-1"]
    assert led.get("ux-1").state is State.NEEDS_YOU


# --- the brake --------------------------------------------------------------------------


def test_the_kill_file_stops_dispatch(world):
    project, _ = world
    led, sch, sessions = build(world)
    write_session(sessions, "ux-1")
    led.add("ux-1")
    scheduler.KILL_FILE.write_text("outage", encoding="utf-8")
    out = sch.tick()
    assert out.halted == "outage"
    assert out.dispatched == []


def test_a_halted_queue_still_tells_the_truth_about_what_is_running(world):
    led, sch, sessions = build(world, {"agy": FakeAdapter(state=Live.GONE)})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    scheduler.KILL_FILE.write_text("paused", encoding="utf-8")
    sch.tick()
    assert led.get("ux-1").state is not State.RUNNING, "reconcile must still run"


# --- verify -----------------------------------------------------------------------------


def test_a_shipped_session_closes_and_frees_its_slot(world):
    led, sch, sessions = build(world, {"agy": FakeAdapter(state=Live.GONE)})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    out = sch.tick()
    assert out.closed == ["ux-1"]
    assert led.get("ux-1").state is State.DONE


def test_merged_but_not_deployed_is_relaunched_with_the_step_it_skipped(world):
    led, sch, sessions = build(
        world, {"agy": FakeAdapter(state=Live.GONE)}, probe=FakeProbe(ancestor=False)
    )
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    out = sch.tick(dispatch=False)
    assert out.relaunched == ["ux-1"]
    it = led.get("ux-1")
    assert it.state is State.QUEUED
    assert "deploy" in (it.note or "").lower()


def test_a_relaunch_is_capped(world):
    led, sch, sessions = build(
        world, {"agy": FakeAdapter(state=Live.GONE)}, probe=FakeProbe(ancestor=False)
    )
    write_session(sessions, "ux-1")
    led.add("ux-1")
    for _ in range(6):
        sch.tick()
    it = led.get("ux-1")
    assert it.state is State.PARKED
    assert it.attempts <= scheduler.MAX_ATTEMPTS + 1


def test_an_unknown_verdict_goes_to_him_not_to_a_redeploy(world):
    """A build still running is not a deploy failure."""
    led, sch, sessions = build(
        world,
        {"agy": FakeAdapter(state=Live.GONE)},
        probe=FakeProbe(runs=("",), ancestor=False),
    )
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    out = sch.tick(dispatch=False)
    assert out.escalated == ["ux-1"]
    assert led.get("ux-1").state is State.NEEDS_YOU


# --- the watchdog -----------------------------------------------------------------------


def test_a_session_idle_too_long_is_requeued(world):
    led, sch, sessions = build(world, {"agy": FakeAdapter(state=Live.IDLE)})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    led._db.execute(
        "UPDATE items SET started_at = ? WHERE session = 'ux-1'",
        (time.time() - scheduler.IDLE_SECONDS - 60,),
    )
    out = sch.tick(dispatch=False)
    assert out.stalled == ["ux-1"]
    assert led.get("ux-1").state is State.QUEUED


def test_a_session_idle_but_not_yet_stale_is_left_alone(world):
    led, sch, sessions = build(world, {"agy": FakeAdapter(state=Live.IDLE)})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    out = sch.tick(dispatch=False)
    assert out.stalled == []
    assert led.get("ux-1").state is State.RUNNING


# --- the ledger -------------------------------------------------------------------------


def test_adding_the_same_session_twice_is_refused(world):
    led, _, sessions = build(world)
    assert led.add("ux-1") is True
    assert led.add("ux-1") is False


def test_a_claim_can_only_be_won_once(world):
    led, _, _ = build(world)
    led.add("ux-1")
    assert led.claim("ux-1") is True
    assert led.claim("ux-1") is False


def test_requeue_clears_the_stale_handle(world):
    led, sch, sessions = build(world)
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    led.requeue("ux-1", "stalled")
    it = led.get("ux-1")
    assert it.pid is None and it.tmux is None, "a stale pid is worse than none"


def test_verifying_still_occupies_a_slot(world):
    """The gate may send it straight back, so its slot is not free yet."""
    led, sch, sessions = build(world, {"agy": FakeAdapter(accounts={"main": 1})})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    led.transition("ux-1", State.VERIFYING)
    assert led.running_by_account() == {("agy", "main"): 1}


def test_events_are_appended_for_every_transition(world):
    led, sch, sessions = build(world)
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    kinds = [e["kind"] for e in led.events()]
    assert "queued" in kinds and "launched" in kinds


# --- the wrapper, found broken on the box ------------------------------------------------


def test_fleet_env_picks_up_only_fleet_variables(monkeypatch):
    from fleet.adapters.shell import fleet_env

    monkeypatch.setenv("FLEET_STATE", "/s")
    monkeypatch.setenv("HOME_SOMETHING", "no")
    env = fleet_env()
    assert env.get("FLEET_STATE") == "/s"
    assert all(k.startswith("FLEET_") for k in env)


def test_the_wrapper_is_a_bash_script_not_a_shell_one_liner(tmp_path):
    """tmux starts commands with the login shell, which on the box is zsh, and zsh has no
    PIPESTATUS, it has $pipestatus. The exit code arrived empty, `fleet event` was called
    with no --rc, argparse rejected it, and the session looked like it never finished.
    """
    from fleet.adapters.shell import write_wrapper

    script = tmp_path / "ux-1.run.sh"
    cmd = write_wrapper("ux-1", "agya main -i x", "/tmp/a.log", str(script), env={})
    assert cmd.startswith("bash ") and script.name in cmd
    body = script.read_text(encoding="utf-8")
    assert body.startswith("#!/usr/bin/env bash")
    assert "PIPESTATUS" in body


def test_the_wrapper_carries_its_config_rather_than_inheriting_it(tmp_path):
    """A tmux session inherits the tmux SERVER's environment, not the spawning shell's."""
    from fleet.adapters.shell import write_wrapper

    script = tmp_path / "ux-1.run.sh"
    write_wrapper(
        "ux-1", "agya main -i x", "/tmp/a.log", str(script), env={"FLEET_STATE": "/tmp/fleet-probe"}
    )
    assert "export FLEET_STATE=/tmp/fleet-probe" in script.read_text(encoding="utf-8")


def test_the_wrapper_reports_then_ticks_in_that_order(tmp_path):
    from fleet.adapters.shell import write_wrapper

    script = tmp_path / "ux-1.run.sh"
    write_wrapper("ux-1", "run", "/tmp/a.log", str(script), env={})
    body = script.read_text(encoding="utf-8")
    assert body.index("event exited") < body.index("fleet tick"), (
        "a crash between them must lose a dispatch, not a fact"
    )


# --- fleet is not the only thing on the box ----------------------------------------------


def test_agents_fleet_did_not_launch_still_occupy_slots(world):
    """Eight agy sessions were live on 2026-09-19 that fleet had never heard of."""
    adapter = FakeAdapter(accounts={"main": 2})
    adapter.observed = {"main": 2}
    led, sch, sessions = build(world, {"agy": adapter})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    assert sch.free_slots() == {}
    assert sch.tick().dispatched == []


def test_fleets_own_sessions_are_not_counted_twice(world):
    """The OS sees fleet's own agent too, so busy is the max, never the sum.

    One launched against a ceiling of two leaves one slot. Summing would leave none.
    """
    adapter = FakeAdapter(accounts={"main": 2})
    led, sch, sessions = build(world, {"agy": adapter})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    adapter.observed = {"main": 1}
    assert sch.free_slots() == {("agy", "main"): 1}


def test_an_idle_marker_never_proves_a_session_is_idle(tmp_path, monkeypatch):
    """Claude Code's hooks only ever write `idle`, so a working claude marks itself idle.

    Observed live on 2026-09-19: the marker said idle while the process was demonstrably
    working, and the log was empty because piping stdout puts claude in print mode.
    """
    from fleet.adapters import shell
    from fleet.adapters.base import Handle
    from fleet.adapters.base import State as Live

    markers = tmp_path / "session-pids"
    markers.mkdir()
    (markers / "claude-abc.json").write_text(
        f'{{"agent_type":"claude","pid":4242,"state":"idle","state_timestamp":{int(time.time())}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(shell, "MARKERS", markers)
    monkeypatch.setattr(shell, "pid_alive", lambda pid: True)
    monkeypatch.setattr(shell, "cpu_advanced", lambda s, p, d, record=True: True)

    handle = Handle("claude", "ux-1", 4242, log=None)
    assert shell.liveness("claude", handle, state_dir=tmp_path) is Live.WORKING


def test_a_fresh_dispatch_is_never_reaped_for_want_of_a_baseline(tmp_path, monkeypatch):
    from fleet.adapters import shell
    from fleet.adapters.base import Handle
    from fleet.adapters.base import State as Live

    monkeypatch.setattr(shell, "MARKERS", tmp_path / "none")
    monkeypatch.setattr(shell, "pid_alive", lambda pid: True)
    monkeypatch.setattr(shell, "cpu_advanced", lambda s, p, d, record=True: None)
    handle = Handle("agy", "ux-1", 4242, log=None)
    assert shell.liveness("antigravity", handle, state_dir=tmp_path) is Live.WORKING


def test_every_signal_negative_is_idle(tmp_path, monkeypatch):
    from fleet.adapters import shell
    from fleet.adapters.base import Handle
    from fleet.adapters.base import State as Live

    monkeypatch.setattr(shell, "MARKERS", tmp_path / "none")
    monkeypatch.setattr(shell, "pid_alive", lambda pid: True)
    monkeypatch.setattr(shell, "cpu_advanced", lambda s, p, d, record=True: False)
    handle = Handle("agy", "ux-1", 4242, log=None)
    assert shell.liveness("antigravity", handle, state_dir=tmp_path) is Live.IDLE


# --- the foreman inside the tick ----------------------------------------------------------


def fake_foreman(verdict, note="because", citation=None, answer=None):
    import json as _json

    from fleet.foreman.judge import Foreman

    payload = {"verdict": verdict, "note": note}
    if citation:
        payload["citation"] = citation
    if answer:
        payload["answer"] = answer
    return Foreman(runner=lambda m, s, p: (0, _json.dumps({"result": _json.dumps(payload)})))


def with_foreman(world, verdict, rung, **kw):
    from fleet.adapters.base import State as Live
    from fleet.foreman import autonomy

    project, sessions = world
    led = Ledger(project.workspace / "ledger.db")
    sch = scheduler.Scheduler(
        project,
        led,
        {"agy": FakeAdapter(state=Live.GONE)},
        probe=FakeProbe(runs=("",), ancestor=False),
        workspace=str(project.workspace),
        foreman=fake_foreman(verdict, **kw),
        rung=autonomy.Rung(rung),
    )
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    return led, sch, sch.tick(dispatch=False)


def test_at_observe_the_verdict_is_recorded_and_nothing_moves(world):
    led, _, out = with_foreman(world, "merged-not-deployed", "observe")
    assert out.judged == ["ux-1=merged-not-deployed"]
    assert out.relaunched == [], "observe must not act"
    assert led.get("ux-1").state is State.NEEDS_YOU
    assert any(e["kind"] == "judged" for e in led.events())


def test_at_act_the_same_verdict_relaunches(world):
    led, _, out = with_foreman(world, "merged-not-deployed", "act")
    assert out.relaunched == ["ux-1"]
    assert led.get("ux-1").state is State.QUEUED
    assert "deploy" in (led.get("ux-1").note or "").lower()


def test_at_act_a_decision_still_reaches_him(world):
    led, _, out = with_foreman(
        world, "needs-decision", "act", citation="the brief", answer="use the existing key"
    )
    assert out.escalated == ["ux-1"]
    assert led.get("ux-1").state is State.NEEDS_YOU


def test_at_decide_the_answer_is_delivered_with_its_citation(world):
    led, _, out = with_foreman(
        world,
        "needs-decision",
        "decide",
        citation="ux-123 brief: drive listingUrlKeys",
        answer="use the existing listing params",
    )
    assert out.relaunched == ["ux-1"]
    note = led.get("ux-1").note or ""
    assert "listing params" in note
    assert "listingUrlKeys" in note, "the citation must ride with the answer"


def test_divergence_never_becomes_automatic_at_any_rung(world):
    for rung in ("observe", "act", "decide"):
        led, _, out = with_foreman(world, "diverged", rung)
        assert out.escalated == ["ux-1"], f"{rung} must not act on divergence"
        led.forget("ux-1")


def test_an_unreadable_answer_falls_through_to_him(world):
    from fleet.adapters.base import State as Live
    from fleet.foreman import autonomy
    from fleet.foreman.judge import Foreman

    project, sessions = world
    led = Ledger(project.workspace / "ledger.db")
    sch = scheduler.Scheduler(
        project,
        led,
        {"agy": FakeAdapter(state=Live.GONE)},
        probe=FakeProbe(runs=("",), ancestor=False),
        workspace=str(project.workspace),
        foreman=Foreman(runner=lambda m, s, p: (0, "I reckon it is fine")),
        rung=autonomy.Rung.ACT,
    )
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    out = sch.tick(dispatch=False)
    assert out.escalated == ["ux-1"]
    assert out.judged == [], "an answer we cannot read is not a verdict"


def test_no_foreman_at_all_behaves_exactly_as_phase_two_did(world):
    led, sch, sessions = build(
        world, {"agy": FakeAdapter(state=Live.GONE)}, probe=FakeProbe(runs=("",), ancestor=False)
    )
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    out = sch.tick(dispatch=False)
    assert out.escalated == ["ux-1"] and out.judged == []


# --- what the first real run broke on ----------------------------------------------------


def test_cpu_is_sampled_over_the_whole_tree_not_just_the_wrapper(monkeypatch):
    """The handle's pid is the tmux pane's shell, which does nothing at all while the agent
    works. Sampling only that made every claude session look idle within the hour, and on
    the first real run the watchdog reaped one burning 32 seconds of CPU."""
    from fleet.adapters import shell

    monkeypatch.setattr(shell.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(shell, "_children", lambda: {100: [200], 200: [300]})
    monkeypatch.setattr(shell, "_own_ticks", lambda pid: {100: 0, 200: 30, 300: 12}.get(pid, 0))
    assert shell.cpu_ticks(100) == 42, "the wrapper alone reports 0 forever"


def test_a_requeue_stops_the_old_session_first(world, monkeypatch):
    """Requeueing a live session left the old agent running and the relaunch collided:
    'tmux refused to start: duplicate session'."""
    from fleet.adapters.base import State as Live

    reaped = []
    monkeypatch.setattr(scheduler, "reap", lambda pid, name: reaped.append((pid, name)))

    led, sch, sessions = build(world, {"agy": FakeAdapter(state=Live.IDLE)})
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    led._db.execute(
        "UPDATE items SET started_at = ? WHERE session = 'ux-1'",
        (time.time() - scheduler.IDLE_SECONDS - 60,),
    )
    sch.tick(dispatch=False)
    assert reaped, "the stalled session must be stopped before it is replaced"


def test_a_verdict_retry_also_stops_the_old_session(world, monkeypatch):
    from fleet.adapters.base import State as Live

    reaped = []
    monkeypatch.setattr(scheduler, "reap", lambda pid, name: reaped.append((pid, name)))

    led, sch, sessions = build(
        world, {"agy": FakeAdapter(state=Live.GONE)}, probe=FakeProbe(ancestor=False)
    )
    write_session(sessions, "ux-1")
    led.add("ux-1")
    sch.tick()
    sch.tick(dispatch=False)
    assert reaped


def test_a_session_waiting_on_him_can_be_sent_round_again(world):
    """needs_you and parked wait for a human decision; `fleet add` is that decision."""
    led, _, _ = build(world)
    led.add("ux-1")
    led.transition("ux-1", State.NEEDS_YOU, note="looked wrong")
    assert led.add("ux-1") is True
    assert led.get("ux-1").state is State.QUEUED

    led.transition("ux-1", State.PARKED)
    assert led.add("ux-1") is True


def test_a_session_already_in_flight_is_not_added_twice(world):
    led, _, _ = build(world)
    led.add("ux-1")
    led.claim("ux-1")
    assert led.add("ux-1") is False
    led.transition("ux-1", State.VERIFYING)
    assert led.add("ux-1") is False


def test_the_parallel_cap_is_a_standing_limit_not_a_per_pass_one(world, monkeypatch, tmp_path):
    """A cap that lives only in the argv of `fleet start` is not a cap: the runner wrapper
    calls `tick` itself when a session exits and does not know what you typed. On the first
    real run that is how a cap of six ended up with seven running."""
    from fleet import paths

    monkeypatch.setattr(paths, "STATE", tmp_path)
    paths.write_cap(2)

    led, sch, sessions = build(world, {"agy": FakeAdapter(accounts={"main": 4})})
    for sid in ("ux-1", "ux-2", "ux-3", "ux-4"):
        write_session(sessions, sid)
        led.add(sid)

    assert len(sch.tick().dispatched) == 2, "the cap holds on the first pass"
    assert sch.tick().dispatched == [], "and on every pass after it, with no argument"


def test_no_cap_means_the_runner_ceilings_decide(world, monkeypatch, tmp_path):
    from fleet import paths

    monkeypatch.setattr(paths, "STATE", tmp_path)
    paths.write_cap(None)

    led, sch, sessions = build(world, {"agy": FakeAdapter(accounts={"main": 3})})
    for sid in ("ux-1", "ux-2", "ux-3"):
        write_session(sessions, sid)
        led.add(sid)
    assert len(sch.tick().dispatched) == 3
