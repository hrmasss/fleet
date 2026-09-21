"""The three adapters.

They land together on purpose. Building one and adding the others later makes the first one
special, and "special" is how a runner name ends up in the scheduler.

What actually differs between them is small: how to start the binary, where concurrency
comes from, and whether the queue may reach for them uninvited. agy has three accounts, a
real quota oracle, and `auto = True`. claude and cursor have one account each and
`auto = False`: they run only when a session names them.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from fleet.adapters import quota
from fleet.adapters.base import Capacity, Handle, State
from fleet.adapters.shell import (
    count_by_env,
    count_matching,
    liveness,
    process_lines,
    sh,
    tmux_send,
    tmux_spawn,
    write_wrapper,
)
from fleet.paths import STATE

LOGS = Path(os.environ.get("FLEET_LOGS", str(Path.home() / ".local/state/fleet/logs")))


def _log_for(session: str, attempt: int) -> str:
    LOGS.mkdir(parents=True, exist_ok=True)
    return str(LOGS / f"{session}.{attempt}.log")


def _script_for(session: str) -> str:
    LOGS.mkdir(parents=True, exist_ok=True)
    return str(LOGS / f"{session}.run.sh")


def _brief_file(session: str, brief: str) -> str:
    """Briefs go to a file, never onto a command line.

    They run to a couple of thousand words and contain quotes, backticks and newlines.
    Interpolating that into a shell command is how a dispatch silently truncates.
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    p = LOGS / f"{session}.brief.md"
    p.write_text(brief, encoding="utf-8")
    return str(p)


