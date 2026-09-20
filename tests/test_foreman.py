"""The foreman, with no model behind it.

These are mostly tests of what it refuses to do. A judge that can be talked into something
is worse than no judge, and every rule here exists because the alternative is an autonomous
loop acting on a sentence.
"""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from fleet.foreman import autonomy
from fleet.foreman.judge import Foreman, parse, system_prompt
from fleet.foreman.packet import Packet
from fleet.foreman.verdicts import Authority, Verdict


def answer(**kw) -> str:
    """What `claude -p --output-format json` actually hands back."""
    return json.dumps({"result": json.dumps(kw)})


def packet() -> Packet:
    return Packet(
        session="ux-1",
        brief="do the thing",
        claims="[x] one",
        evidence="merged: pass",
        log_tail="...",
        attempts=1,
        previous_note=None,
    )


def foreman_returning(*answers: str) -> Foreman:
    calls = list(answers)

    def runner(model, system, prompt):
        return 0, calls.pop(0)

    return Foreman(runner=runner)


# --- parsing ----------------------------------------------------------------------------


def test_a_clean_verdict_parses():
    j = parse(answer(verdict="shipped", note="everything checks out"))
    assert j.verdict is Verdict.SHIPPED
    assert j.usable


def test_an_unknown_verdict_is_not_a_verdict():
    j = parse(answer(verdict="looks-fine-to-me", note="trust me"))
    assert j.verdict is None
    assert not j.usable


def test_prose_instead_of_json_is_not_a_verdict():
    j = parse("I think this session probably shipped, honestly.")
    assert j.verdict is None


def test_json_buried_in_prose_is_still_read():
    j = parse('Here is my answer:\n{"verdict": "incomplete", "note": "two gaps"}\nThanks.')
    assert j.verdict is Verdict.INCOMPLETE


def test_an_uncited_decision_is_downgraded_not_trusted():
    """Rule 3. A foreman answering without a source is a foreman inventing product
    decisions on the operator's behalf."""
    j = parse(
        answer(
            verdict="needs-decision",
            note="it asked about the pill behaviour",
            answer="make them toggle",
        )
    )
    assert j.verdict is Verdict.BLOCKED_EXTERNAL
    assert "cited nothing" in j.note


def test_a_cited_decision_survives():
    j = parse(
        answer(
            verdict="needs-decision",
            note="asked which key to use",
            citation="ux-123 brief: must drive listingUrlKeys",
            answer="use the existing listing params",
        )
    )
    assert j.verdict is Verdict.NEEDS_DECISION
    assert j.citation and j.answer


# --- the second opinion -------------------------------------------------------------------


def test_a_cheap_verdict_is_not_second_guessed():
    f = foreman_returning(answer(verdict="shipped", note="fine"))
    assert f.judge(packet()).verdict is Verdict.SHIPPED


def test_divergence_gets_a_stronger_model():
    f = foreman_returning(
        answer(verdict="diverged", note="built something else"),
        answer(verdict="diverged", note="confirmed, it rewrote the sidebar"),
    )
    j = f.judge(packet())
    assert j.verdict is Verdict.DIVERGED
    assert j.model == "opus"


def test_a_second_opinion_that_disagrees_wins_and_says_so():
    f = foreman_returning(
        answer(verdict="diverged", note="looks wrong"),
        answer(verdict="incomplete", note="no, it just did not finish"),
    )
    j = f.judge(packet())
    assert j.verdict is Verdict.INCOMPLETE
    assert "diverged" in j.note, "the disagreement must be visible to a human"


def test_a_failed_confirmation_keeps_the_first_answer_but_flags_it():
    calls = [(0, answer(verdict="diverged", note="looks wrong")), (1, "rate limited")]

    def runner(model, system, prompt):
        return calls.pop(0)

    j = Foreman(runner=runner).judge(packet())
    assert j.verdict is Verdict.DIVERGED
    assert "unconfirmed" in j.note


def test_a_model_that_will_not_run_is_not_a_verdict():
    j = Foreman(runner=lambda m, s, p: (127, "claude: not found")).judge(packet())
    assert j.verdict is None


# --- the prompt ---------------------------------------------------------------------------


def test_the_prompt_lists_every_verdict():
    text = system_prompt()
    for v in Verdict:
        assert v.value in text


def test_the_prompt_says_a_ticked_box_is_not_evidence():
    assert "ticked acceptance criterion is not evidence" in system_prompt()


def test_the_packet_separates_evidence_from_claims():
    rendered = packet().render()
    assert rendered.index("EVIDENCE") < rendered.index("CLAIMS")
    assert "A ticked box proves nothing" in rendered


