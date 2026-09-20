# Design

Why fleet is shaped the way it is. Every constraint here came from something that broke on
`the box` between 2026-09-15 and 2026-09-19, not from a preference.

## The problem

Three agent accounts sat idle with quota remaining while a dozen briefed sessions waited.
Nothing counted free slots, so nothing knew when one came free; nothing fired when a session
ended, so the next one started whenever a human noticed.

Separately and worse: five sessions in one week merged a green PR and stopped without
deploying it. Every brief named the step. Four green PRs once sat unmerged for twenty hours.
Production ran four commits behind while every report said shipped.

## The two rules everything else follows from

**The foreman never holds the loop.** The queue calls it like a function: one evidence
packet in, one verdict out, the process exits. It owns no state, so a crash mid-judgement
costs a retry and nothing more. An earlier design where an LLM carried queue state in its
context and woke on a timer died twice on session restart and took the queue with it.

**The queue never names a runner.** It counts free slots and hands a session to an adapter.
Which binary that adapter starts is the adapter's business.

## Which runner a session goes to

| Runner | Auto-claims | Ceiling | Gate on quota |
|---|---|---|---|
| agy | **yes, the default** | 2 per profile x 3 profiles | 5h bucket under 10% parks the profile |
| claude | no, on request | 2 | any window under 25% left takes the ceiling to 0 |
| cursor | no, on request | 1 | none possible — it meters in money, not a fraction |

A session that names no runner goes to agy and waits for agy however long that takes. It
does not spill onto claude or cursor. Those two are the tools the operator also works in, and a
queue that drains them to save some waiting has taken their terminal away. `fleet add ux-131
--runner claude` is how a session reaches one, and that choice sticks across a requeue.

`auto` is declared on the adapter, not checked by name in the scheduler.

## Two allowances per agy account

An agy account meters **Gemini** and **Claude/GPT** as separate pools of the same size.
`agy models` lists `gemini-*`, `claude-*` and `gpt-*`; the quota API returns them as two
groups. So the model a session asks for is not a preference, it is which allowance the work
spends, and an account with an empty Gemini weekly can still take a Claude session.

```
fleet add ux-131 --model claude-sonnet-4-6     # the Claude/GPT pool
fleet add ux-132                               # the default model, so the Gemini pool
```

| | models | gated on |
|---|---|---|
| Gemini pool | `gemini-3.8-flash-*`, `gemini-3.1-pro-*` | `gemini-5h`, `gemini-weekly` |
| Claude/GPT pool | `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium` | `3p-5h`, `3p-weekly` |

`Adapter.capacity(model)` takes the model for this reason, and the mapping from an id to a
pool is the adapter's business — nothing above `adapters/` knows that `claude-sonnet-4-6`
is not a Gemini model. An unrecognised id counts as third-party, because a new Gemini model
misread that way wastes a pool nothing is using, while a new Claude model misread the other
way dispatches against an allowance that may be empty.

## Two lines inside a cursor plan

Cursor splits its included allowance the same way, into `auto` and `api`, metered apart:

| | models | gated on |
|---|---|---|
| auto | `default`, composer, grok, vega | the `auto` line |
| api | Claude, GPT, Gemini | the `api` line |

The membership list is not guessed. `GetCurrentPeriodUsage` returns `autoBucketModels`
beside the numbers, and that is what the adapter asks. agy earns a prefix test because its
three families are stable and vendor-named; Cursor renames its house models often enough
that a prefix test would rot without anyone noticing.

⚠ An empty list means the call failed, not that nothing is an auto model, so an unknown
model reads as `auto` and gates on the normally-emptier line. An unknown holds work back
rather than spending a pool we cannot see.

**On-demand is a wall, not a pool.** With it off — which it is on both accounts — a spent
plan is a clean stop rather than a bill. It shows on the board as a yes/no.

claude has one allowance and takes `--model` for quality, not for a second pool.

## Parts

