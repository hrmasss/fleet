"""The verdict table is a contract, so it gets tested like one."""

from fleet.foreman import RULES, Authority, Verdict
from fleet.foreman.verdicts import WHATSAPP_IMMEDIATELY


def test_every_verdict_has_exactly_one_rule():
    assert set(RULES) == set(Verdict)


def test_six_verdicts_resolve_without_a_human():
    auto = [v for v, r in RULES.items() if r.authority is Authority.AUTO]
    assert len(auto) == 6


def test_only_two_verdicts_interrupt_him():
    assert WHATSAPP_IMMEDIATELY == {Verdict.DIVERGED, Verdict.BLOCKED_EXTERNAL}


def test_everything_that_interrupts_him_is_human_authority():
    for v in WHATSAPP_IMMEDIATELY:
        assert RULES[v].authority is Authority.HUMAN


def test_every_rule_says_what_it_saw():
    for v, r in RULES.items():
        assert r.saw.strip(), f"{v} has no evidence description"