# --- the autonomy ladder --------------------------------------------------------------------


@pytest.mark.parametrize("verdict", list(Verdict))
def test_observe_acts_on_nothing(verdict):
    assert autonomy.may_act(autonomy.Rung.OBSERVE, verdict) is False


@pytest.mark.parametrize("verdict", list(Verdict))
def test_no_rung_ever_automates_what_belongs_to_him(verdict):
    """diverged and blocked-external are the floor. No dial moves them."""
    from fleet.foreman.verdicts import RULES

    if RULES[verdict].authority is not Authority.HUMAN:
        return
    for rung in autonomy.Rung:
        assert autonomy.may_act(rung, verdict) is False


def test_act_takes_the_mechanical_six_and_not_the_decision():
    rung = autonomy.Rung.ACT
    assert autonomy.may_act(rung, Verdict.MERGED_NOT_DEPLOYED)
    assert autonomy.may_act(rung, Verdict.SHIPPED)
    assert not autonomy.may_act(rung, Verdict.NEEDS_DECISION)
    assert not autonomy.may_act(rung, Verdict.DIVERGED)


def test_decide_adds_the_gated_one_only():
    rung = autonomy.Rung.DECIDE
    assert autonomy.may_act(rung, Verdict.NEEDS_DECISION)
    assert not autonomy.may_act(rung, Verdict.BLOCKED_EXTERNAL)


def test_it_ships_at_observe(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "STATE", tmp_path)
    assert autonomy.read() is autonomy.Rung.OBSERVE


def test_an_unreadable_rung_file_falls_back_rather_than_escalating(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "STATE", tmp_path)
    (tmp_path / "autonomy").write_text("maximum", encoding="utf-8")
    assert autonomy.read() is autonomy.Rung.OBSERVE, "a typo must never grant autonomy"


def test_the_packet_goes_in_on_stdin_not_as_an_argument(monkeypatch):
    """`--disallowed-tools` is variadic, so a positional prompt after it is read as one
    more tool name and claude refuses with "Input must be provided". Found live."""
    import subprocess

    from fleet.foreman import judge

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw.get("input")

        class R:
            returncode = 0
            stdout = '{"result": "{}"}'
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    judge.claude_runner("sonnet", "rules", "THE PACKET")
    assert seen["input"] == "THE PACKET"
    assert "THE PACKET" not in seen["cmd"]
    assert seen["cmd"][-1] in judge.DENIED, "the deny list must be last, with nothing after it"


def test_the_prompt_forbids_overturning_a_passing_check():
    """The foreman called a shipped session not-deployed because production ran a later
    commit than the merge. The gate had already tested ancestry and passed it."""
    text = system_prompt()
    assert "settled fact" in text
    assert "later deploy" in text


# --- quota readings -----------------------------------------------------------------------


def test_claude_pct_is_usage_and_is_flipped_to_remaining(tmp_path):
    """agy reports remaining, claude reports used. Reversing it would show a spent account
    as fresh, which is the one error here that would matter."""
    import json as _json

    from fleet.adapters import quota

    f = tmp_path / "limit-state.json"
    f.write_text(
        _json.dumps({"ts": 0, "seven_day": {"pct": 90, "resets_at": 123.0}}), encoding="utf-8"
    )
    buckets = {b.id: b for b in quota.claude(f)}
    assert buckets["seven_day"].pct == 10, "90% used is 10% left"


def test_both_claude_windows_are_always_listed(tmp_path):
    """Dropping a window with no reading makes it look as though the runner has no such
    limit. It is listed with no figure instead."""
    import json as _json

    from fleet.adapters import quota

    f = tmp_path / "limit-state.json"
    f.write_text(_json.dumps({"ts": 0, "seven_day": {"pct": 51}}), encoding="utf-8")
    ids = [b.id for b in quota.claude(f)]
    assert ids == ["five_hour", "seven_day"]
    assert quota.claude(f)[0].remaining is None


