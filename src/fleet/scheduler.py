"""The tick. Reconcile what is running, judge what finished, start what fits.

One entry point, `tick()`, called from three places and safe from all of them: the runner
wrapper the moment a session exits (the push), an hourly timer (the net), and by hand.

Nothing here polls in the normal case. The wrapper calls `fleet tick` itself, so a finishing
session starts the next one within a second. The timer exists only for the cases the push
cannot cover: a wrapper killed with its pane, a box rebooted, a tick that crashed.

⚠ Brakes come before automation. Production has been customer-facing since 2026-09-15, so
this can cause an outage rather than a broken demo. The kill file stops everything, the
attempt cap stops a session relaunching forever, and neither is optional.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from fleet import gate, paths
from fleet import session as sessions
from fleet.adapters.base import Adapter, Handle
from fleet.adapters.base import State as Live
from fleet.adapters.shell import reap, sh, silent_for
from fleet.foreman import autonomy
from fleet.foreman.judge import Foreman, Judgement
from fleet.foreman.packet import build as build_packet
from fleet.foreman.verdicts import Verdict
from fleet.ledger import LIVE, Item, Ledger, State
from fleet.paths import read_cap
from fleet.project import Project

KILL_FILE = Path(os.environ.get("FLEET_KILL", str(Path.home() / ".local/state/fleet/STOP")))
MAX_ATTEMPTS = int(os.environ.get("FLEET_MAX_ATTEMPTS", "2"))
IDLE_SECONDS = float(os.environ.get("FLEET_IDLE_SECONDS", "1800"))
"""Idle this long with no exit is a stall. Thirty minutes is deliberately generous: a long
browser walk goes quiet for a while, and reaping real work is worse than waiting."""
MAX_SILENCE_SECONDS = float(os.environ.get("FLEET_MAX_SILENCE_SECONDS", str(3 * 3600)))
"""A running session that has not said anything new in this long is stuck, whatever its
CPU or its runner's own liveness claims.

Not a runtime cap. A monitoring job or a long small task may run for days and should, as
long as it keeps reporting. What this catches is the opposite: on 2026-09-23 fa-11 was
found seventy hours into an attempt that went silent after sixty-six minutes, its CPU
counter moving the whole time. Three hours is past the longest quiet stretch real work
showed that week, a build queued behind five others on one lock. A session that is
meant to go quieter than that says so with `quiet_for:` in its frontmatter."""


CONTINUATIONS: dict[Verdict, str] = {
    Verdict.MERGED_NOT_DEPLOYED: (
        "Your PR is merged but production is not running it. Dispatch the production "
        "deploy and confirm the running image tag contains the merge commit. Do not "
        "change any code and do not widen the scope."
    ),
    Verdict.BUILD_CANCELLED: (
        "The images build for your merge commit was cancelled, so production would run "
        "stale code. Re-run the images workflow on main's head, wait for it to go green, "
        "then deploy and confirm the running tag. Do not change any code."
    ),
    Verdict.INCOMPLETE: (
        "Your session is not finished. Close the gaps listed below, then finish the "
        "session as its acceptance criteria require. Do not add scope."
    ),
}
"""⚠ A continuation may only point at the original brief and name a gap.

