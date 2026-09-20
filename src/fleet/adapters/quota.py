"""What is left, and when it comes back.

Every runner meters differently, so this normalises them to one shape: a named bucket with
a fraction remaining and a reset time. What it does not do is invent numbers. A runner that
exposes nothing reports nothing and says why, because a blank reading and a full tank must
never look the same.

    agy       four buckets in two groups, straight from Antigravity's own quota cache:
              Gemini weekly and 5h, and the Claude-and-GPT weekly and 5h beside them.
              Per profile, because each account has its own cache.
    claude    five-hour and seven-day, from the limit-guard state file. ⚠ Its `pct` is
              percent **used**; agy's `remaining_fraction` is percent **left**. Getting
              that backwards would report an exhausted account as fresh.
    cursor    spend and whether overage is allowed, from Cursor's own dashboard API using
              the token `cursor-agent` already stores. ⚠ The CLI itself exposes nothing —
              `about` and `status` carry the plan and the account, never usage — so this
              goes to `api2.cursor.sh` directly and caches the answer, because the board
              refreshes every five seconds and that is no rate to poll somebody's API at.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from fleet.paths import STATE


@dataclass(frozen=True, slots=True)
class Bucket:
    id: str
    label: str
    remaining: float | None
    """0.0 to 1.0, or None when the runner did not say."""
    resets_at: float | None = None
    group: str = ""
    brief: str = ""
    """One or two words for the board strip, where there is room for a figure and no more.

    Set explicitly rather than derived from `detail`: guessing at it in the view produced
    "auto pro plan," and "api off —", because a first-word heuristic cannot know where a
    sentence's useful part ends."""

    detail: str = ""
    """For a reading that is not a fraction at all.

    Cursor meters in spend and in whether overage is permitted, not in percent remaining.
    Forcing that into a bar would be inventing a denominator it never gave us."""

    @property
    def pct(self) -> int | None:
        return None if self.remaining is None else round(self.remaining * 100)


