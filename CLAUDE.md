# fleet — agent instructions

A dispatch queue for coding agent sessions, run on the box where the agents live and driven from wherever you type.
Read `README.md` for what it is and `docs/design.md` for why it is shaped this way.

## The rules that are not negotiable

- **Nothing outside `src/fleet/adapters/` may branch on a runner name.** No `if runner ==
  "agy"` in the queue, the gate, the foreman or the board. The adapter protocol exists to
  remove that coupling and it comes back the moment one exception is allowed.
- **The foreman never holds the loop.** It is called, it returns one verdict, it exits. It
  owns no state and no background task. An earlier design where an LLM carried queue state
  in its context died twice on session restart and took the queue with it.
- **The foreman returns a `Verdict` and nothing else.** Free text goes in a `note` field
  that reaches the session file and the board. It never reaches a control decision.
- **The permission wall lives in `cli.py`, not in a document.** A skill document is a
  request; a refusal in the CLI is a rule. `LOCAL_ONLY` verbs refuse when the call arrived
  over SSH.
- **The board reads state only through `fleet status --json`.** It never imports the
  ledger. That contract is what keeps the Go half disposable.
- **The ledger is local to the box and is the dispatch record. The session file in
  the workspace repo is the work record.** Two different things, never merged — remote
  agents push to that repo too, and a shared git file is not a lock.
- **No secrets in this repo.** Credentials live in `.env`, which is gitignored, or in the
  environment on the box.

## What it dispatches against

Sessions are markdown files with frontmatter, in the workspace repo you configure,
under `todo/sessions/`. `status: claimable` is a ready state, `owner:` is a claim, `runner:`
names a preferred adapter or is absent for queue policy. fleet reads them; it does not own
them.

⚠ **Assume production is live and in front of real users.** fleet can dispatch work
that merges and deploys, so a bad run is an outage rather than a broken demo. Anything here
that can trigger a deploy is capped, and the brake has to work before the automation does.

## Conventions

- Conventional commits, lowercase. No IP assignment line — this is personal tooling.
- Python 3.12 is the floor. Do not use newer syntax.
- `uv` for dependencies, `ruff` for lint and format, `pytest` for tests.
- Docstrings say **why**, and name the failure a rule came from. A comment that only
  restates the code is noise; a comment that records what broke is the reason the next
  session does not break it again.
- Tests pin contracts, not implementations. `tests/test_verdicts.py` and
  `tests/test_adapter_contract.py` exist so phases 2 and 3 cannot drift from the design.
