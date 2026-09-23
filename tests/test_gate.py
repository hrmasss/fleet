"""The gate, against a fake world.

Every scenario here is one that actually happened on the box. If a test reads like an
invented edge case, it is not — check docs/decisions.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet import gate, session
from fleet.foreman.verdicts import Verdict
from fleet.gate import PullRequest, Run
from fleet.project import Project

MERGE = "010e0555" + "a" * 32
OLDER = "789c359f" + "b" * 32

SESSION_MD = """---
id: ux-123
title: A session that did some work
status: {status}
owner: agy a2
prs: {prs}
---

# ux-123

## Acceptance criteria

{criteria}

## Out of scope

- [ ] this is not an acceptance criterion and must never be counted

## Progress log

- [x] neither is this
"""


def make(tmp_path: Path, *, status="done", prs="[524]", criteria="- [x] one\n- [x] two"):
    p = tmp_path / "ux-123-a-session.md"
    p.write_text(SESSION_MD.format(status=status, prs=prs, criteria=criteria), encoding="utf-8")
    return session.load(p)


class FakeProbe:
    def __init__(
        self,
        *,
        pr_state="MERGED",
        merge=MERGE,
        runs=("success",),
        tags=None,
        resolvable=True,
        ancestor=True,
    ):
        self.pr_state, self.merge, self.runs = pr_state, merge, runs
        self.tags = {"app-web-1": "sha-010e055"} if tags is None else tags
        self.resolvable, self.ancestor = resolvable, ancestor

    def pull_request(self, number):
        return PullRequest(number, self.pr_state, self.merge if self.pr_state == "MERGED" else None)

    def build_runs(self, sha):
        return [Run(conclusion=c) for c in self.runs]

    def running_tags(self):
        return dict(self.tags)

    def resolve(self, short):
        return (short + "0" * (40 - len(short))) if self.resolvable else None

    def is_ancestor(self, ancestor, descendant):
        return self.ancestor


def verdict_for(tmp_path, probe, **kw):
    return gate.check(make(tmp_path, **kw), probe).verdict


# --- the shipped case -------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """A project of this test's own. fleet ships no default coordinates, so anything
    touching the gate has to say which containers and which repo it means."""
    return Project(
        repo="owner/app",
        workspace=tmp_path,
        code_repo=tmp_path,
        prod_host="prod",
        prod_containers=("app-web-1", "app-api-1"),
        build_workflow="images",
        deploy_workflow="deploy",
    )


def test_a_session_that_truly_shipped(tmp_path):
    r = gate.check(make(tmp_path), FakeProbe())
    assert r.shipped
    assert r.verdict is Verdict.SHIPPED


# --- the five failures that actually happened -------------------------------------------


def test_green_pr_left_open_is_incomplete(tmp_path):
    assert verdict_for(tmp_path, FakeProbe(pr_state="OPEN")) is Verdict.INCOMPLETE


def test_cancelled_build_is_caught_and_not_mistaken_for_a_deploy_problem(tmp_path):
    """An images run cancelled by the next merge retags forward over unbuilt code."""
    v = verdict_for(tmp_path, FakeProbe(runs=("cancelled",)))
    assert v is Verdict.BUILD_CANCELLED


def test_a_cancelled_run_beside_a_successful_one_is_fine(tmp_path):
    assert verdict_for(tmp_path, FakeProbe(runs=("cancelled", "success"))) is Verdict.SHIPPED


def test_merged_but_production_is_behind(tmp_path):
    v = verdict_for(tmp_path, FakeProbe(ancestor=False))
    assert v is Verdict.MERGED_NOT_DEPLOYED


def test_unticked_criteria_are_incomplete_even_when_deployed(tmp_path):
    v = verdict_for(tmp_path, FakeProbe(), criteria="- [x] one\n- [ ] two")
    assert v is Verdict.INCOMPLETE


def test_status_not_done_is_incomplete_even_when_deployed(tmp_path):
    assert verdict_for(tmp_path, FakeProbe(), status="in-progress") is Verdict.INCOMPLETE


# --- unknown is not failure -------------------------------------------------------------


def test_no_pr_recorded_is_unjudgeable_not_broken(tmp_path):
    r = gate.check(make(tmp_path, prs="[]"), FakeProbe())
    assert r.unknown and not r.shipped
    assert r.verdict is None, "an unknown must never trigger an automatic redeploy"


def test_no_build_run_found_is_unknown(tmp_path):
    r = gate.check(make(tmp_path), FakeProbe(runs=()))
    assert r.unknown
    assert r.verdict is None


def test_unresolvable_running_sha_is_unknown_not_behind(tmp_path):
    probe = FakeProbe(resolvable=False, tags={"app-web-1": "sha-ffffff9"})
    r = gate.check(make(tmp_path), probe)
    assert r.unknown
    assert r.verdict is None


# --- the staging trap -------------------------------------------------------------------


def test_a_container_that_is_not_running_fails(tmp_path):
    r = gate.check(make(tmp_path), FakeProbe(tags={"app-web-1": None}))
    assert r.failed("running")
    assert r.verdict is Verdict.MERGED_NOT_DEPLOYED


def test_probe_never_reads_a_staging_container(monkeypatch, project):
    """⚠ A staging container shares the daemon with the production one and its name
    contains the production one's. A substring match reads staging as production and
    reports a deploy that never happened."""
    probe = gate.ShellProbe(project)
    monkeypatch.setattr(
        probe,
        "_run",
        lambda *a, **k: (
            0,
            "app-web-1\tghcr.io/owner/web:sha-789c359\n"
            "app-staging-web-1\tghcr.io/owner/web:sha-acc3586",
        ),
    )
    tags = probe.running_tags()
    assert tags["app-web-1"] == "sha-789c359"
    assert "app-staging-web-1" not in tags


# --- parsing ----------------------------------------------------------------------------


def test_only_the_acceptance_section_counts_as_criteria(tmp_path):
    s = make(tmp_path)
    assert len(s.criteria) == 2, "out-of-scope and progress checkboxes must not be counted"


def test_find_resolves_a_bare_id(tmp_path):
    make(tmp_path)
    assert session.find(tmp_path, "ux-123").name == "ux-123-a-session.md"


def test_find_refuses_an_ambiguous_id(tmp_path):
    make(tmp_path)
    (tmp_path / "ux-123-another.md").write_text("---\nid: ux-123\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="more than one"):
        session.find(tmp_path, "ux-123")


def test_a_file_without_frontmatter_is_not_a_session(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# just notes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no frontmatter"):
        session.load(p)


# --- malformed session files, found by running against the real workspace ---------------


def test_a_hash_in_prs_is_reported_not_crashed(tmp_path):
    """`prs: [#526]` was live in ux-123. YAML eats `#` as a comment and the list never
    closes, so the parser died and took the whole run with it."""
    p = tmp_path / "ux-123-bad.md"
    p.write_text("---\nid: ux-123\nstatus: done\nprs: [#526]\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid YAML"):
        session.load(p)


def test_a_quoted_hash_pr_is_accepted(tmp_path):
    p = tmp_path / "ux-123-ok.md"
    p.write_text('---\nid: ux-123\nprs: ["#526", 527]\n---\n\nbody\n', encoding="utf-8")
    assert session.load(p).prs == (526, 527)


def test_a_bare_pr_number_is_accepted(tmp_path):
    p = tmp_path / "ux-1-scalar.md"
    p.write_text("---\nid: ux-1\nprs: 524\n---\n\nbody\n", encoding="utf-8")
    assert session.load(p).prs == (524,)


def test_a_pr_that_is_not_a_number_is_refused(tmp_path):
    p = tmp_path / "ux-1-junk.md"
    p.write_text("---\nid: ux-1\nprs: [later]\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a PR number"):
        session.load(p)


def test_a_build_still_running_is_not_cancelled(tmp_path):
    """gh reports a null conclusion while a run is in flight."""
    r = gate.check(make(tmp_path), FakeProbe(runs=("",)))
    assert r.undecided("built")
    assert r.verdict is not Verdict.BUILD_CANCELLED


def test_no_redeploy_is_ordered_while_the_build_is_still_running(tmp_path):
    """Production cannot be behind a commit whose images do not exist yet."""
    r = gate.check(make(tmp_path), FakeProbe(runs=("",), ancestor=False))
    assert r.failed("running")
    assert r.verdict is None, "merged-not-deployed here would race the build"


def test_a_passing_running_check_says_why_it_passes(tmp_path):
    """A bare tag that differs from the merge sha reads as a failure to anyone comparing
    them for equality. The foreman misread exactly that on its first real judgement."""
    probe = FakeProbe(tags={"app-web-1": "sha-e6dd00a"})
    r = gate.check(make(tmp_path), probe)
    running = next(c for c in r.checks if c.name == "running")
    assert running.ok is True
    assert "contains" in running.evidence
    assert MERGE[:7] in running.evidence


# --- flow audits ship nothing -----------------------------------------------------------


WALK_MD = """---
id: fa-09
kind: walk
status: {status}
prs: []
---

