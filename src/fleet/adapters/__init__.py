"""Adapter registry.

All three land together on purpose: building one and adding the others later makes the
first one special, and "special" is how a runner name ends up in the scheduler.

Which runners exist, how many sessions each account takes and how close to empty an
allowance may run are per-installation facts, so they come from the config file. A box
with no cursor account should not carry a cursor row reporting that it cannot find the
binary, every five seconds, forever.
"""

from fleet import config
from fleet.adapters.base import Adapter, Capacity, Handle, State
from fleet.adapters.runners import Agy, Claude, Cursor

__all__ = ["Adapter", "Capacity", "Handle", "State", "Agy", "Claude", "Cursor", "registry"]

KNOWN = ("agy", "claude", "cursor")


def registry() -> dict[str, Adapter]:
    """Every runner this installation dispatches to.

    An adapter that is enabled but has no credential still appears — its `capacity()`
    reports zero with a reason, which the board shows, rather than the runner silently
    not existing. An adapter that is *not* enabled does not appear at all, which is a
    different statement: not "broken" but "not part of this setup".
    """
    out: dict[str, Adapter] = {}
    for name in config.enabled_runners(KNOWN):
        opts = config.runner(name)
        if name == "agy":
            out[name] = Agy(
                per_account=int(opts.get("per_account", 2)),
                floor=float(opts.get("floor", 0.10)),
            )
        elif name == "claude":
            out[name] = Claude(
                concurrent=int(opts.get("concurrent", 2)),
                floor=float(opts.get("floor", 0.25)),
            )
        elif name == "cursor":
            out[name] = Cursor(
                concurrent=int(opts.get("concurrent", 1)),
                floor=float(opts.get("floor", 0.05)),
            )
    return out