Writing one is writing a brief, which is judgement work. Bounding it to a template is what
keeps that judgement with the operator. If a gap cannot be said this way, the verdict is not one
of these and the session goes to him instead.
"""


@dataclass
class Outcome:
    dispatched: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    relaunched: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    judged: list[str] = field(default_factory=list)
    halted: str | None = None

    @property
    def quiet(self) -> bool:
        return not any(
            (
                self.dispatched,
                self.closed,
                self.relaunched,
                self.escalated,
                self.stalled,
                self.judged,
            )
        )


def brief_for(project: Project, item: Item, session_path: Path) -> str:
    """What the agent is actually told.

    It points at the session file rather than inlining it. The file is the work record, the
    agent updates it as it goes, and a brief that copies it creates a second version that
    immediately disagrees with the first.

    ⚠ The foreground line is not style advice. A printing run kills its own background
    tasks when it exits, so an agent that backgrounds a build and waits for it loses the
    build and reports on nothing. It is said to every runner rather than to the one that
    needs it, because nothing outside `adapters/` may know which runner this is.
    """
    head = (
        f"Work session {item.session}. Its brief, acceptance criteria and traps are in "
        f"{session_path}. Read `todo/CLAUDE.md` for the pipeline rules before you claim "
        f"it, and `todo/FINDINGS.md` for what is still open on production.\n\n"
        f"Take it end to end: claim it, do the work in a worktree, open a PR, get it "
        f"merged, deploy it, confirm the running production image tag contains your merge "
        f"commit, re-walk the change on the live site, then close the session with "
        f"per-criterion evidence.\n\n"
        f"Run builds, tests and deploys in the foreground and read the output. Do not "
        f"background a long command and poll it — it will be killed when you exit.\n\n"
        f"Finish. Do not stop one step short: a merged PR that is not deployed is not "
        f"done, and a ticked box with no evidence behind it is worse than an unticked one."
    )
    if item.note and item.attempts > 0:
        head += f"\n\n--- This is attempt {item.attempts + 1}. ---\n{item.note}"
    return head


class Scheduler:
    def __init__(
        self,
        project: Project,
        ledger: Ledger,
        adapters: dict[str, Adapter],
        probe: gate.Probe | None = None,
        workspace: str | None = None,
        foreman: Foreman | None = None,
        rung: autonomy.Rung | None = None,
    ) -> None:
        self.p = project
        self.led = ledger
        self.adapters = adapters
        self.probe = probe or gate.ShellProbe(project)
        self.workspace = workspace or str(project.workspace)
        self.foreman = foreman
        self.rung = rung if rung is not None else autonomy.read()

    # --- brakes ---------------------------------------------------------------------

    def halted(self) -> str | None:
        if KILL_FILE.exists():
            try:
                why = KILL_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                why = ""
            return why or f"{KILL_FILE} exists"
        return None

    # --- the three passes -----------------------------------------------------------

    def reconcile(self, out: Outcome) -> None:
        """Has anything running actually stopped, or gone quiet long enough to be stuck?"""
        for it in self.led.in_state(State.RUNNING):
            adapter = self.adapters.get(it.runner or "")
            if adapter is None:
                self.led.transition(
                    it.session, State.NEEDS_YOU, note=f"no adapter for runner {it.runner!r}"
                )
                out.escalated.append(it.session)
                continue

            handle = Handle(
                it.runner or "",
                it.session,
                it.pid or 0,
                account=it.account,
                tmux=it.tmux,
                log=it.log,
            )
            state = adapter.liveness(handle)

            if state is Live.GONE:
                # The wrapper normally reports this first. Getting here means it did not,
                # so the exit is being noticed by the net rather than the push.
                self.led.transition(
                    it.session,
                    State.VERIFYING,
                    kind="exited",
                    note="process gone, no exit event received",
                )
                continue

            quiet = silent_for(it.session, it.log, paths.STATE)
            limit = self._quiet_limit(it.session)
            if quiet is not None and quiet > limit:
                self._stall(it, out, quiet, why=f"said nothing new for {quiet / 3600:.1f}h")
                continue

            ran = time.time() - it.started_at if it.started_at else 0.0
            if state is Live.IDLE and ran > IDLE_SECONDS:
                self._stall(it, out, ran)

    def _quiet_limit(self, session: str) -> float:
        """The session's own `quiet_for`, or the queue default when it names none."""
        try:
            own = sessions.load(sessions.find(self.p.sessions, session)).quiet_for
        except (FileNotFoundError, ValueError, OSError):
            own = None
        return MAX_SILENCE_SECONDS if own is None else own

    def _stall(self, it: Item, out: Outcome, quiet: float, why: str | None = None) -> None:
        # ⚠ Stop it before replacing it. Requeueing a live session leaves the old agent
        # running — burning quota, possibly still writing to the same worktree — and the
        # relaunch then collides on the tmux name and fails outright.
        reap(it.pid, it.tmux)
        note = why or f"idle {int(quiet // 60)}m with no exit and no question"
        if it.attempts >= MAX_ATTEMPTS:
            self.led.transition(
                it.session,
                State.PARKED,
                verdict=Verdict.STALLED.value,
                note=f"{note}; {it.attempts} attempts spent",
            )
            out.escalated.append(it.session)
            return
        self.led.requeue(it.session, note)
        out.stalled.append(it.session)

    def verify(self, out: Outcome) -> None:
        """Run the gate on anything that finished, and act on what it can decide alone."""
        for it in self.led.in_state(State.VERIFYING):
            try:
                s = sessions.load(sessions.find(self.p.sessions, it.session))
            except (FileNotFoundError, ValueError, OSError) as e:
                self.led.transition(it.session, State.NEEDS_YOU, note=str(e))
                out.escalated.append(it.session)
                continue

            result = gate.check(s, self.probe)
            verdict = result.verdict

            if verdict is Verdict.SHIPPED:
                self.led.transition(
                    it.session,
                    State.DONE,
                    verdict=verdict.value,
                    note="gate: merged, built, running, recorded",
                )
                out.closed.append(it.session)
                continue

            if verdict in CONTINUATIONS:
                self._retry(it, out, verdict, result)
                continue

            # The gate could not decide. Ask the foreman, if there is one and if the rung
            # lets it matter. An unknown treated as a failure is how a loop redeploys a
            # commit whose images do not exist yet.
            if self._adjudicate(it, s, result, out):
                continue

            failing = "; ".join(
                f"{c.name}: {c.evidence}" for c in result.checks if c.ok is not True
            )
            self.led.transition(
                it.session,
                State.NEEDS_YOU,
                verdict=verdict.value if verdict else None,
                note=failing or "gate could not decide",
            )
            out.escalated.append(it.session)

    def _retry(self, it: Item, out: Outcome, verdict: Verdict, result: gate.Result) -> None:
        reap(it.pid, it.tmux)
        gaps = "; ".join(f"{c.name}: {c.evidence}" for c in result.checks if c.ok is False)
        note = f"{CONTINUATIONS[verdict]}\n\nWhat the check found: {gaps}"
        if it.attempts >= MAX_ATTEMPTS:
            self.led.transition(
                it.session,
                State.PARKED,
                verdict=verdict.value,
                note=f"{it.attempts} attempts spent. {gaps}",
            )
            out.escalated.append(it.session)
            return
        self.led.requeue(it.session, note)
        self.led.record(it.session, "verdict", verdict.value)
        out.relaunched.append(it.session)

    def _adjudicate(self, it: Item, session, result: gate.Result, out: Outcome) -> bool:
        """Call the foreman. True means it settled the session.

        ⚠ At `observe` this records the verdict and still sends the session to the operator.
        That is the whole point of the rung: a week of judgements they can check before any
        of them is allowed to move anything.
        """
        if self.foreman is None or self.rung is autonomy.Rung.OFF:
            return False

        packet = build_packet(
            session, result, log=it.log, attempts=it.attempts, previous_note=it.note
        )
        j = self.foreman.judge(packet)
        self.led.record(
            it.session,
            "judged",
            f"{j.verdict.value if j.verdict else 'unreadable'} via {j.model}: {j.note}"[:400],
        )
        if not j.usable:
            # An answer we could not read is not a verdict. Fall through to the operator rather
            # than act on a sentence.
            return False

        out.judged.append(f"{it.session}={j.verdict.value}")
        if not autonomy.may_act(self.rung, j.verdict):
            self.led.transition(
                it.session,
                State.NEEDS_YOU,
                verdict=j.verdict.value,
                note=f"foreman ({j.model}): {j.note}",
            )
            out.escalated.append(it.session)
            return True

        return self._apply(it, out, j, result)

    def _apply(self, it: Item, out: Outcome, j: Judgement, result: gate.Result) -> bool:
        """Act on a verdict the rung permits. Only the shapes the queue actually has."""
        if j.verdict is Verdict.SHIPPED:
            self.led.transition(
                it.session,
                State.DONE,
                verdict=j.verdict.value,
                note=f"foreman ({j.model}): {j.note}",
            )
            out.closed.append(it.session)
            return True

        if j.verdict is Verdict.NEEDS_DECISION:
            # The citation is the licence. `parse` already downgrades an uncited answer, so
            # reaching here means a source was quoted, and it goes into the note so every
            # decision made on their behalf is auditable at the end of the day.
            note = (
                f"{j.answer}\n\nThis was decided for you from: {j.citation}\n"
                f"If that is wrong, say so and the session will be re-briefed."
            )
            self._requeue_or_park(it, out, j.verdict, note, j.note)
            return True

        if j.verdict in CONTINUATIONS:
            gaps = "; ".join(f"{c.name}: {c.evidence}" for c in result.checks if c.ok is False)
            note = f"{CONTINUATIONS[j.verdict]}\n\nWhat the check found: {gaps or j.note}"
            self._requeue_or_park(it, out, j.verdict, note, j.note)
            return True

        if j.verdict is Verdict.QUOTA_EXHAUSTED:
            if it.account and it.runner == "agy":
                sh(["agy-next", "--limited", it.account])
            self.led.requeue(it.session, f"{it.account or it.runner} out of quota: {j.note}")
            out.stalled.append(it.session)
            return True

        if j.verdict is Verdict.STALLED:
            self._stall(it, out, 0.0)
            return True

        return False

    def _requeue_or_park(
        self, it: Item, out: Outcome, verdict: Verdict, note: str, why: str
    ) -> None:
        reap(it.pid, it.tmux)
        if it.attempts >= MAX_ATTEMPTS:
            self.led.transition(
                it.session,
                State.PARKED,
                verdict=verdict.value,
                note=f"{it.attempts} attempts spent. {why}",
            )
            out.escalated.append(it.session)
            return
        self.led.requeue(it.session, note)
        out.relaunched.append(it.session)

    def free_slots(self, model: str | None = None) -> dict[tuple[str, str], int]:
        """Per (runner, account), how many more sessions may start right now.

        ⚠ Busy is the larger of what the ledger says fleet started and what the OS says is
        actually running. fleet is not the only thing that starts agents here: on
        2026-09-19 eight agy sessions were live that fleet had never heard of, dispatched
        by hand and by Hermes, and counting only its own would have stacked a full ceiling
        on top of them. Taking the max counts fleet's own once and everyone else's too.
        """
        running = self.led.running_by_account()
        free: dict[tuple[str, str], int] = {}
        for name, adapter in self.adapters.items():
            cap = adapter.capacity(model)
            for account, ceiling in cap.per_account.items():
                busy = max(running.get((name, account), 0), cap.observed.get(account, 0))
                if ceiling - busy > 0:
                    free[(name, account)] = ceiling - busy

        # A global cap on top of the per-account ceilings. Without it the ceilings sum to
        # more than anyone asked for, and the wrapper's own tick quietly exceeds whatever
        # was typed at `fleet start`.
        limit = read_cap()
        if limit is None:
            return free
        room = limit - len(self.led.in_state(*LIVE))
        if room <= 0:
            return {}

        # ⚠ Auto-claiming runners take the room first. The trim used to go by size alone,
        # which meant a cap of six with agy and claude both two free could hand the whole
        # remainder to claude — a runner only a named session can use — and leave an
        # unnamed session with nowhere to go while agy sat empty.
        def order(kv: tuple[tuple[str, str], int]) -> tuple[int, int]:
            (runner, _), n = kv
            return (0 if getattr(self.adapters.get(runner), "auto", True) else 1, -n)

        trimmed: dict[tuple[str, str], int] = {}
        for slot, n in sorted(free.items(), key=order):
            if room <= 0:
                break
            take = min(n, room)
            trimmed[slot] = take
            room -= take
        return trimmed

    def dispatch(self, out: Outcome, limit: int | None = None) -> None:
        queued = self.led.in_state(State.QUEUED)
        if not queued:
            return
        started = 0

        # ⚠ Capacity depends on the model, because on agy the model decides which of the
        # account's two allowances the work spends. So it is read once per distinct model
        # in the queue rather than once per tick. In practice that is one or two reads; a
        # queue of thirty sessions asking for thirty different models would be a different
        # problem than this one.
        #
        # The per-model views are kept side by side rather than merged: a slot free for a
        # Claude session on a1 is genuinely not free for a Gemini one, and collapsing them
        # would hand out the same seat twice.
        views: dict[str | None, dict[tuple[str, str], int]] = {}
        for it in queued:
            if it.model not in views:
                views[it.model] = self.free_slots(it.model)

        for it in queued:
            if limit is not None and started >= limit:
                break
            free = views[it.model]
            slot = self._pick(it, free)
            if slot is None:
                continue
            runner, account = slot

            if not self.led.claim(it.session):
                continue  # someone else took it between the read and the claim
            try:
                path = sessions.find(self.p.sessions, it.session)
                handle = self._launch(runner, account, it, path)
            except (FileNotFoundError, ValueError, OSError, RuntimeError) as e:
                self.led.transition(it.session, State.NEEDS_YOU, note=f"launch failed: {e}")
                out.escalated.append(it.session)
                continue

            self.led.launched(it.session, runner, account, handle.pid, handle.tmux, handle.log)
            # ⚠ Spend the seat in every view, not just this session's. The slot is one
            # tmux pane on one account; which pool it bills is a different question from
            # whether it is occupied.
            for view in views.values():
                if slot in view:
                    view[slot] -= 1
                    if view[slot] <= 0:
                        del view[slot]
            out.dispatched.append(it.session)
            started += 1

    def _pick(self, it: Item, free: dict[tuple[str, str], int]) -> tuple[str, str] | None:
        """A session that names a runner waits for it, however long that takes. One that
        names none may only go to a runner that auto-claims, and among those it takes the
        emptiest slot, which spreads load rather than filling one account first.

        ⚠ An unnamed session waits for agy rather than spilling onto claude or cursor.
        Those two are the operator's own tools, and a queue that drains them has taken his
        terminal away to save some waiting. Which runners auto-claim is the adapter's
        declaration, not a name checked here.

        ⚠ It reads `wanted`, never `runner`. `runner` is where the session last ran, which
        `launched()` overwrites, so an unnamed session that once landed on claude would
        otherwise be pinned to claude for every attempt after it.
        """
        wanted = it.wanted
        if wanted is not None:
            options = [k for k in free if k[0] == wanted]
        else:
            options = [k for k in free if getattr(self.adapters.get(k[0]), "auto", True)]
        if not options:
            return None
        return max(options, key=lambda k: free[k])

    def _launch(self, runner: str, account: str, it: Item, path: Path) -> Handle:
        adapter = self.adapters[runner]
        brief = brief_for(self.p, it, path)
        launch_on = getattr(adapter, "launch_on", None)
        if callable(launch_on):
            return launch_on(it.session, brief, self.workspace, account, it.model)
        return adapter.launch(it.session, brief, self.workspace, it.model)

    # --- the whole tick -------------------------------------------------------------

    def tick(self, limit: int | None = None, dispatch: bool = True) -> Outcome:
        out = Outcome()
        halt = self.halted()
        if halt:
            # Reconciling and verifying are read-only about the world. Only dispatch is
            # stopped, so a halted queue still tells the truth about what is running.
            out.halted = halt
            self.reconcile(out)
            self.verify(out)
            return out

        self.reconcile(out)
        self.verify(out)
        if dispatch:
            self.dispatch(out, limit)
        return out
