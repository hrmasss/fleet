"""Where fleet keeps its own state on the box.

One directory, so `rm -rf` is a complete reset and nothing important hides elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE = Path(os.environ.get("FLEET_STATE", str(Path.home() / ".local/state/fleet")))


def ledger_path() -> Path:
    return STATE / "ledger.db"


def cap_path() -> Path:
    return STATE / "max_running"


def read_cap() -> int | None:
    """The most sessions fleet may have running at once, across every runner.

    ⚠ It is persisted, not passed. A cap that lives only in the argv of `fleet start` is
    not a cap: the runner wrapper calls `fleet tick` itself the moment a session exits, and
    the hourly net calls it too, and neither of those knows what you typed. On the first
    real run that is exactly how a cap of six ended up with seven sessions running.
    """
    try:
        value = int(cap_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def write_cap(value: int | None) -> None:
    p = cap_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if value is None:
        p.unlink(missing_ok=True)
        return
    p.write_text(str(value), encoding="utf-8")
