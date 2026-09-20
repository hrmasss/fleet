# Decisions

What was chosen, what was rejected, and why. Append; do not rewrite history.

## 2026-09-19 — the queue is ours, not hermes kanban

`hermes kanban` is on the box already: a SQLite board with atomic claim, dependency links,
review states, crash detection and a documented `spawn_fn(task, workspace, board)` injection
point, so pointing it at agy would have been about sixty lines.

**Rejected.** It is a vendored third-party codebase that updates underneath us, it has never
run a task on this box, and its worker contract expects the agent to report its own
completion — which is precisely the thing our agents keep failing to do. The state machine
vocabulary was worth borrowing; the engine was not.

## 2026-09-19 — the foreman is called, it does not run

An LLM holding queue state in its context, waking on a timer, was tried twice on the box.
Both times the watcher died on session restart and sessions sat undispatched overnight.

**Chosen.** The queue is deterministic and calls the foreman as a stateless function: one
evidence packet in, one verdict out, process exits. A crash costs a retry.

## 2026-09-19 — runner-agnostic from the first line

An earlier draft was `agy-queue`, with agy in the name and in the dispatch path.

**Changed.** The adapter protocol has four methods and all three adapters land together in
phase 2. Building one first and adding the others later makes the first one special, which
is the coupling the protocol exists to prevent.

Liveness turned out to be nearly free: ccmux installs hooks into all three agents and each
writes the same marker shape — `agent_type`, `pid`, `session_id`, `state`,
`state_timestamp` — into one directory.

⚠ **But the marker is an optimisation, never a dependency.** On 2026-09-19 eight agy
processes were live on the box and not one had ever written a marker, while claude and
cursor wrote theirs correctly; all three agy profiles declare the hooks and point at scripts
that exist. Cause not chased. `liveness` must always be answerable from pid plus log mtime.

## 2026-09-19 — bubbletea for the board, not Textual

First recommendation was Textual, on the grounds that `textual-serve` gives a browser view
from the same codebase with no second frontend.

**Reversed.** The box is remote, so a browser view means standing up a tunnel to reach a web
server, and a board behind that much friction is a board nobody opens. On merit bubbletea
wins anyway: a static binary, ~10ms startup against Textual's several hundred, and lighter
over SSH where every repaint costs a round trip.

The `fleet status --json` contract is what made the reversal cost one paragraph. Keep it.

## 2026-09-19 — ssh is the control surface, not an API

Local sessions on the workstation drive fleet through `ssh the box fleet …`. No HTTP service, no
port, no token. A remote control surface that needs its own authentication is a second thing
to get wrong, and the key already exists.

The wall between what a local agent may do and what only the operator may do is enforced in
`cli.py` by checking whether the call arrived over SSH — not in the skill document, because
a document is a request and a refusal is a rule.

## 2026-09-19 — three things the box taught us that fixtures never would

Phase 2 passed 61 tests against fakes and then failed three times in a row on the first
live handshake. All three were in the seam between the dispatcher and the shell, which is
exactly where a fake cannot help.

**tmux does not pass your environment.** A tmux session inherits the tmux *server's*
environment, not the environment of the shell that spawned it, and on this box a server is
always already running. Every `FLEET_*` variable the dispatcher had resolved was simply
absent inside the pane, so the wrapper reported into a different ledger than the one
dispatching it. The values now travel in the wrapper itself.

**tmux runs your login shell, which here is zsh.** zsh has no `PIPESTATUS`; it has
`$pipestatus`, one-indexed. So `--rc` arrived empty, argparse rejected the call, and the
exit report vanished. The wrapper is now a real bash script written on dispatch, which
fixes the dialect and removes the quoting problem at the same time.

**An orphan report must be loud.** `fleet event` for a session the ledger had never heard
of returned 0. That is what hid the first bug through an entire debugging round: a
misconfigured wrapper looked exactly like a session that quietly never finished.

## 2026-09-19 — fleet is not the only thing that starts agents here

While dispatching the first real session, eight agy sessions were live on the box that
fleet had never heard of, started by hand and by Hermes. Slot accounting counted only
fleet's own, so it would have stacked its full ceiling of six on top of those eight.

Adapters now report what the OS actually shows running per account, and the scheduler takes
`max(observed, ledger)` — counting fleet's own sessions once and everybody else's as well.
The board shows the same number, because a board that disagrees with the scheduler is a
board nobody trusts.