def _iso(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


LABELS = {
    "gemini-5h": "Gemini 5h",
    "gemini-weekly": "Gemini weekly",
    "3p-5h": "Claude/GPT 5h",
    "3p-weekly": "Claude/GPT weekly",
    "five_hour": "5 hour",
    "seven_day": "7 day",
}


WINDOW_SECONDS = {"5h": 5 * 3600, "weekly": 7 * 86400}


def _untracked(data: dict) -> bool:
    """True when the whole account came back full with every window fresh.

    ⚠ This used to be reported as "not metered on this account", which was the wrong
    conclusion from the right observation. On 2026-09-20 the CLI showed one of those
    accounts at 0.77% of its Gemini weekly while this said it was untracked. The cause was
    the host: `cloudcode-pa` answers with a full tank for accounts whose real usage lives
    on `daily-cloudcode-pa`, which is the host the CLI itself calls. The poller asks the
    right one now, so this should never fire again — it stays as the guard for the day a
    fetch silently degrades, and it says the reading is unusable rather than inventing a
    reason for it.

    ⚠ Two of the three agy accounts report every bucket at exactly 100% with the *whole*
    window still to run, at every single poll — a2 and a3 answer from project
    `a shared consumer project` while main answers from a real project and shows 80% of its weekly
    gone. Their sessions burn quota; this endpoint simply does not count it.

    A freshly reset bucket looks the same on its own, which is why the test is over the
    whole account: an account where *nothing* has moved and *every* window restarts at the
    poll is not an idle account, it is an unmetered one. main fails this test on its weekly
    bucket, which is the point.
    """
    ts = float(data.get("ts") or 0)
    if not ts:
        return False
    seen = 0
    for group in data.get("groups", []):
        for b in group.get("buckets", []):
            if b.get("remaining_fraction") != 1:
                return False
            full = WINDOW_SECONDS.get(str(b.get("window")))
            reset = _iso(b.get("reset_time"))
            if not full or reset is None:
                return False
            if (reset - ts) < full * 0.99:
                return False
            seen += 1
    return seen > 0


UNTRACKED = "no usable reading from this host"


def agy(profile_dir: Path) -> list[Bucket]:
    """Antigravity's own cache, which `agy-next` already keeps fresh per profile."""
    data = _read(profile_dir / "antigravity-cli" / "quota-cache.json")
    if not data:
        return []
    blind = _untracked(data)
    out: list[Bucket] = []
    for group in data.get("groups", []):
        name = str(group.get("display_name") or "")
        for b in group.get("buckets", []):
            bid = str(b.get("bucket_id") or "")
            if not bid:
                continue
            remaining = b.get("remaining_fraction")
            out.append(
                Bucket(
                    id=bid,
                    label=LABELS.get(bid, bid),
                    remaining=(
                        None if blind else (float(remaining) if remaining is not None else None)
                    ),
                    resets_at=None if blind else _iso(b.get("reset_time")),
                    group=name,
                    brief="?" if blind else "",
                    detail=UNTRACKED if blind else "",
                )
            )
    return out


def claude(state: Path | None = None) -> list[Bucket]:
    """The limit-guard state file.

    ⚠ `pct` here is percent **used**. agy reports percent **left**. They are normalised to
    "remaining" on the way out, and getting that backwards would show a spent account as
    fresh — which is the one error that would matter.
    """
    path = state or Path(os.path.expanduser("~/.claude/limit-state.json"))
    data = _read(path) or {}
    out: list[Bucket] = []
    for key in ("five_hour", "seven_day"):
        window = data.get(key)
        if not isinstance(window, dict) or window.get("pct") is None:
            # ⚠ Both windows are always listed, even when one has no reading. The statusline
            # only writes the five-hour figure once Claude Code has actually reported one,
            # so an unused account simply has no line for it — and silently dropping a
            # window makes it look as though the runner does not have that limit at all.
            out.append(Bucket(id=key, label=LABELS.get(key, key), remaining=None))
            continue
        used = float(window["pct"])
        out.append(
            Bucket(
                id=key,
                label=LABELS.get(key, key),
                remaining=1.0 - used / 100.0,
                resets_at=(float(window["resets_at"]) if window.get("resets_at") else None),
            )
        )
    return out


CURSOR_ACCOUNTS = Path(os.path.expanduser("~/cursor-accounts"))
"""One config overlay per account. cursor-agent has no profile flag; it reads its
credential from $XDG_CONFIG_HOME/cursor/auth.json, so an account *is* a config directory.
c1 is a symlink to ~/.config, so the original account did not move."""
CURSOR_API = "https://api2.cursor.sh"
CURSOR_CACHE_TTL = 600.0
"""⚠ The board refreshes every five seconds. Cursor's dashboard API is somebody else's
service and that is no rate to poll it at, so the answer is cached for ten minutes."""

CURSOR_REASON = "cursor: not signed in, or the dashboard API did not answer"


def _cursor_call(path: str, token: str, body: dict | None = None) -> dict | None:
    """One read against Cursor's own dashboard API.

    The token comes off disk at call time and is never logged, never passed on a command
    line, and never stored anywhere by fleet.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{CURSOR_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "fleet",
        },
        data=(json.dumps(body).encode() if body is not None else None),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None


def cursor_auth(profile: str = "c1") -> Path:
    return CURSOR_ACCOUNTS / profile / "cursor" / "auth.json"


def cursor_profiles() -> list[str]:
    """Signed-in cursor accounts, in name order. A scaffolded profile with no credential
    is not one: it would report an empty tank rather than no tank."""
    try:
        return sorted(d.name for d in CURSOR_ACCOUNTS.iterdir() if cursor_auth(d.name).exists())
    except OSError:
        return []


def _cursor_live(profile: str = "c1") -> tuple[list[Bucket], list[str]]:
    """Cursor's own usage panel, and the model list that says which pool is which.

    ⚠ An earlier version of this read `noUsageBasedAllowed` and `customerBalance` and
    concluded cursor had one allowance. Those two describe **on-demand billing** — paying
    past the plan — which is a different thing from the split inside the plan. The plan
    itself meters `auto` and `api` separately, and on 2026-09-20 c1 had spent 36% of its
    auto line and nothing at all of its api one. Reading the wrong two fields hid a whole
    pool and came within a commit of being written into the design doc as a fact.

    `autoBucketModels` is the server's own list of what draws the auto line. Everything
    else — Claude, GPT, Gemini — draws the api line. No prefix heuristic is needed here
    and none should be added: Cursor renames its house models often enough that a guess
    would rot, and this list arrives with the numbers anyway.
    """
    auth = _read(cursor_auth(profile)) or {}
    token = auth.get("accessToken")
    if not token:
        return [], []

    period = _cursor_call("/aiserver.v1.DashboardService/GetCurrentPeriodUsage", token, {})
    hard = _cursor_call("/aiserver.v1.DashboardService/GetHardLimit", token, {})
    if not isinstance(period, dict):
        return [], []

    plan = period.get("planUsage") or {}
    resets = _ms(period.get("billingCycleEnd"))
    auto_models = [str(m) for m in (period.get("autoBucketModels") or [])]

    def line(bucket_id: str, label: str, pct_used: object, note: str) -> Bucket:
        pct = None if pct_used is None else max(0.0, min(1.0, 1.0 - float(pct_used) / 100.0))
        return Bucket(
            id=bucket_id,
            label=label,
            remaining=pct,
            resets_at=resets,
            brief="?" if pct is None else f"{pct * 100:.0f}%",
            detail=note,
        )

    out = [
        line("included", "Included", plan.get("totalPercentUsed"), _cursor_spend(plan)),
        line(
            "auto",
            "Auto",
            plan.get("autoPercentUsed"),
            "composer, grok and vega draw this line",
        ),
        line(
            "api",
            "API",
            plan.get("apiPercentUsed"),
            "claude, gpt and gemini draw this line",
        ),
    ]

    # On-demand is the wall past the plan, not a pool. It belongs on the board as a
    # yes/no, because with it off a spent plan is a hard stop rather than a bill.
    blocked = bool((hard or {}).get("noUsageBasedAllowed"))
    out.append(
        Bucket(
            id="on_demand",
            label="On-demand",
            remaining=None,
            brief="off" if blocked else "on",
            detail=(
                "off, so a spent plan is a hard stop"
                if blocked
                else "ON — spending past the plan is billed"
            ),
        )
    )
    return out, auto_models


def _cursor_spend(plan: dict) -> str:
    """What the plan has actually cost, against the allowance the percentages are of.

    ⚠ Not against `limit`. `limit` is the nominal plan ($20) and the percentages are not
    fractions of it: c1 had spent $16.25 of a $20 `limit` while reporting 18% used. The
    real denominator is derivable and the same on both accounts — auto and api are one
    allowance each, and spend divided by the reported fraction gives $45 a line. Printing
    the spend against `limit` put "82% left" and "$16.25 of $20.00" on the same row, which
    cannot both be true and is worse than printing neither.
    """
    total = plan.get("totalSpend")
    if total is None:
        return "no spend reported"
    text = f"${float(total) / 100:.2f} spent"
    allowance = _cursor_allowance(plan)
    if allowance:
        text += f" of ~${allowance / 100:.0f} across both lines"
    if plan.get("bonusSpend"):
        text += f", ${float(plan['bonusSpend']) / 100:.2f} of it bonus"
    return text


def _cursor_allowance(plan: dict) -> float | None:
    """The per-line allowance, back out of the numbers Cursor does give.

    Each line is the same size, so spend = (auto% + api%) x line. Both accounts agree on
    $45 a line to the cent, which is the only figure that makes the percentages and the
    per-model spend add up.
    """
    try:
        pct = (float(plan["autoPercentUsed"]) + float(plan["apiPercentUsed"])) / 100.0
        return (float(plan["totalSpend"]) / pct) * 2 if pct else None
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _ms(value: object) -> float | None:
    """Cursor sends epoch milliseconds as a string."""
    try:
        return float(value) / 1000.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _next_month(period_start: float | None) -> float | None:
    """Cursor's period is a calendar month from the billing anchor."""
    if not period_start:
        return None
    started = datetime.fromtimestamp(period_start)
    year, month = started.year, started.month + 1
    if month > 12:
        year, month = year + 1, 1
    try:
        return started.replace(year=year, month=month).timestamp()
    except ValueError:
        return None


def cursor(profile: str = "c1", cache: Path | None = None) -> list[Bucket]:
    """Spend, plan and whether overage is allowed. Cached, because this is a network call.

    ⚠ `cursor-agent` itself exposes none of this — `about` and `status` carry the plan and
    the account and nothing about usage — so it comes from Cursor's dashboard API with the
    token the CLI already stores.
    """
    # ⚠ One cache file per account. A shared one would serve the first account's spend
    # for whichever profile asked, for the next ten minutes.
    return _cursor_cached(profile, cache)[0]


def cursor_auto_models(profile: str = "c1", cache: Path | None = None) -> list[str]:
    """Which models draw the `auto` line rather than the `api` one, per Cursor itself.

    ⚠ Empty means we do not know, not "none of them". A caller must treat that as unknown
    and fall back to the whole plan, rather than routing every model to the api pool on
    the strength of a failed network call."""
    return _cursor_cached(profile, cache)[1]


def _cursor_cached(profile: str, cache: Path | None) -> tuple[list[Bucket], list[str]]:
    path = cache or (STATE / f"cursor-usage-{profile}.json")
    cached = _read(path)
    if cached and (time.time() - float(cached.get("ts", 0))) < CURSOR_CACHE_TTL:
        return [Bucket(**b) for b in cached.get("buckets", [])], list(cached.get("auto", []))

    buckets, auto = _cursor_live(profile)
    if buckets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"ts": time.time(), "buckets": [asdict(b) for b in buckets], "auto": auto}
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return buckets, auto
