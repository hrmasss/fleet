# Agent instructions — fleet

**Canonical instructions live in [CLAUDE.md](CLAUDE.md). Read it first and follow it.**
This file exists so non-Claude harnesses (Codex, OpenCode, Cursor, Zed) pick up the same rules.

The short version:

- fleet is a runner-agnostic dispatch queue for coding agent sessions. It runs on the box
  where the agents live and is driven from wherever you type, over SSH.
- **Nothing outside `src/fleet/adapters/` may branch on a runner name.** The adapter protocol
  exists to remove that coupling.
- **The foreman never holds the loop.** Called like a function, returns one `Verdict`, exits.
- **The permission wall is in `cli.py`, not in prose.** `LOCAL_ONLY` verbs refuse over SSH.
- **The board talks to the core only through `fleet status --json`.**
- Python 3.12 floor, `uv`, `ruff`, `pytest`. Conventional
  commits, lowercase, no IP line.
- ⚠ Assume it dispatches against **live production**. Deploy paths are capped and the
  brake has to work before the automation does.

Full reasoning and the failures each rule came from: `docs/design.md`.