## 2026-09-19 — no single liveness signal works for all three runners

Found by watching the first real dispatch rather than by testing. A claude session was
demonstrably working while its ccmux marker said `idle` and its log was zero bytes, and
fleet would have reaped it as stalled after thirty minutes.

| Runner | Marker | Log |
|---|---|---|
| agy | hooks configured, never fire — no marker at all | streams, so mtime works |
| claude | only ever writes `idle`; no hook writes `working` | empty until the end: piping stdout is print mode |
| cursor | writes both | untested under load |

So `liveness` now takes three signals and lets working win. A marker is evidence *for*
working and never against it. CPU time from `/proc` is the third, and it is the only one
that holds everywhere, because it asks the kernel instead of the agent — which is what
judging these sessions by hand always came down to.

No baseline yet reads as working, so a fresh dispatch is never reaped for want of a sample.

## 2026-09-19 — the foreman's first real judgement was wrong, at `observe`

Asked to judge `ux-123`, a session that had demonstrably shipped, it answered
`merged-not-deployed`. Production was running `e6dd00a` while the merge commit was
`789c359`, and it compared the two for equality. The gate had already tested ancestry and
passed the check: a later deploy carries the earlier commit.

At rung `act` that verdict would have ordered a pointless redeploy of a live storefront.
It came back at `observe` instead, where it moved nothing. **That is the entire argument
for the rung**, and it paid for itself on the first call.

The fix was not "prompt it better". The evidence was not self-explaining: a passing running
check named a tag and left the reader to infer why a different sha was fine. It now says
"production runs sha-e6dd00a, which contains 789c359". Evidence that requires inference is
evidence that gets misread, by a model or by a person. The prompt separately now says a
check marked `pass` is a settled fact and names this case.

Two smaller things the same session surfaced:

- `--disallowed-tools` is variadic, so a positional prompt after it is swallowed as one
  more tool name. The packet goes in on stdin now, which it should have anyway — it
  carries a whole session file, which is the size that meets argv limits.
- The SSH wall was on the verb `foreman`, which made reading the rung and running a
  read-only judgement impossible from the workstation for no safety gained. **The wall belongs on
  the mutation, not the noun.** Only moving the rung is refused now.

## 2026-09-19 — the board is Go, and that is the only Go

Built on the workstation and cross-compiled, so the box needs no toolchain at all. It talks to the
core only through `fleet status --json` and shells out to fleet verbs for every key, so it
can be rewritten or thrown away without touching anything that dispatches work. That
contract is what made a second language affordable, and it is the reason the earlier
Textual-to-bubbletea reversal cost one paragraph.

Two things it taught us immediately. It polls every five seconds rather than two, because a
status read costs about two seconds — most of it asking `agy-next` for real quota — and a
faster poll would keep the box busy answering a board nobody watches that hard. And it says
"reading the queue" before the first read returns, because an empty board looks exactly
like an empty queue, which is a lie a glance would believe.

## 2026-09-19 — the wall is on the mutation, never the noun

Walling a whole verb was tried twice and was wrong both times. `foreman` was walled, which
blocked reading the autonomy rung from the workstation. Fixed, and then `brake` turned out to have
the same shape: reading the brake state was refused too.

No verb is refused outright now. Only the flags that change what the system may do on its
own — `foreman --autonomy`, `brake --stop`, `brake --release` — are refused over ssh.
Reading is never refused, because refusing a read bought no safety and made the verb
useless from the machine the operator actually sits at.

## 2026-09-19 — there is no deploy cap, and the plan said there would be

fleet never deploys. Agents do. A cap here would have been a number that stopped nothing.

What actually bounds the damage is the attempt cap — how many times fleet may tell an agent
to go and deploy before the session parks — and the kill file, which stops dispatch
entirely. `fleet brake` says so on its own output rather than leaving the earlier claim
standing.

## 2026-09-19 — the first real six-session run found five bugs

128 tests passed before it. None of them would have found these, because all five live in
the seam between the queue and the world.

**A flow audit ships nothing.** Ten of the eleven queueable sessions were audit walks. The
gate would have called every one incomplete, because merged/built/running do not apply to a
session that produces findings rather than a pull request. Sessions now declare
`kind: walk`, explicitly — inferring it from "only `recorded` applies" was tried and was
wrong, since a fix session whose PR was never recorded looks identical from the checks
alone, and that would have marked unshipped work as shipped.