# fa-09

## Acceptance criteria

{criteria}
"""


def walk_session(tmp_path, *, status="done", criteria="- [x] walked"):
    p = tmp_path / "fa-09-x.md"
    p.write_text(WALK_MD.format(status=status, criteria=criteria), encoding="utf-8")
    return session.load(p)


def test_a_finished_walk_is_shipped_not_incomplete(tmp_path):
    """Judging an audit by whether production runs its merge commit would mark every walk
    this project has ever done as unfinished."""
    r = gate.check(walk_session(tmp_path), FakeProbe())
    assert r.verdict is Verdict.SHIPPED


def test_an_unfinished_walk_is_still_incomplete(tmp_path):
    r = gate.check(walk_session(tmp_path, status="in-progress"), FakeProbe())
    assert r.verdict is Verdict.INCOMPLETE


def test_a_walk_never_reports_a_deploy_problem(tmp_path):
    r = gate.check(walk_session(tmp_path, criteria="- [ ] walked"), FakeProbe(ancestor=False))
    assert not r.failed("running")
    assert r.verdict is Verdict.INCOMPLETE


def test_a_fix_session_is_unaffected(tmp_path):
    assert gate.check(make(tmp_path), FakeProbe()).verdict is Verdict.SHIPPED


def test_a_fix_session_with_no_pr_is_never_mistaken_for_a_finished_walk(tmp_path):
    """Inferring "walk" from "only recorded applies" would mark unshipped work as shipped:
    a fix session whose PR was never recorded looks identical from the checks alone."""
    r = gate.check(make(tmp_path, prs="[]"), FakeProbe())
    assert r.walk is False
    assert r.walk_done is False
    assert r.verdict is None


@pytest.mark.parametrize(
    "value, seconds",
    [
        ("90m", 5400),
        ("6h", 21600),
        ("2d", 172800),
        (6, 21600),
        ("never", float("inf")),
        (None, None),
    ],
)
def test_quiet_for_reads_the_durations_a_person_would_write(tmp_path, value, seconds):
    from fleet import session

    assert session._duration(value, tmp_path / "x.md") == seconds


def test_a_quiet_for_that_is_not_a_duration_says_so(tmp_path):
    from fleet import session

    with pytest.raises(ValueError, match="quiet_for"):
        session._duration("soonish", tmp_path / "x.md")