```
 you ──► fleet ──► adapter ──► session
           │                      │ exits
           │                      ▼
           │                  gate 1 · mechanical, free
           │                      │ not clean
           │                      ▼
           │                   foreman · stateless, one verdict
           │                      │
           ◄──────────────────────┤ relaunch / answer / escalate
                                  ▼
                            board · digest
```

| Part | Job | Knows about runners |
|---|---|---|
| ledger | Slots, claims, attempts, verdicts. Single writer, flock'd. | no |
| adapters | Launch, capacity, liveness, nudge. | **yes, only here** |
| gate | Merged, built, running, recorded. | no |
| foreman | Judges what the gate cannot. | no |
| board | Read-only TUI over `fleet status --json`. | no |

## The gate

Four mechanical checks, no tokens spent, each one catching a failure that has happened:

| Check | Via | Catches |
|---|---|---|
| PR merged | `gh pr view` | A green PR left open for twenty hours |
| Image built | `gh run list` | An `images` run cancelled by the next merge, which retags forward and ships stale code while the deploy reports success |
| Running tag | `docker ps` over ssh | Production four commits behind with nothing reporting it |
| Session closed | frontmatter | Work done, record never written, board lies |

A failed check does not free the slot. The session is relaunched with a continuation naming
the exact step it skipped.

## The verdicts

Nine, fixed, each mapping to exactly one action. See `src/fleet/foreman/verdicts.py` — the
table there is the contract and the tests pin it. Six resolve without a human, one is gated
on the foreman's autonomy rung, two always escalate.

A fixed vocabulary is the point. A free-text opinion cannot drive a state machine, and the
moment a verdict means "it depends" the queue starts guessing.

## Autonomy is earned

The foreman ships switched off and is turned up only when a week of its verdicts agrees with
The operator's own reading.

| Rung | It does |
|---|---|
| `observe` | Judges everything, writes verdicts to the ledger and the board, acts on nothing |
| `act` | The six mechanical verdicts go automatic. Decisions and divergence still escalate |
| `decide` | `needs-decision` goes automatic, every answer cited and logged |

`fleet foreman --autonomy` refuses over SSH. A session asking for more trust is not evidence
it deserves more.

## What the foreman may not do

It never writes code, touches a worktree, opens a PR, merges or deploys. It may relaunch a
session with a continuation, but only one that references the original brief and names a
gap — it cannot restate scope or add requirements. If a gap cannot be expressed that way the
verdict is `diverged` and it escalates.

It answers a question only by citing the brief, `context/platform.md`, the SRS,
`todo/FINDINGS.md`, or a shipped decision recorded in a docstring. No citation, no decision.
The citation is written into the session file so every call made on the operator's behalf is
auditable.

It may requeue, park and reorder what is already in the queue. **It may not add work.**

## Notification

Sessions do not message WhatsApp at all — that line comes out of the brief template for
every runner. Thirty sessions each writing to one thread, none of them aware of the others,
is why the thread stopped being read.

Only the foreman speaks, twice: immediately on `diverged` or `blocked-external`, one line
with the exact ask; and once at end of day with what shipped, what is running, what needs
him, every decision it made on their behalf with its citation, and the findings-register
delta.

The digest runs as a `hermes cron` job. That scheduler is already live on the box.

## Brakes

Production has been customer-facing since 2026-09-15, so a bad deploy is an outage.

- A hard cap on relaunch attempts per session
- A cap on deploys per hour
- A kill file that stops all dispatch while it exists

`fleet brake` refuses over SSH. The brake must work before the automation does.

## Known weaknesses

**The foreman is an LLM judging an LLM.** Same family, same blind spots. Mitigated by never
treating the executor's self-report as evidence — inputs are merge state, running image tag,
diff and log, and it is told explicitly that a ticked box proves nothing. A real difference,
not a guarantee.

**Writing a continuation is writing a brief**, which is judgement work. Bounded by template,
and the escape hatch is `diverged`.

**The queue can outrun a human.** Hence the brakes above, which ship before the autonomy
does.