**CPU was sampled from the wrong process.** The handle's pid is the tmux pane's shell,
because the wrapper is what reports the exit — but the wrapper does nothing while the agent
works, so its CPU never advanced. Every claude session would have looked idle within the
hour. The watchdog duly reaped one that had burned 32 seconds and was still going. It sums
the whole process tree under the pane now.

**Requeueing did not stop what it was replacing.** The old agent stayed alive, burning
quota and still holding its worktree, and the relaunch collided: `tmux refused to start:
duplicate session`. Every requeue reaps first — tmux, then the process tree, in that order.

**`needs_you` was a dead end.** The state exists to wait for a human decision, and
`fleet add` is that decision, but it refused to re-add anything not `done`. A session put
there by our own bug could not be put back without editing the database.

**`--parallel` was not a cap.** It limited how many started in one pass. The runner wrapper
calls `fleet tick` itself the moment a session exits and does not know what was typed, so
six parallel became seven running. The cap is persisted now and every tick honours it.


## 2026-09-19 — every runner meters differently, so normalise and never invent

Three shapes, no two alike:

| Runner | Buckets | Source |
|---|---|---|
| agy | Gemini weekly + 5h, Claude/GPT weekly + 5h, in two named groups | its own `quota-cache.json`, per profile |
| claude | five-hour and seven-day | `~/.claude/limit-state.json`, written by the statusline |
| cursor | included / auto / api | its dashboard API — **not** the CLI |

They are normalised to one shape: a named bucket, a fraction remaining, a reset time.

⚠ **agy reports what is left; claude reports what is used.** They are flipped to remaining
on the way out. Reversing that would show a spent account as fresh, which is the one error
here that would actually cost something.

claude's two windows are both always listed even when one has no reading, because the
statusline only writes the five-hour figure once Claude Code has reported one, and dropping
the line made it look as though claude has no five-hour limit at all.

## 2026-09-19 — cursor does expose its limits, and I said it did not

I checked `cursor-agent --help`, found no usage command, looked for a cached usage file,
found none, and concluded the allowances were unreadable. The operator said otherwise, and he
was right.

`cursor-agent about --format json` and `status --format json` carry the plan and the
account and genuinely no usage. But the CLI stores an access token, and Cursor's own
dashboard API answers it:

| Endpoint | Gives |
|---|---|
| `GET /auth/usage` | request counts, and `maxRequestUsage` where the plan meters requests |
| `POST …DashboardService/GetAggregatedUsageEvents` | real spend for the period, in cents |
| `POST …DashboardService/GetHardLimit` | `noUsageBasedAllowed`, which is the api limit |
| `GET /auth/full_stripe_profile` | tier, and whether billable auto is on |

⚠ **Cursor does not report a fraction remaining, so fleet does not draw one.** It meters in
spend and in whether overage is permitted. A percentage bar there would be a denominator
nobody gave us, so the bucket carries words instead: `$16.25 this period`, `pro plan,
billable auto on`, `off, hard stop at the included allowance`.

Cached for ten minutes, because the board refreshes every five seconds and that is no rate
to poll somebody else's service at. The token is read from disk at call time, never logged
and never put on a command line.

The lesson is the ordinary one: "the CLI has no flag for it" is not the same as "the tool
does not expose it", and I reported the first as though it were the second.

## 2026-09-19 — two agy accounts are not metered, and the board was saying they were full

> **Wrong, corrected 2026-09-20.** They are metered. The poller was asking the wrong
> host. See "the quota numbers came from the wrong backend" below. The observation in
> this entry was real and the conclusion drawn from it was not.

The operator noticed a2 and a3 reading 100% on every bucket while their sessions had been
running for two hours. They were right to ask.

The readings are current — the caches were seven minutes old — and the accounts are
genuinely distinct, with three different tokens. But:

| | project | weekly reading | window remaining |
|---|---|---|---|
| main | `a per-user project id` | 80% left | 2539 of 10080 minutes |
| a2 | `a shared consumer project` | 100% left | 10079 of 10080 minutes |
| a3 | `a shared consumer project` | 100% left | 10079 of 10080 minutes |

a2 and a3 report the *whole* window still to run, at every single poll, on every bucket.
Their windows restart each time they are asked. That is not an idle account, it is an
unmetered one: those sessions burn quota and this endpoint does not count it.

