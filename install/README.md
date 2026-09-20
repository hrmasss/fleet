# Installing fleet

Two machines, or one. The **box** is where agent sessions actually run — it needs tmux,
the agent CLIs, and whatever credentials they use. The **workstation** is wherever you
type, and it only needs ssh.

Running everything on one machine is fine: install the box half and skip the rest.

## On the box

```sh
git clone <this repo> ~/projects/fleet
cp install/fleet ~/.local/bin/fleet
cp config.example.toml ~/.config/fleet/config.toml   # then edit it
```

Edit the config before the first run. Nothing in it has a working default, and fleet
refuses to start rather than guess — a dispatcher quietly running its production checks
against the wrong containers is worse than one that will not start.

The board is Go and is built here, then copied, so the box needs no Go toolchain:

```sh
make ship-board FLEET_BOX=<your-ssh-alias>
```

An hourly timer is the safety net under the push handshake. The wrapper calls `fleet tick`
itself when a session exits, so this only covers the cases the push cannot — a pane killed
with its wrapper, a reboot, a tick that crashed:

```cron
17 * * * * PATH=$HOME/.local/bin:$PATH fleet tick >/dev/null 2>&1
```

## On the workstation

```sh
cp install/fleet-remote        ~/.local/bin/fleet
cp install/fleet-board-remote  ~/.local/bin/fleet-board
echo "<your-ssh-alias>" > ~/.config/fleet/box
```

The alias is an **ssh config alias**, not a hostname, so `~/.ssh/config` owns the user, the
port and the key and none of that is repeated here.

## Shell helpers

Optional, and per-shell. `fls` is the board, `fls -t` the TUI, `flu` what every account has
left, `fla <session>` attaches to that session's pane. See `docs/design.md`.

## What each runner needs

The adapters shell out. `claude` and `cursor-agent` are the vendors' own CLIs and that is
all the `claude` and `cursor` runners need beyond being signed in.

⚠ **The `agy` runner also needs two helpers that are not in this repo**, because multiple
Antigravity accounts on one machine is not something the CLI supports on its own:

| command | contract |
|---|---|
| `agya <profile> [args…]` | run the CLI against one profile. A profile is a `--gemini_dir`. |
| `agy-next --status` | one line per signed-in profile: `<name> last <ago> <buckets>`, with `PARKED` or `SPENT` where it applies |
| `agy-next --limited <p>` | park a profile after a quota error |

`cursora <profile> [args…]` is the same idea for `cursor-agent`, whose profile is an
`XDG_CONFIG_HOME` pointing at a directory holding `cursor/auth.json`.

⚠ Whatever you write for `cursora`, make the profile directory an **overlay**, not an empty
config dir. `XDG_CONFIG_HOME` is inherited by every command the agent runs, so a bare
directory takes `gh`, `gcloud` and everything else in `~/.config` away from the session —
and an agent that cannot open a PR is worse than one account.

If you only run one account per runner, none of this applies: point `agya` at a one-line
wrapper, or run claude and cursor alone.

## What is not here

No secrets, no service, no port and no token. fleet reads the credentials each agent CLI
already stores, and ssh is the only control surface — an authenticated remote API would be
a second thing to get wrong and a second thing to leak.
