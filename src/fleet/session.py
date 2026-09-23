"""Reading a session file.

A session is one markdown file with YAML frontmatter, in the workspace under
`todo/sessions/`. The frontmatter is the truth about state; the body carries the brief and
the acceptance criteria as a checklist.

fleet reads these. It does not own them and it does not write them during phase 1 — the
pipeline rules in the workspace say a session closes only with per-criterion evidence, and
a tool that ticks boxes on a session's behalf would be defeating the only check that ever
caught anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
CRITERION = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$", re.MULTILINE)
CRITERIA_HEADING = re.compile(r"^##\s+Acceptance criteria\s*$", re.MULTILINE | re.IGNORECASE)
NEXT_HEADING = re.compile(r"^##\s+", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Criterion:
    text: str
    ticked: bool


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    path: Path
    status: str
    owner: str | None
    prs: tuple[int, ...]
    runner: str | None
    model: str | None
    kind: str
    criteria: tuple[Criterion, ...]
    title: str = ""
    quiet_for: float | None = None
    """Seconds this session may go without new output before it counts as stuck.

    `None` takes the queue's default. `float('inf')` is `quiet_for: never`, for a
    watcher that is expected to sit silent until something happens."""

    @property
    def unticked(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if not c.ticked)

    @property
    def walk(self) -> bool:
        """A flow audit, not a change.

        ⚠ A walk produces findings in the register; it does not produce a pull request, and
        judging it by whether production runs its merge commit would mark every audit this
        project has ever done as unfinished. Declared as `kind: walk` in frontmatter.
        """
        return self.kind == "walk"

    @property
    def closed(self) -> bool:
        return self.status.strip().lower() == "done"


def _criteria_block(body: str) -> str:
    """Only the Acceptance criteria section.

    Session bodies carry other checklists — progress logs, discovered notes — and counting
    those as acceptance criteria makes every session look incomplete forever.
    """
    m = CRITERIA_HEADING.search(body)
    if not m:
        return ""
    rest = body[m.end() :]
    nxt = NEXT_HEADING.search(rest)
    return rest[: nxt.start()] if nxt else rest


def _prs(raw: object, path: Path) -> tuple[int, ...]:
    """PR numbers, however they were written.

    `prs: [524]`, `prs: 524` and `prs: ["#524"]` all mean the same thing. The bare
    `prs: [#524]` form cannot reach here because YAML eats it as a comment — that one is
    caught in `parse` and reported.
    """
    if raw is None or raw == "":
        return ()
    items = raw if isinstance(raw, list) else [raw]
    out: list[int] = []
    for item in items:
        s = str(item).strip().lstrip("#")
        if not s:
            continue
        if not s.isdigit():
            raise ValueError(f"{path.name}: {item!r} in `prs` is not a PR number")
        out.append(int(s))
    return tuple(out)


def parse(text: str, path: Path) -> Session:
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path}: no frontmatter, so nothing here is a session file")

    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        # Session files are written by hand and by agents, and they get this wrong.
        # `prs: [#526]` is the one seen in the wild — `#` opens a YAML comment, so the
        # list never closes. Say what is wrong rather than dying in the parser.
        detail = str(e).replace("\n", " ").strip()
        raise ValueError(f"{path.name}: frontmatter is not valid YAML — {detail}") from e
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name}: frontmatter is not a mapping")

    body = text[m.end() :]
    criteria = tuple(
        Criterion(text=t.strip(), ticked=mark.lower() == "x")
        for mark, t in CRITERION.findall(_criteria_block(body))
    )

    return Session(
        id=str(meta.get("id") or path.stem),
        path=path,
        status=str(meta.get("status") or "unknown"),
        owner=(str(meta["owner"]) if meta.get("owner") else None),
        prs=_prs(meta.get("prs"), path),
        runner=(str(meta["runner"]) if meta.get("runner") else None),
        model=(str(meta["model"]) if meta.get("model") else None),
        kind=str(meta.get("kind") or "fix").strip().lower(),
        criteria=criteria,
        title=str(meta.get("title") or ""),
        quiet_for=_duration(meta.get("quiet_for"), path),
    )


def _duration(value: object, path: Path) -> float | None:
    """`90m`, `6h`, `2d` or `never` into seconds. A bare number is hours.

    Hours for a bare number because that is the unit anyone reaches for when saying how
    long a job may go quiet; seconds would make `quiet_for: 6` a six-second leash.
    """
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"never", "none", "off", "inf"}:
        return float("inf")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        if text[-1] in units:
            return float(text[:-1]) * units[text[-1]]
        return float(text) * 3600
    except (ValueError, IndexError) as e:
        raise ValueError(
            f"{path.name}: quiet_for {value!r} is not a duration like 90m, 6h or never"
        ) from e


def load(path: Path) -> Session:
    return parse(path.read_text(encoding="utf-8"), path)


def find(sessions_dir: Path, session_id: str) -> Path:
    """Resolve `ux-123` to its file, by id prefix.

    Files are named `<id>-<slug>.md`, so a bare id is enough and is what a human types.
    """
    exact = sessions_dir / f"{session_id}.md"
    if exact.is_file():
        return exact
    hits = sorted(sessions_dir.glob(f"{session_id}-*.md"))
    if not hits:
        raise FileNotFoundError(f"no session file for {session_id!r} in {sessions_dir}")
    if len(hits) > 1:
        names = ", ".join(p.name for p in hits)
        raise ValueError(f"{session_id!r} matches more than one file: {names}")
    return hits[0]