**A false full tank is the dangerous direction**, because it is exactly the reading a
dispatcher routes more work at. An account where nothing has moved *and* every window
restarts at the poll now reports no figure at all and says "not metered on this account".
The test is over the whole account on purpose — a freshly reset bucket looks identical on
its own, which is why main's 100% five-hour bucket is still shown as 100%: its weekly
bucket proves the account is metered.

What this does not fix: `agy-next` will keep rotating onto a2 and a3 until they return a
hard limit error, because there is no number to park them on. That was already true; it is
just visible now.

## agy is the default runner, and claude and cursor are on request only

2026-09-19. Until now a session that named no runner took the emptiest slot anywhere,
which meant claude and cursor picked up general queue work whenever agy was full. They are
not spare capacity. They are the tools the operator works in themselves, and a queue that drains
them to save some waiting has taken their terminal away to keep a dashboard busy.

So `auto` is now a field on the adapter: agy declares `True`, claude and cursor declare
`False`. A session with no runner may only land on an adapter that auto-claims, and it
waits for agy however long agy is busy. Naming a runner is the only way to reach the other
two, and that choice survives a requeue so a continuation cannot migrate.

It is on the adapter rather than filtered by name in `_pick` because the same rule written
in the scheduler is the coupling this whole design exists to remove. A new adapter that
forgets to declare it inherits the permissive answer, so a contract test pins it.

The cap trim changed with it. It sorted slots by how many were free, so a cap of six with
agy and claude both two free could hand the whole remainder to claude — a runner only a
named session can use — and leave every unnamed session with nowhere to go while agy sat
empty. Auto runners take the room first.

## claude had no dispatch guard at all, and hit its own wall

2026-09-19, same afternoon. The Claude adapter returned a constant ceiling of 2 and threw
away the quota it had just read. Its docstring said why: `limit-guard.py` on the box
refuses tool calls near the edge, so fleet did not need to predict anything.

That reasoning was wrong, and here is the shape of it. `limit-guard.py` is a **PreToolUse
hook**. It gates the next tool call inside a session it is already running in. It can
refuse no launch at all. So when the five-hour window ran out at 15:41, fleet started
`fa-17` and `fa-18` into a spent account. Both met Anthropic's own wall three seconds in —
`You've hit your session limit · resets 4:10pm` — before a single tool call existed for
the hook to block. Both then burned attempt 2 having done nothing, and were parked as
"2 attempts spent · incomplete" over work that had never run a second time.

The ceiling now reads the same figure the board shows and goes to zero under 25% left on
any window. That floor sits above `limit-guard.json`'s own stop of 20% on this box, on the
same reasoning as agy's: launching into the gap between the two produces a session that
starts, works for a minute and is then blocked mid-task, which is worse than not starting.

A window with no reading does not stop dispatch. The statusline only writes the five-hour
figure once Claude Code has reported one, so absent means unknown, and refusing all work
on an unknown would idle the runner for the first session of every day.

**Still open, and not fixed here:** a launch that dies before the agent starts still burns
an attempt. That is what parked `fa-17` and `fa-18` on a false verdict, and it is separate
from the ceiling — a rate limit is one way to die at the door, a duplicate tmux name is
another.

## where a session ran is not where it must run

2026-09-19, found while shipping the rule above. `launched()` overwrites `items.runner`
with whatever the session actually landed on, and `_pick` was reading that same field to
decide where it may go. So a session added with no runner, which had once been dispatched
to claude back when claude auto-claimed, was pinned to claude for every attempt after it:
it arrived uninvited and then could never leave. `fa-17` and `fa-18` are both in exactly
that shape in the ledger on the box right now.

The request now lives in its own column, `wanted`, written only by `add`. `runner` keeps
its old meaning — where it last ran — and liveness, nudge, the digest and the board all
still read it, because that is the question they are asking.

Rows written before the column get `wanted = NULL`, which is the true answer: nobody asked
for a runner. Copying `runner` across on migration would have preserved the exact bug.


## the quota numbers came from the wrong backend, and every one of them was wrong

2026-09-20. The operator put the Antigravity CLI's own quota panel next to the board. The CLI
said the second profile had 0.77% of its Gemini weekly left, refreshing in
74 hours. The board said "not metered on this account".

Google answers this on two hosts and they do not agree:

| profile | `cloudcode-pa` says | `daily-cloudcode-pa` says | truth |
|---|---|---|---|
| main | 80.37% | 3.49% | 3.49% |
| a2 | 100% | 0.77% | 0.77% |
| a3 | 100% | 19.24% | 19.24% |

