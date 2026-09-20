"""Everything that is true of one installation rather than of fleet.

fleet knows how to dispatch agent sessions and how to check whether work shipped. It does
not know your hostnames, your repo, your container names or how many sessions an account
will take. Those live here, in one TOML file, so the code can be read without guessing
which constants are load-bearing and which are somebody's laptop.

    ~/.config/fleet/config.toml         (or $FLEET_CONFIG)

Nothing has a default that points anywhere real. A missing config is an error with the
path in it, not a silent fallback to whatever machine this was written on — a dispatcher
that quietly runs against the wrong project is worse than one that refuses to start.

Environment variables still win, because the runner wrapper carries its coordinates in the
environment and a tmux pane must not depend on a file being where the dispatcher thought.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get("FLEET_CONFIG", "~/.config/fleet/config.toml")).expanduser()

EXAMPLE = "config.example.toml"


class ConfigError(RuntimeError):
    """Raised with a path and a fix, never just 'invalid config'."""


_cache: dict[str, Any] | None = None


def load(path: Path | None = None) -> dict[str, Any]:
    """The parsed config. Cached, because a tick reads it from several places."""
    global _cache
    if path is None and _cache is not None:
        return _cache
    p = path or CONFIG_PATH
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"no fleet config at {p}\ncopy {EXAMPLE} there and fill in your own coordinates"
        ) from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{p} could not be read: {e}") from None
    if path is None:
        _cache = data
    return data


def reset() -> None:
    """Drop the cache. For tests and for a long-lived process that has been reconfigured."""
    global _cache
    _cache = None


def get(section: str, key: str, default: Any = None, *, env: str | None = None) -> Any:
    """One setting. `env` wins over the file, and the file wins over `default`.

    ⚠ The environment wins on purpose. A runner wrapper exports its coordinates and runs
    in a tmux pane that inherits the tmux *server's* environment, not the dispatcher's, so
    the wrapper has to be able to state them outright rather than trust a file path.
    """
    if env and (raw := os.environ.get(env)):
        return raw
    return load().get(section, {}).get(key, default)


def get_list(section: str, key: str, default: tuple[str, ...] = (), *, env: str | None = None):
    if env and (raw := os.environ.get(env)):
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    value = load().get(section, {}).get(key, default)
    return tuple(value) if isinstance(value, (list, tuple)) else (str(value),)


def require(section: str, key: str, *, env: str | None = None) -> str:
    value = get(section, key, env=env)
    if not value:
        raise ConfigError(
            f"[{section}] {key} is not set in {CONFIG_PATH}"
            + (f" (or ${env})" if env else "")
            + f"\nsee {EXAMPLE}"
        )
    return str(value)


def runner(name: str) -> dict[str, Any]:
    """One runner's settings, or {} when the config says nothing about it."""
    return dict(load().get("runners", {}).get(name, {}))


def enabled_runners(known: tuple[str, ...]) -> tuple[str, ...]:
    """Which runners this installation has. An absent section means the runner is absent:
    a box with no cursor account should not have a cursor row reporting that it cannot
    find the binary, every five seconds, forever."""
    section = load().get("runners", {})
    if not section:
        return known
    return tuple(n for n in known if section.get(n, {}).get("enabled", False))