def test_agy_reads_groups_and_reset_times(tmp_path):
    import json as _json

    from fleet.adapters import quota

    d = tmp_path / "antigravity-cli"
    d.mkdir()
    (d / "quota-cache.json").write_text(
        _json.dumps(
            {
                "groups": [
                    {
                        "display_name": "Gemini Models",
                        "buckets": [
                            {
                                "bucket_id": "gemini-5h",
                                "remaining_fraction": 0.5,
                                "reset_time": "2026-09-19T18:39:19Z",
                            }
                        ],
                    },
                    {
                        "display_name": "Claude and GPT models",
                        "buckets": [{"bucket_id": "3p-weekly", "remaining_fraction": 1}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    buckets = quota.agy(tmp_path)
    assert [b.id for b in buckets] == ["gemini-5h", "3p-weekly"]
    assert buckets[0].pct == 50 and buckets[0].resets_at
    assert buckets[1].group == "Claude and GPT models"


def test_cursor_reports_nothing_only_when_it_cannot_read(monkeypatch, tmp_path):
    """Cursor does expose its limits — through the dashboard API, not the CLI. Nothing is
    reported only when there is no token to ask with."""
    from fleet.adapters import quota

    monkeypatch.setattr(quota, "CURSOR_ACCOUNTS", tmp_path / "nobody")
    assert quota.cursor("c1", cache=tmp_path / "c.json") == []
    assert "not signed in" in quota.CURSOR_REASON


def test_cursor_meters_auto_and_api_apart(monkeypatch, tmp_path):
    """⚠ Cursor's plan has two lines and they are metered separately. An earlier reading
    took `noUsageBasedAllowed` and a null `customerBalance` to mean one allowance; those
    describe on-demand billing, which is paying *past* the plan, not the split inside it.
    On 2026-09-20 c1 had spent 36% of its auto line and none of its api one."""
    from fleet.adapters import quota

    monkeypatch.setattr(quota, "CURSOR_ACCOUNTS", tmp_path)
    auth = tmp_path / "c2" / "cursor" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"accessToken": "x"}', encoding="utf-8")
    monkeypatch.setattr(
        quota,
        "_cursor_call",
        lambda path, token, body=None: {
            "/aiserver.v1.DashboardService/GetCurrentPeriodUsage": {
                "billingCycleEnd": "1790495081000",
                "planUsage": {
                    "totalSpend": 1625,
                    "limit": 2000,
                    "autoPercentUsed": 36.11,
                    "apiPercentUsed": 0,
                    "totalPercentUsed": 18.05,
                },
                "autoBucketModels": ["default", "composer-2.5", "cursor-grok-4.5"],
            },
            "/aiserver.v1.DashboardService/GetHardLimit": {"noUsageBasedAllowed": True},
        }.get(path),
    )

    buckets = {b.id: b for b in quota.cursor("c2", cache=tmp_path / "cache.json")}
    assert set(buckets) == {"included", "auto", "api", "on_demand"}
    assert round(buckets["auto"].remaining, 4) == 0.6389
    assert buckets["api"].remaining == 1.0, "the api line is the untouched one"
    assert buckets["included"].detail.startswith("$16.25 spent of ~$90 across both lines")
    assert buckets["on_demand"].remaining is None, "a wall is not a pool"
    assert "hard stop" in buckets["on_demand"].detail


def test_cursors_own_models_bill_the_auto_line_whatever_the_list_says(monkeypatch):
    """⚠ `autoBucketModels` is incomplete. On 2026-09-20 it named every grok 4.5 variant
    and no 4.6 one, while c2's billing put `cursor-grok-4.6-high` in the auto bucket: auto
    was 81.667% of a $45 line, which is $36.75, which is grok 4.6 at $20.91 plus composer
    2.5 at $15.84 to the cent. The list and the prefixes have each been wrong alone, so
    both are consulted."""
    from fleet.adapters import quota, runners

    monkeypatch.setattr(
        quota, "cursor_auto_models", lambda p="c1", cache=None: ["default", "composer-2.5"]
    )
    c = runners.Cursor()
    assert c.pool("composer-2.5") == "auto", "named by the server"
    assert c.pool("cursor-grok-4.6-high") == "auto", "a house model the list forgot"
    assert c.pool("vega-high") == "auto"
    assert c.pool(None) == "auto", "no model means the default, which is a house model"

    assert c.pool("claude-sonnet-5-thinking-high") == "api"
    assert c.pool("gpt-5.6-luna-high") == "api"
    assert c.pool("gemini-3.7-flash-high") == "api"


def test_a_vendor_model_is_still_api_when_the_server_list_is_missing(monkeypatch):
    """A failed call must not reclassify Claude as a house model. The prefixes decide on
    their own, and they are the half that does not depend on the network."""
    from fleet.adapters import quota, runners

    monkeypatch.setattr(quota, "cursor_auto_models", lambda p="c1", cache=None: [])
    c = runners.Cursor()
    assert c.pool("claude-opus-5-thinking-high") == "api"
    assert c.pool("cursor-grok-4.6-high") == "auto"


def test_the_cursor_reading_is_cached(monkeypatch, tmp_path):
    """The board refreshes every five seconds; that is no rate to poll somebody's API at."""
    import json as _json
    import time as _time

    from fleet.adapters import quota

    cache = tmp_path / "cache.json"
    cache.write_text(
        _json.dumps(
            {
                "ts": _time.time(),
                "buckets": [
                    {
                        "id": "included",
                        "label": "Included",
                        "remaining": None,
                        "resets_at": None,
                        "group": "",
                        "brief": "$1.00",
                        "detail": "cached",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    called = []
    monkeypatch.setattr(quota, "_cursor_live", lambda profile="c1": called.append(1) or [])
    buckets = quota.cursor("c1", cache=cache)
    assert not called, "a fresh cache must not hit the network"
    assert buckets[0].detail == "cached"


def test_an_unmetered_agy_account_does_not_read_as_full(tmp_path):
    """a2 and a3 report every bucket at 100% with the whole window still to run, at every
    poll, while their sessions burn quota. Showing that as a full tank is the lie that
    matters — it is the reading a dispatcher would route more work at."""
    import json as _json
    import time as _time

    from fleet.adapters import quota

    now = _time.time()
    d = tmp_path / "antigravity-cli"
    d.mkdir()
    (d / "quota-cache.json").write_text(
        _json.dumps(
            {
                "ts": now,
                "groups": [
                    {
                        "display_name": "Gemini Models",
                        "buckets": [
                            {
                                "bucket_id": "gemini-5h",
                                "window": "5h",
                                "remaining_fraction": 1,
                                "reset_time": _stamp(now + 5 * 3600),
                            },
                            {
                                "bucket_id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": 1,
                                "reset_time": _stamp(now + 7 * 86400),
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    buckets = quota.agy(tmp_path)
    assert all(b.remaining is None for b in buckets)
    assert all(b.detail == quota.UNTRACKED for b in buckets)


def test_a_tracked_account_is_left_alone(tmp_path):
    """main has 80% of its weekly gone, so it is metered and reads normally — including its
    own freshly reset five-hour bucket, which on its own looks identical to an unmetered one."""
    import json as _json
    import time as _time

    from fleet.adapters import quota

    now = _time.time()
    d = tmp_path / "antigravity-cli"
    d.mkdir()
    (d / "quota-cache.json").write_text(
        _json.dumps(
            {
                "ts": now,
                "groups": [
                    {
                        "display_name": "Gemini Models",
                        "buckets": [
                            {
                                "bucket_id": "gemini-5h",
                                "window": "5h",
                                "remaining_fraction": 1,
                                "reset_time": _stamp(now + 5 * 3600),
                            },
                            {
                                "bucket_id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.80,
                                "reset_time": _stamp(now + 2 * 86400),
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    by = {b.id: b for b in quota.agy(tmp_path)}
    assert by["gemini-5h"].pct == 100
    assert by["gemini-weekly"].pct == 80


def _stamp(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_each_cursor_account_gets_its_own_cache_file(tmp_path, monkeypatch):
    """⚠ One shared cache would serve the first account's spend for whichever profile
    asked, for the next ten minutes. cursor-agent cannot tell two accounts apart from the
    outside, so nothing downstream would have caught it."""
    from fleet.adapters import quota

    monkeypatch.setattr(quota, "STATE", tmp_path)
    monkeypatch.setattr(quota, "CURSOR_ACCOUNTS", tmp_path / "nobody")
    quota.cursor("c1")
    quota.cursor("c2")
    # No token anywhere, so neither writes; the point is that they ask for different paths.
    assert quota.cursor.__defaults__[0] == "c1", "c1 stays the default account"
    assert quota.cursor_auth("c2").parts[-4:] == ("nobody", "c2", "cursor", "auth.json")
    assert quota.cursor_auth("c1") != quota.cursor_auth("c2")


def test_a_scaffolded_cursor_profile_is_not_a_signed_in_one(tmp_path, monkeypatch):
    """A profile directory with no credential must not be offered as an account: it would
    report an empty tank rather than no tank, and the queue would dispatch into it."""
    from fleet.adapters import quota

    monkeypatch.setattr(quota, "CURSOR_ACCOUNTS", tmp_path)
    (tmp_path / "c1" / "cursor").mkdir(parents=True)
    (tmp_path / "c1" / "cursor" / "auth.json").write_text("{}", encoding="utf-8")
    (tmp_path / "c2" / "cursor").mkdir(parents=True)
    assert quota.cursor_profiles() == ["c1"]