`daily-cloudcode-pa` is the host, and the evidence is not a preference. Its numbers match
the CLI panel to two decimal places, and the CLI's own log shows every call going there.
The poller tried `cloudcode-pa` first and returned on its first success, so it never asked
the host with the answer. Both were queried with the same token and the same project, so
the project was never the variable — I chased it first and it was a dead end.

The poller now asks `daily-cloudcode-pa` first, in `the quota poller`. One line, and
it fixes the statusline and `agy-next` as well as this.

Three consequences here.

**The "not metered" wording is gone.** It was a conclusion invented to explain an
observation, and the observation had a much duller cause. The guard stays, because a fetch
can still degrade, but it now says "no usable reading from this host" and leaves the why
alone.

**The floor counts the weekly bucket.** It read `gemini-5h` only. All three profiles had a
full five-hour window and a nearly empty weekly one, so nothing ever tripped it, and four
sessions sat in `quota reached ... retrying` loops for fifteen hours while fleet counted
them as working. The five-hour window refills six times a working day; the weekly is the
one that runs out. The third-party buckets stay out of it — Claude and GPT are separate
credits and an empty Gemini bucket says nothing about them.

**The board says nothing until it has read something.** Until the first status lands the
model is a zero value, which rendered as "no runners reporting" over "the queue is empty".
Both sentences were true of the struct and false of the world, and since a status read
costs about two seconds, the board opened on them every single time.

## six agy accounts, one config, and the first one is called a1

2026-09-20. Three more Google accounts were signed in, taking agy from three profiles to
six. Two things came out of it.

**Every profile now carries the same config, and the statusline knows which one it is.**
Only the first profile had a `statusLine` at all; the others had been authed and left bare.
Copying the line across would have been worse than leaving them bare, because
`statusline.py` hardcoded `~/.gemini/antigravity-cli/quota-cache.json` — all six sessions
would have shown the first account's quota. That is the same error as reading the wrong
backend, a fortnight apart: a real number from the wrong place.

The script now takes the profile directory as an argument and derives the cache, the CPU
state file and the spawn lock from it. When the cache is stale it asks
`agy-next --refresh-profile <name>` rather than spawning the poller itself, because
`agy-next` owns the shim HOME that points the poller at one profile and doing it here would
refresh the first account's cache whichever profile asked. That verb is new and takes no
rotation lock: it writes one profile's own cache and touches nothing shared.

**`main` is `a1`.** It is an ordinary profile now, and `agya`, `agy-next` and fleet all
treat it as one. `main` still resolves as an alias so nothing already typed breaks.

⚠ `~/agy-accounts/a1` is a **symlink to `~/.gemini`**, not a moved directory. Two sessions
had been running against `--gemini_dir=$HOME/.gemini` for sixteen hours, and moving a
directory out from under them to win a tidier tree is a bad trade. Every absolute path in
the hooks, the statusline and the poller keeps resolving, and a bare `agy` still lands on
the same profile. To make it a real move once the box is quiet:

    rm ~/agy-accounts/a1 && mv ~/.gemini ~/agy-accounts/a1 && ln -s ~/agy-accounts/a1 ~/.gemini

Nothing has to change for that. Everything already treats a1 as an ordinary profile.

Two details that would have bitten. `agy-next`'s `discover()` prepended `"main"` to the
directory listing, so once `a1` existed it listed one account twice — which would have let
the rotation put two sessions on one credential believing they were on two. And fleet's
observed-process count matches on `--gemini_dir=`, so a1 is the one profile that answers to
two paths: a session started before the rename carries the old one for as long as it runs,
and counting only the new path would drop it from the slot arithmetic.

## cursor gets accounts too, and they are config overlays

2026-09-20. cursor-agent has no profile flag and no config-dir flag. It honours
`XDG_CONFIG_HOME` and reads its credential from `$XDG_CONFIG_HOME/cursor/auth.json`, so an
account is a config directory. `cursora c2 <args>` sets it; `cursor-profile new c3`
scaffolds one. c1 is a symlink to `~/.config`, so the original account did not move —
the same arrangement as a1 on the agy side, for the same reason.