class Agy:
    """Antigravity, across three Google accounts.

    Capacity comes from `agy-next`, which already does the hard part: it discovers signed-in
    profiles, reads the real quota buckets, locks its rotation state so two dispatches
    cannot land on one account, and parks a profile for five hours after a limit error.
    Reimplementing any of that here would be a second opinion about the same facts.

    ⚠ It runs with `-p`, not `-i`. Interactive agy does not exit when a turn ends — it
    returns to its prompt and holds the pane open, which on 2026-09-21 left five finished
    sessions sitting on their slots for up to nineteen hours with their work already
    merged and deployed. `-p` exits, so the wrapper reports and the slot comes back. The
    cost is that a `-p` run cannot be nudged and kills its own background tasks on the way
    out, so a brief must carry everything it needs and say to keep commands in the
    foreground.
    """

    name = "agy"
    auto = True
    """The default runner, and the only one the queue reaches for on its own. Its accounts
    exist to be spent; that is the whole reason there are three of them."""

    tui = False
    """Only because of `-p`. Under `-i` this was True in everything but name, and the flag
    did not exist to say so."""

    STATUS = re.compile(r"^\s*(?P<profile>\S+)\s+last\s+\S+\s*\S*\s+(?P<rest>.*?)$", re.MULTILINE)
    FIVE_HOUR = re.compile(r"gemini-5h\s+(?P<pct>\d+)%")

    def __init__(self, per_account: int = 2, floor: float = 0.10) -> None:
        self.per_account = per_account
        self.floor = floor
        """Fraction of a Gemini bucket below which an account is treated as spent. agy-next
        uses 5%; this sits above it so fleet stops handing out work slightly earlier than
        the rotation would, rather than racing it to the bottom.

        ⚠ Every Gemini bucket, not just the five-hour one. On 2026-09-20 all three profiles
        had a full five-hour bucket and between 0.77% and 19% of the *weekly* left, and four
        sessions sat in `quota reached ... retrying` loops for fifteen hours while fleet
        counted them as working. The five-hour window refills six times a working day; the
        weekly is the one that actually runs out.

        The third-party buckets are left out. Claude and GPT are metered as separate
        credits, and an empty Gemini bucket says nothing about them.
        """

    GEMINI = "gemini"
    THIRD_PARTY = "3p"

    @classmethod
    def pool(cls, model: str | None) -> str:
        """Which of the account's two allowances this model spends.

        The quota API names the groups "Gemini Models" and "Claude and GPT models", and
        `agy models` lists exactly `gemini-*`, `claude-*` and `gpt-*`. So the prefix is
        the answer and there is no table to keep in sync with Google's model list.

        ⚠ Anything unrecognised counts as third-party rather than Gemini. A new Gemini
        model misread as third-party wastes a pool that is currently untouched; a new
        Claude model misread as Gemini would dispatch against an allowance that is empty
        on three of the seven accounts.
        """
        return cls.GEMINI if (model or "gemini").startswith("gemini") else cls.THIRD_PARTY

    def capacity(self, model: str | None = None) -> Capacity:
        rc, out = sh(["agy-next", "--status"], timeout=90)
        if rc != 0:
            return Capacity(self.name, {}, {}, reason=f"agy-next unavailable: {out[:80]}")

        per: dict[str, int] = {}
        detail: dict[str, str] = {}
        parked: dict[str, bool] = {}
        fallback: dict[str, float] = {}
        for line in out.splitlines():
            m = self.STATUS.match(line)
            if not m:
                continue
            profile, rest = m.group("profile"), m.group("rest")
            parked[profile] = "PARKED" in rest or "SPENT" in rest
            fallback[profile] = (
                int(m2.group("pct")) / 100 if (m2 := self.FIVE_HOUR.search(rest)) else 0.0
            )
            detail[profile] = rest.strip()

        pool = self.pool(model)
        limits = {p: quota.agy(Path(self._dir(p))) for p in parked}
        for profile in parked:
            left = self._pool_floor(limits[profile], pool, fallback[profile])
            per[profile] = 0 if (parked[profile] or left < self.floor) else self.per_account
            if not parked[profile] and left < self.floor:
                detail[profile] = f"{pool} at {left * 100:.1f}%, below the floor"

        lines = process_lines()
        observed = {
            profile: sum(count_matching(lines, f"--gemini_dir={d}") for d in self._dirs(profile))
            for profile in per
        }
        return Capacity(self.name, per, detail, observed=observed, quota=limits)

    @staticmethod
    def _pool_floor(buckets: list[quota.Bucket], pool: str, fallback: float) -> float:
        """The emptiest bucket in one pool, or the status line's figure if none were read.

        ⚠ Only that pool. The two are separate allowances of the same size, so letting an
        empty Gemini weekly speak for the Claude one would hide seven untouched accounts,
        and letting a full Claude one speak for Gemini would dispatch into a wall.
        """
        left = [
            b.remaining for b in buckets if b.id.startswith(f"{pool}-") and b.remaining is not None
        ]
        return min(left) if left else fallback

    @staticmethod
    def _dir(profile: str) -> str:
        return f"{Path.home()}/agy-accounts/{profile}"

    @staticmethod
    def _dirs(profile: str) -> list[str]:
        """Every path a session on this profile may have been launched with.

        ⚠ a1 was called `main` until 2026-09-20 and lived at `~/.gemini`; it is an ordinary
        profile now, with `~/agy-accounts/a1` symlinked to the old location so that live
        sessions and the absolute paths in the hooks keep resolving. A session started
        before the rename carries the old `--gemini_dir` on its command line for as long as
        it runs, so counting only the new path would lose it from the slot arithmetic and
        let fleet stack a fresh ceiling on top of work already in flight.
        """
        paths = [Agy._dir(profile)]
        if profile == "a1":
            paths.append(f"{Path.home()}/.gemini")
        return paths

    def launch(self, session: str, brief: str, workspace: str, model: str | None = None) -> Handle:
        account = None
        for profile, slots in self.capacity(model).per_account.items():
            if slots:
                account = profile
                break
        if account is None:
            raise RuntimeError("agy has no account with capacity")
        return self._start(session, brief, workspace, account, model)

    def launch_on(
        self, session: str, brief: str, workspace: str, account: str, model: str | None = None
    ) -> Handle:
        return self._start(session, brief, workspace, account, model)

    def _start(
        self, session: str, brief: str, workspace: str, account: str, model: str | None = None
    ) -> Handle:
        attempt = 1
        log = _log_for(session, attempt)
        path = _brief_file(session, brief)
        q = shlex.quote
        pick = f"--model {q(model)} " if model else ""
        cmd = (
            f"cd {q(workspace)} && agya {q(account)} {pick}--dangerously-skip-permissions "
            f'-p "$(cat {q(path)})"'
        )
        tmux = f"{session}-{account}"
        pid = tmux_spawn(tmux, write_wrapper(session, cmd, log, _script_for(session)))
        return Handle(self.name, session, pid, account=account, tmux=tmux, log=log)

    def liveness(self, handle: Handle, record: bool = True) -> State:
        return liveness("antigravity", handle, state_dir=STATE, record=record, tui=self.tui)

    def nudge(self, handle: Handle, text: str) -> bool:
        """Always False. A `-p` run has no prompt to type into.

        Keystrokes would land in the pane's shell once the agent exits, or in a run that
        is not reading them, and `base.py` is explicit that a runner which cannot take a
        continuation says so rather than pretending. The queue relaunches instead.
        """
        return False


