"""The CLI surface is a design artifact, and the permission wall is enforced here."""

import pytest

from fleet.cli import LOCAL_ONLY, PHASES, VERBS, build_parser, main


def test_every_verb_declares_a_phase():
    assert set(VERBS) == set(PHASES)


def test_every_mutation_is_refused_over_ssh(monkeypatch):
    from fleet.cli import MUTATIONS, build_parser, walled

    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 51000 10.0.0.2 22")
    cases = {
        ("foreman", "autonomy"): ["foreman", "--autonomy", "act"],
        ("brake", "stop"): ["brake", "--stop", "outage"],
        ("brake", "release"): ["brake", "--release"],
    }
    for verb, flags in MUTATIONS.items():
        for flag in flags:
            argv = cases[(verb, flag)]
            assert walled(build_parser().parse_args(argv)), f"{argv} must be refused"


def test_reading_is_never_refused_over_ssh(monkeypatch):
    """Walling the whole verb was tried twice and blocked reading the rung and the brake
    state from the workstation, for no safety gained."""
    from fleet.cli import MUTATIONS, build_parser, walled

    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 51000 10.0.0.2 22")
    for verb in MUTATIONS:
        assert walled(build_parser().parse_args([verb])) is None


def test_moving_the_rung_is_refused_over_ssh(monkeypatch, capsys):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 51000 10.0.0.2 22")
    assert main(["foreman", "--autonomy", "act"]) == 4
    assert "on its own" in capsys.readouterr().err


def test_reading_the_rung_is_not_refused_over_ssh(monkeypatch):
    """The wall is on the mutation, not the noun. Refusing a read made the verb useless
    remotely for no safety gained."""
    from fleet.cli import build_parser, walled

    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 51000 10.0.0.2 22")
    assert walled(build_parser().parse_args(["foreman"])) is None
    assert walled(build_parser().parse_args(["foreman", "--judge", "ux-1"])) is None


def test_the_wall_is_not_in_the_way_at_the_box(monkeypatch):
    from fleet.cli import build_parser, walled

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    for argv in (["brake"], ["foreman", "--autonomy", "act"]):
        assert walled(build_parser().parse_args(argv)) is None


def test_every_declared_verb_is_built():
    """Four phases in. Nothing is left stubbed, so nothing should claim a future phase."""
    from fleet.cli import BUILT

    assert BUILT == VERBS.keys()


def test_bare_invocation_prints_help(capsys):
    assert main([]) == 0
    assert "fleet" in capsys.readouterr().out


def test_version_exits_clean():
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--version"])
    assert e.value.code == 0


def test_the_wall_is_mutations_only():
    """No verb is refused outright. Walling the noun was tried twice and both times it
    blocked a read from the workstation for no safety gained."""
    from fleet.cli import MUTATIONS

    assert LOCAL_ONLY == frozenset()
    assert set(MUTATIONS) == {"foreman", "brake"}
    assert set(MUTATIONS) <= VERBS.keys()