⚠ **The profile directory is an overlay, not an empty config dir.** `XDG_CONFIG_HOME` is
inherited by every command the agent runs, and `~/.config` holds `gh`, `gcloud`,
`glab-cli`, `ccmux` and nineteen others. A bare directory would take all of them away from
the session: a cursor agent that cannot open a PR is worse than one account. Every entry is
symlinked in and only `cursor` is real, and the links are rebuilt on each launch so a tool
added to `~/.config` later appears without anyone remembering to come back.

Two consequences inside fleet.

**The observed count reads `/proc`, not `ps`.** Two accounts produce byte-identical command
lines, because the profile lives in the environment. `count_matching` would have put every
running cursor session on whichever profile it checked first. `count_by_env` matches
`XDG_CONFIG_HOME` out of `/proc/<pid>/environ` and skips a pid it cannot read, because
under-counting starts a session that was not needed and guessing starts one on an account
already at its ceiling.

**One usage cache per account.** A shared one would serve the first account's spend for
whichever profile asked, for the next ten minutes, and nothing downstream could have caught
it. A scaffolded profile with no credential is not offered as an account at all: it would
report an empty tank rather than no tank.


## a session can name its model, because on agy that names the allowance

2026-09-20. Every agy account meters Gemini and the Claude/GPT models as two separate
pools of the same size. We had been spending one of them. On the day this went in, a1, a2
and a3 were at 3.5%, 0.8% and 19% of their Gemini weekly while all seven accounts sat at
100% of their Claude/GPT weekly, untouched since the accounts were created.

So `fleet add ux-131 --model claude-sonnet-4-6`, or `model:` in the session frontmatter.
The model rides in the ledger in its own column beside `wanted`, is never overwritten by a
launch, and reaches the CLI as `--model`.

The mapping from an id to a pool is a prefix test in the agy adapter. `agy models` returns
exactly `gemini-*`, `claude-*` and `gpt-*` and the quota API names the groups the same way,
so there is no table to keep in sync with Google's model list. **Unrecognised counts as
third-party**: a new Gemini model misread that way wastes a pool nothing is using, while a
new Claude model misread the other way dispatches against an allowance that is empty on
three of the seven accounts.

Two things the scheduler had to learn.

**Capacity depends on the model**, so `free_slots` takes one and `dispatch` reads it once
per distinct model in the queue rather than once per tick. Usually that is one or two
reads. The per-model views are kept side by side rather than merged, because a slot free
for a Claude session on a1 is genuinely not free for a Gemini one.

**A seat is spent in every view.** A slot is one tmux pane on one account; which allowance
it bills is a different question from whether it is occupied. Decrementing only the view
the session came from would have handed the same seat to a second session asking for the
other pool.

**Cursor is not the same story, despite looking like it.** ~~It does have an API path for
Claude and GPT models, but on both accounts here `noUsageBasedAllowed` is true and
`customerBalance` is null: it is switched off and has nothing behind it.~~

> **Wrong, corrected the same day.** Cursor has two pools too. See "cursor has two lines
> inside the plan, and I read the wrong two fields" below.


## cursor has two lines inside the plan, and I read the wrong two fields

2026-09-20, an hour after saying it did not. The operator put cursor-agent's own usage panel on
screen: `Included 18% used`, and under it `Auto 36% used` and `API 0% used`.

I had called `GetHardLimit` and `full_stripe_profile`, read `noUsageBasedAllowed: true` and
`customerBalance: null`, and concluded one allowance. Those two fields describe **on-demand
billing** — paying *past* the plan — which is a different thing from how the plan is
divided inside. The numbers live in `GetCurrentPeriodUsage`, which I had not called.

| | auto used | api used | total |
|---|---|---|---|
| c1 | 36% | **0%** | 18% |
| c2 | 82% | 23% | 52% |

So c1 has an untouched api line, and the correction matters in the expensive direction:
the wrong answer was "there is nothing here", which would have left a whole pool unused
for as long as nobody re-checked.

**The model-to-pool map comes from the server, not a prefix test.** `GetCurrentPeriodUsage`
returns `autoBucketModels` alongside the numbers: `default`, the composer family, grok and
vega. Everything else — Claude, GPT, Gemini — bills the api line. agy earns a prefix test
because its three families are stable and named after their vendors; Cursor renames its own
house models often enough that the same trick would rot silently, and the list arrives with
the usage anyway.

⚠ An empty `autoBucketModels` means the call failed, not that nothing is an auto model, so
an unknown model reads as `auto` — gating on the line that is normally the emptier of the
two. An unknown should hold work back, not spend a pool we cannot see.