class Claude:
    """Claude Code. One account, on request only.

    ⚠ This adapter used to delegate its whole quota question to `limit-guard.py`, the
    PreToolUse hook on the box, on the reasoning that a session which would blow the limit
    fails cleanly rather than half-shipping. That was wrong, and 2026-09-19 showed how. The
    hook gates the next *tool call inside a session it is already running in*. It cannot
    refuse a launch. So when the five-hour window ran out at 15:41, fleet started `fa-17`
    and `fa-18` into a spent account, both met Anthropic's own wall three seconds in before
    any tool call existed for the hook to block, and both burned an attempt on a session
    that never ran. The ceiling is read here now, from the same figure the board shows.
    """

    name = "claude"
    auto = False
    """On request. A session reaches claude by naming it, never by being next in line."""

    tui = False
    """Launched without `-p` but still non-interactive, because piping stdout puts Claude
    Code in print mode. The log is plain text and silent until the end, which is why CPU
    is the only signal that speaks for this runner."""

    PROCESS = "/share/claude/versions/"
    """What a live Claude Code session looks like from outside.

    ⚠ Not `claude --dangerously-skip-permissions`. That is what you type; it is not what
    runs. `claude` is a shim that execs the versioned binary, so the flag never appears
    beside the word `claude` on any command line, and this count was **always zero**. On
    2026-09-20 one session started from `claude agents` had been working for 47 minutes
    with a PR open while fleet reported the runner completely idle — every claude session
    it had ever counted came from its own ledger, and it had never once seen anybody
    else's. The agy adapter learned this on 2026-09-19 and claude was left with the bug.
    """

    NOT_A_SESSION = ("bg-pty-host", "bg-spare", "daemon run", "--bg-sp")
    """Claude Code puts a daemon, a pty host and a spare beside each session, all carrying
    the same install path. Counting the path alone turns one session into four."""

    def __init__(self, concurrent: int = 2, floor: float = 0.25) -> None:
        self.concurrent = concurrent
        self.floor = floor
        """Fraction of a window below which the account is treated as spent. It sits above
        `limit-guard.json`'s own stop — 20% left on this box — so fleet stops handing out
        work slightly before the in-session hook starts refusing tool calls. Launching into
        the gap between the two produces a session that starts, works for a minute and is
        then blocked mid-task, which is worse than not starting it."""

    def capacity(self, model: str | None = None) -> Capacity:
        # One allowance, whichever model runs inside it, so the argument changes nothing
        # here. It is accepted so the scheduler never has to know which runners have pools.
        rc, _ = sh(["claude", "--version"], timeout=20)
        if rc != 0:
            return Capacity(self.name, {}, {}, reason="claude not on PATH")
        observed = count_matching(process_lines(), self.PROCESS, self.NOT_A_SESSION)
        buckets = quota.claude()
        spent = [b for b in buckets if b.remaining is not None and b.remaining < self.floor]
        if spent:
            b = min(spent, key=lambda b: b.remaining or 0.0)
            left, floor = int((b.remaining or 0) * 100), int(self.floor * 100)
            why = f"{b.label} at {left}%, below the {floor}% floor"
            return Capacity(
                self.name,
                {"default": 0},
                {"default": why},
                reason=why,
                observed={"default": observed},
                quota={"default": buckets},
            )
        return Capacity(
            self.name,
            {"default": self.concurrent},
            {"default": "one account, on request"},
            observed={"default": observed},
            quota={"default": buckets},
        )

    def launch(self, session: str, brief: str, workspace: str, model: str | None = None) -> Handle:
        log = _log_for(session, 1)
        path = _brief_file(session, brief)
        q = shlex.quote
        pick = f"--model {q(model)} " if model else ""
        cmd = f'cd {q(workspace)} && claude {pick}--dangerously-skip-permissions "$(cat {q(path)})"'
        tmux = f"{session}-claude"
        pid = tmux_spawn(tmux, write_wrapper(session, cmd, log, _script_for(session)))
        return Handle(self.name, session, pid, account="default", tmux=tmux, log=log)

    def liveness(self, handle: Handle, record: bool = True) -> State:
        return liveness("claude", handle, state_dir=STATE, record=record, tui=self.tui)

    def nudge(self, handle: Handle, text: str) -> bool:
        return bool(handle.tmux) and tmux_send(handle.tmux, text)


