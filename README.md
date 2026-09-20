# fleet

A dispatch queue for coding agent sessions. You fill it; it keeps the runners busy, checks
that finished work actually shipped, and interrupts you only when something is genuinely
blocked.

Runner-agnostic by construction. `agy` (Antigravity), `claude` (Claude Code) and `cursor`
(cursor-agent) are adapters, not assumptions, and nothing outside `src/fleet/adapters/` is
allowed to know which one is in use.

## Why

Three agent accounts sat idle with quota remaining while a dozen briefed sessions waited,
because nothing counted free slots and nothing fired when a session ended. Separately, five
sessions in one week merged a green PR and stopped without deploying it — every brief named
the step, and it kept happening anyway.

So: a queue that starts the next session the moment one finishes, and a gate that refuses
to believe a session is done until production is actually running its code.

## What it does

```sh
fleet add ux-131 ux-132              # queue briefed sessions
fleet start --parallel 6             # drain across every runner with capacity
fleet status                         # who is working, on what, on which account
fleet limits                         # what every account has left, and when it resets
fleet gate ux-131                    # did it truly ship? merged, built, running, recorded
fleet-board                          # the TUI
```

A session is a markdown file in your own repo with acceptance criteria in it. fleet reads
those files and never writes them: the ledger is the dispatch record, the file is the work
record, and keeping them apart is what stops two dispatchers claiming the same session.

## Three ideas it is built on

**The foreman never holds the loop.** When the mechanical gate cannot decide, the queue
calls an LLM like a function — one evidence packet in, one verdict out, process exits. It
owns no state, so a crash mid-judgement costs a retry and nothing more.

**Finished is not the same as shipped.** The gate reads GitHub, production and git, never
the session's own ticked boxes. A merged PR that is not deployed is not done, and a ticked
box with no evidence behind it is worse than an unticked one.

**A model is which allowance you spend.** Agent subscriptions meter more than one pool per
account — Antigravity meters Gemini apart from Claude and GPT, Cursor meters its own models
apart from third-party ones — so `--model` routes to a pool and gates on that pool's number,
not on a single ceiling per account.

## Shape

| Part | Language | Job |
|---|---|---|
| queue | Python | Slots, rotation, launch, the ledger. Deterministic, asks nothing. |
| adapters | Python | The only code that knows what an `agy` or a `claude` is. |
| gate | Python | Merged, built, running, recorded. Mechanical and free. |
| foreman | Python + LLM | Called only when the gate is not clean. Stateless, one verdict. |
| board | Go, bubbletea | Read-only TUI. Talks to the core only through `fleet status --json`. |

The board and the core meet at exactly one place, `fleet status --json`, which is why the
board can be rewritten or thrown away without touching anything that dispatches work.

## Setup

Everything installation-specific lives in one TOML file — repo, containers, hosts, how many
sessions an account takes, how close to empty an allowance may run. Nothing has a working
default: fleet refuses to start rather than guess, because a dispatcher quietly running its
production checks against the wrong containers is worse than one that will not start.

```sh
cp config.example.toml ~/.config/fleet/config.toml
```

`install/README.md` has the rest, including the two-machine setup where sessions run on one
box and you drive it from another over ssh. There is no service, no port and no token: ssh
already exists, and a remote control surface with its own authentication would be a second
thing to get wrong.

## Working on it

```sh
make install     # uv sync
make test        # pytest
make lint        # ruff
make board       # go build the TUI
make ship-board  # cross-compile for the box and scp it
```

Python 3.12 or newer and `uv`. The board needs Go.

## Layout

```
src/fleet/
  cli.py              the verb surface, and the permission wall
  config.py           everything true of one installation rather than of fleet
  gate.py             the four checks
  ledger.py           the single writer, flock'd
  scheduler.py        the tick: reconcile, verify, dispatch
  adapters/base.py    the four-method runner contract
  adapters/quota.py   what each account has left, normalised across runners
  foreman/verdicts.py the nine verdicts and what each one does
board/                the TUI, in Go
config.example.toml   every setting, with why it matters
docs/design.md        why any of this is shaped the way it is
docs/decisions.md     what was chosen, what was rejected, and what was wrong
```

## Reading

`docs/design.md` is the reasoning. `docs/decisions.md` is the log, including the decisions
that turned out to be wrong and what the evidence was that overturned them — a dispatcher
that reports a full tank on an empty account is the kind of bug this project keeps finding,
and the log is mostly about how.