On-demand stays on the board as a yes/no rather than a pool, because with it off a spent
plan is a clean stop rather than a bill. That part of the earlier reading was right; it was
only the conclusion drawn from it that was wrong.

**Cursor publishes no prices.** `AvailableModels` returns 224 models and not one pricing,
cost or rate field, and there is no pricing endpoint in the CLI bundle. The only cost data
that exists is measured: `GetAggregatedUsageEvents` reports spend per model for work that
has actually run. So "which model is cost-efficient" is a question to answer by running
one, not by reading a table.


## the api line is vendor pass-through, so the model choice is a 16x decision

2026-09-20. The operator: the api pool bills at vendor prices, so read the price list rather
than run an experiment. He was right, and it checks out to the cent.

`cursor-grok-4.6-high` on c2 ran 2.253M input, 0.179M output and 30.647M cache-read
tokens. At Cursor's published rates for grok 4.6 — $2 / $6 / $0.50 per million — that is
$20.91. The measured spend was $20.91. Pass-through, exactly.

So the same real session, priced across the candidates:

| model | cost | of which cache read | vs cheapest |
|---|---|---|---|
| gpt-5.6-luna | **$1.28** | $0.61 | 1.0x |
| composer-2.5 | $7.70 | $6.13 | 6.0x |
| claude-sonnet-5 | $12.43 | $6.13 | 9.7x |
| grok-4.6 | $20.91 | $15.32 | 16.3x |
| gpt-5.6-sol | $24.86 | $12.26 | 19.4x |
| claude-opus-5 | $31.07 | $15.32 | 24.3x |

**Cache read is the bill.** Thirty of the thirty-three million tokens in that session were
cache reads, so the cache-read rate decides almost everything and the headline
input/output prices barely matter. Luna reads cache at $0.02 a million against grok's
$0.50, and that one number is most of the 16x.

**`autoBucketModels` is incomplete, and the arithmetic caught it.** The list named every
grok 4.5 variant and no 4.6 one, so the adapter had been routing grok 4.6 to the api line.
The billing says otherwise: c2's auto line was 81.667% of a $45 allowance, which is $36.75,
which is grok 4.6 at $20.91 plus composer 2.5 at $15.84 to the cent. The classifier now
unions the server list with Cursor's house-model prefixes, because each has been wrong
alone — the list forgot 4.6, and a prefix test alone would have to guess at a rename.

**The plan's real denominator is $45 a line, $90 across both**, and it is derivable rather
than published: spend divided by (auto% + api%) gives it, and both accounts agree to the
cent. `limit` is the nominal $20 plan and the percentages are not fractions of it, which
is how the board came to print "82% left" beside "$16.25 of $20.00" — two numbers that
cannot both be true. It prints the derived allowance now.

## the claude session count was always zero, and nobody noticed for a day

2026-09-20. A session started from `claude agents` had been working for 47 minutes with a
PR open. The board said the runner was idle.

The adapter counted processes matching `claude --dangerously-skip-permissions`. That is
what you type. It is not what runs: `claude` is a shim that execs the versioned binary, so
those two words never appear together on any command line and the count was **always
zero**. Every claude session fleet had ever shown came from its own ledger. It had never
once seen a session anybody else started.

The agy adapter learned this the day before — eight hand-dispatched sessions it had never
heard of — and the lesson was applied to one adapter instead of all three. The rule is
about the class, not the instance: **what you launch a thing with is not what the OS shows
running**, and an adapter has to be told what a live session looks like from outside.

It matches the install path now, minus the three helpers Claude Code puts beside each
session — a daemon, a pty host and a spare, all carrying that same path. Counting the path
alone turns one session into four and shuts the ceiling on nothing. Cursor turned out to be
correct by luck: its process is `agent`, not `cursor-agent`, but the versioned path on its
command line contains the string anyway.

Two display faults fell out of the same investigation.

**A ceiling of zero is not a runner that does not exist.** A runner reporting a reason was
collapsed to one line, so claude showed as `unavailable: 7 day at 20%` with no accounts, no
quota and no sign of the session running on it. Only a runner with no accounts at all is
one line now; a reason prints under the rows rather than instead of them.

**A slot held by somebody else now says so.** The count always included foreign sessions —
it is `max(ours, observed)` — but a silent `2/2` sends you looking for a row that was never
going to be there. Both surfaces append `(n not ours)`.
