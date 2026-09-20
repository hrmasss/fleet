"""Phase 1 — did this session actually ship?

Four mechanical checks, no tokens spent. Each one exists because it caught something real:

    merged    four green PRs once sat open for twenty hours while every report said done
    built     an images run cancelled by the next merge retags forward, so the deploy
              succeeds and production runs stale code with nothing reporting it
    running   production sat four commits behind while four sessions all claimed to ship
    recorded  work finished, session file never closed, so the board lied

⚠ **A ticked box is not evidence.** Every one of these checks reads the world, not the
session's own account of itself. That is the whole point: the executor's self-report is the
thing that has been wrong five times.

The shell-outs live behind `Probe` so the logic is testable without a network. `ShellProbe`
is the real one; the tests pass a fake.

A check can be `None`. Unknown is not failure — a session with no PR recorded has not been
shown to be broken, it has been shown to be unjudgeable, and firing a redeploy at it would
be acting on ignorance.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol

from fleet.foreman.verdicts import Verdict
from fleet.project import Project
from fleet.session import Session


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    state: str
    merge_commit: str | None


@dataclass(frozen=True, slots=True)
class Run:
    conclusion: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool | None
    evidence: str

    @property
    def mark(self) -> str:
        return {True: "pass", False: "FAIL", None: "?"}[self.ok]


@dataclass(frozen=True, slots=True)
class Result:
    session: str
    checks: tuple[Check, ...]
    walk: bool = False
    """A flow audit. Set by `check`, never inferred from the checks.

    ⚠ Inferring it was tried and was wrong: "only `recorded` applies" is also true of a fix
    session whose PR was never recorded, and that would have marked unshipped work as
    shipped. A walk says so in its own frontmatter or it is not one.
    """

    @property
    def shipped(self) -> bool:
        return all(c.ok is True for c in self.checks)

    @property
    def unknown(self) -> bool:
        return any(c.ok is None for c in self.checks)

    def failed(self, name: str) -> bool:
        return any(c.name == name and c.ok is False for c in self.checks)

    @property
    def walk_done(self) -> bool:
        """A walk is finished when its record is. Nothing else applies to it."""
        if not self.walk:
            return False
        recorded = [c for c in self.checks if c.name == "recorded"]
        return bool(recorded) and recorded[0].ok is True

    def undecided(self, name: str) -> bool:
        return any(c.name == name and c.ok is None for c in self.checks)

    @property
    def verdict(self) -> Verdict | None:
        """The verdict the gate can reach alone. None means it is the foreman's call.

        Order is deliberate: a thing that never merged cannot have failed to deploy, so the
        earliest broken link names the verdict.
        """
        if self.shipped:
            return Verdict.SHIPPED
        if self.walk_done:
            return Verdict.SHIPPED
        if self.failed("merged"):
            return Verdict.INCOMPLETE
        if self.failed("built"):
            return Verdict.BUILD_CANCELLED
        if self.failed("running"):
            # ⚠ A build still in flight has not failed to deploy — it has not finished.
            # Calling that `merged-not-deployed` would fire a redeploy at a commit whose
            # images do not exist yet, which is the loop racing the thing it is watching.
            if self.undecided("built"):
                return None
            return Verdict.MERGED_NOT_DEPLOYED
        if self.failed("recorded"):
            return Verdict.INCOMPLETE
        return None


class Probe(Protocol):
    """Every outward call the gate makes. Implemented for real by `ShellProbe`."""

    def pull_request(self, number: int) -> PullRequest | None: ...
    def build_runs(self, sha: str) -> list[Run]: ...
    def running_tags(self) -> dict[str, str | None]: ...
    def resolve(self, short: str) -> str | None: ...
    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None: ...


class ShellProbe:
    """gh, ssh and git. Every call is read-only; the gate never changes anything."""

    def __init__(self, project: Project, timeout: int = 60) -> None:
        self.p = project
        self.timeout = timeout

    def _run(self, cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, cwd=cwd)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return 127, str(e)
        return r.returncode, (r.stdout or r.stderr).strip()

    def pull_request(self, number: int) -> PullRequest | None:
        rc, out = self._run(
            ["gh", "pr", "view", str(number), "--repo", self.p.repo, "--json", "state,mergeCommit"]
        )
        if rc != 0:
            return None
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            return None
        mc = (d.get("mergeCommit") or {}).get("oid")
        return PullRequest(number=number, state=str(d.get("state", "")), merge_commit=mc)

    def build_runs(self, sha: str) -> list[Run]:
        rc, out = self._run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                self.p.repo,
                "--workflow",
                self.p.build_workflow,
                "--commit",
                sha,
                "--json",
                "conclusion,url",
                "--limit",
                "20",
            ]
        )
        if rc != 0:
            return []
        try:
            rows = json.loads(out)
        except json.JSONDecodeError:
            return []
        return [
            Run(conclusion=str(r.get("conclusion") or ""), url=str(r.get("url") or ""))
            for r in rows
        ]

    def running_tags(self) -> dict[str, str | None]:
        """Image tag per production container, keyed by exact name.

        ⚠ Staging containers share this daemon. Matching by exact name is what keeps
        `app-staging-web-1` out of a production verdict.
        """
        rc, out = self._run(
            ["ssh", self.p.prod_host, "docker ps --format '{{.Names}}\\t{{.Image}}'"]
        )
        found: dict[str, str] = {}
        if rc == 0:
            for line in out.splitlines():
                name, _, image = line.partition("\t")
                if name.strip() in self.p.prod_containers:
                    found[name.strip()] = image.strip().rpartition(":")[2]
        return {name: found.get(name) for name in self.p.prod_containers}

    def resolve(self, short: str) -> str | None:
        rc, out = self._run(["git", "rev-parse", f"{short}^{{commit}}"], cwd=str(self.p.code_repo))
        return out if rc == 0 and len(out) == 40 else None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        rc, _ = self._run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(self.p.code_repo),
        )
        if rc in (0, 1):
            return rc == 0
        return None


def _merged(session: Session, probe: Probe) -> tuple[Check, list[str]]:
    if not session.prs:
        return Check("merged", None, "no PR recorded in frontmatter"), []

    shas: list[str] = []
    problems: list[str] = []
    for n in session.prs:
        pr = probe.pull_request(n)
        if pr is None:
            problems.append(f"#{n} unreadable")
            continue
        if pr.state.upper() != "MERGED":
            problems.append(f"#{n} is {pr.state or 'unknown'}")
            continue
        if not pr.merge_commit:
            problems.append(f"#{n} merged with no merge commit")
            continue
        shas.append(pr.merge_commit)

    if problems:
        return Check("merged", False, "; ".join(problems)), shas
    listed = ", ".join(f"#{n} {s[:7]}" for n, s in zip(session.prs, shas, strict=False))
    return Check("merged", True, listed), shas


def _built(shas: list[str], probe: Probe) -> Check:
    if not shas:
        return Check("built", None, "no merge commit to look up")

    notes: list[str] = []
    for sha in shas:
        runs = probe.build_runs(sha)
        if not runs:
            return Check("built", None, f"no build run found for {sha[:7]}")
        outcomes = [r.conclusion.lower() for r in runs]
        if "success" in outcomes:
            notes.append(f"{sha[:7]} success")
            continue
        if any(not o for o in outcomes):
            # gh reports a null conclusion while a run is still going. In flight is not
            # cancelled and is not failure; it is "ask again in a few minutes".
            return Check("built", None, f"{sha[:7]} still building")
        if "cancelled" in outcomes:
            # The stale-image trap: cancelled by the next merge landing, and the tag
            # moves forward over code that was never built.
            return Check("built", False, f"{sha[:7]} cancelled with no successful build")
        return Check("built", False, f"{sha[:7]} {', '.join(outcomes) or 'no conclusion'}")
    return Check("built", True, "; ".join(notes))


def _running(shas: list[str], probe: Probe) -> Check:
    if not shas:
        return Check("running", None, "no merge commit to compare against")

    tags = probe.running_tags()
    missing = [n for n, t in tags.items() if not t]
    if missing:
        return Check("running", False, f"not running: {', '.join(missing)}")

    behind: list[str] = []
    unknown: list[str] = []
    for name, tag in tags.items():
        short = str(tag).split("-")[-1]
        full = probe.resolve(short)
        if full is None:
            if not any(s.startswith(short) for s in shas):
                unknown.append(f"{name} on {tag}, sha not resolvable locally")
            continue
        for sha in shas:
            anc = probe.is_ancestor(sha, full)
            if anc is None:
                unknown.append(f"{name} ancestry undecidable")
            elif not anc:
                behind.append(f"{name} on {tag}, missing {sha[:7]}")

    if behind:
        return Check("running", False, "; ".join(behind))
    if unknown:
        return Check("running", None, "; ".join(unknown))
    seen = sorted({str(t) for t in tags.values()})
    # ⚠ Say *why* this passes, not just what is running. A later deploy legitimately
    # carries an earlier commit, so a bare tag that differs from the merge sha reads as a
    # failure to anyone comparing them for equality — which is exactly how the foreman
    # misread its first real judgement and called a shipped session not-deployed.
    carried = ", ".join(sha[:7] for sha in shas)
    return Check(
        "running",
        True,
        f"all production containers on {', '.join(seen)}, which contains {carried}",
    )


def _recorded(session: Session) -> Check:
    gaps: list[str] = []
    if not session.closed:
        gaps.append(f"status is {session.status!r}, not done")
    if not session.criteria:
        gaps.append("no acceptance criteria found")
    elif session.unticked:
        n = len(session.unticked)
        first = session.unticked[0].text
        gaps.append(
            f"{n} criterion unticked, first: {first[:60]}"
            if n == 1
            else f"{n} criteria unticked, first: {first[:60]}"
        )
    if gaps:
        return Check("recorded", False, "; ".join(gaps))
    return Check("recorded", True, f"closed, {len(session.criteria)} criteria ticked")


def check(session: Session, probe: Probe) -> Result:
    if session.walk:
        # A flow audit ships nothing. The only question it can be asked is whether it was
        # actually finished and written up, so the three shipping checks are marked not
        # applicable rather than failed — a walk is not a broken change.
        na = "not applicable: this is a flow audit, it ships no code"
        return Result(
            session=session.id,
            walk=True,
            checks=(
                Check("merged", None, na),
                Check("built", None, na),
                Check("running", None, na),
                _recorded(session),
            ),
        )

    merged, shas = _merged(session, probe)
    return Result(
        session=session.id,
        checks=(merged, _built(shas, probe), _running(shas, probe), _recorded(session)),
    )