class Cursor:
    """cursor-agent, across however many accounts are signed in.

    `-f` is its skip-permissions equivalent. It is launched interactively rather than with
    `-p`, because a printing run exits after one answer and cannot be nudged, which would
    make every continuation a relaunch.

    ⚠ cursor-agent has no profile flag. An account is a config overlay selected by
    XDG_CONFIG_HOME, which `cursora` sets, so two accounts produce identical command
    lines — which is why the observed count reads the environment rather than `ps`.
    """

    name = "cursor"
    auto = False
    """On request. Its allowance is measured in money rather than a fraction, so there is
    no floor to stop at — which is the more reason the queue may not reach for it."""

    tui = True
    """The one runner still launched interactively, so its log is a terminal and its CPU
    reading needs the idle-redraw floor. It keeps `-i` deliberately: a cursor continuation
    would otherwise be a relaunch every time."""

    def __init__(self, concurrent: int = 1, floor: float = 0.05) -> None:
        self.concurrent = concurrent
        self.floor = floor
        """Fraction of the line below which the account takes nothing. Lower than agy's
        and claude's because on-demand is off: the wall here is a clean stop, not a bill,
        so there is less to lose by running closer to it."""

    AUTO = "auto"
    API = "api"

    HOUSE = ("cursor-", "composer-", "vega", "grok-", "default", "auto")
    """Cursor's own models, which bill the auto line whatever the server list says.

    ⚠ `autoBucketModels` is incomplete. On 2026-09-20 it listed every grok 4.5 variant and
    no 4.6 one, while c2's billing put `cursor-grok-4.6-high` squarely in the auto bucket:
    auto was 81.667% of a $45 line, which is $36.75, which is grok 4.6 at $20.91 plus
    composer 2.5 at $15.84 to the cent. Trusting the list alone would have gated a grok 4.6
    session on the api line and spent the wrong allowance.

    So the two are unioned. The list catches a house model named oddly; the prefixes catch
    one the list has not learned yet. Both have now been wrong on their own.
    """

    def pool(self, model: str | None, profile: str = "c1") -> str:
        """Which line inside the plan this model bills.

        Cursor splits its plan into `auto` (its own models) and `api` (Claude, GPT,
        Gemini at vendor pass-through rates). The split is real money: the same 33M-token
        session costs $1.28 on gpt-5.6-luna and $20.91 on grok 4.6.
        """
        if model is None:
            return self.AUTO
        if any(model.startswith(h) for h in self.HOUSE):
            return self.AUTO
        return self.AUTO if model in quota.cursor_auto_models(profile) else self.API

    def capacity(self, model: str | None = None) -> Capacity:
        """⚠ Cursor's plan has two lines, `auto` and `api`, and they are metered apart.

        An earlier version of this read `noUsageBasedAllowed` and `customerBalance` and
        concluded there was one allowance. Those describe **on-demand billing** — paying
        past the plan — not the split inside it. On 2026-09-20 c1 had spent 36% of its
        auto line and 0% of its api one. A named Claude or GPT model reaches a pool that
        has never been touched.
        """
        rc, _ = sh(["cursor-agent", "--version"], timeout=20)
        if rc != 0:
            return Capacity(self.name, {}, {}, reason="cursor-agent not on PATH")
        profiles = quota.cursor_profiles()
        if not profiles:
            return Capacity(self.name, {}, {}, reason="no cursor account is signed in")

        per: dict[str, int] = {}
        detail: dict[str, str] = {}
        limits: dict[str, list[quota.Bucket]] = {}
        for p in profiles:
            limits[p] = quota.cursor(p)
            pool = self.pool(model, p)
            left = next((b.remaining for b in limits[p] if b.id == pool), None)
            if left is not None and left < self.floor:
                per[p] = 0
                detail[p] = f"{pool} at {left * 100:.0f}%, below the floor"
            else:
                per[p] = self.concurrent
                detail[p] = f"{pool} line"
        return Capacity(
            self.name,
            per,
            detail,
            observed={
                p: count_by_env("XDG_CONFIG_HOME", str(self._dir(p)), "cursor-agent")
                for p in profiles
            },
            quota=limits,
        )

    @staticmethod
    def _dir(profile: str) -> str:
        return f"{Path.home()}/cursor-accounts/{profile}"

    def launch(self, session: str, brief: str, workspace: str, model: str | None = None) -> Handle:
        profiles = quota.cursor_profiles()
        if not profiles:
            raise RuntimeError("no cursor account is signed in")
        return self.launch_on(session, brief, workspace, profiles[0], model)

    def launch_on(
        self, session: str, brief: str, workspace: str, account: str, model: str | None = None
    ) -> Handle:
        log = _log_for(session, 1)
        path = _brief_file(session, brief)
        q = shlex.quote
        pick = f"--model {q(model)} " if model else ""
        cmd = f'cd {q(workspace)} && cursora {q(account)} {pick}-f "$(cat {q(path)})"'
        tmux = f"{session}-{account}"
        pid = tmux_spawn(tmux, write_wrapper(session, cmd, log, _script_for(session)))
        return Handle(self.name, session, pid, account=account, tmux=tmux, log=log)

    def liveness(self, handle: Handle, record: bool = True) -> State:
        return liveness("cursor", handle, state_dir=STATE, record=record, tui=self.tui)

    def nudge(self, handle: Handle, text: str) -> bool:
        return bool(handle.tmux) and tmux_send(handle.tmux, text)
