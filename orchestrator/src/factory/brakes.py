"""Why a queued run is held back: the limits and brakes
(orchestrator.md#limits-and-brakes).

The same rules decide what `orchestrator.run_once` dispatches, what each
held-back run's status line says, and what `factory status` shows:

- per-repo holds (`enrolment.held`: not enrolled, `enabled: false`, or Claude
  rejecting the repo's token) hold one repo's rows, which then don't take a
  free slot;
- the manual pause (`factory pause`), the `contract_mismatch` pause (lifted
  by restarting the orchestrator, see `factory.attempts`), the back-off after
  the runner refused a run, the daily cap, then the usage pause (or usage
  unknown) hold the rest of the queue;
- the rest wait for one of `max_in_flight` slots.

`Limits` is part of the global config, `orchestrator.Settings.limits`; a repo
can't override it. No brake ever cancels a run in flight.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from factory import attempts, loop, state


@dataclass(frozen=True)
class Limits:
    max_in_flight: int = 2
    daily_cap: int = 40
    five_hour_threshold: float = 60  # percent
    weekly_threshold: float = 50  # percent; weekly_scoped and unknown kinds too
    usage_max_age: timedelta = timedelta(minutes=60)  # older readings count as unknown
    usage_reuse: timedelta = timedelta(minutes=5)  # younger readings aren't taken again


FIVE_HOUR_KINDS = {"session"}
KIND_NAMES = {"session": "5-hour", "weekly_all": "weekly"}

PAUSED = "factory paused"
DAILY_CAP = "daily cap reached"
USAGE_UNKNOWN = "Claude usage unknown"
CONTRACT_MISMATCH = "dispatching paused: the runner rejected the contract version"
REFUSED = "the runner refused a run"
NEXT = "next to dispatch"
NO_SLOT = "waiting for a free slot"


def local_midnight(now: datetime) -> datetime:
    return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def daily_count(conn: sqlite3.Connection, now: datetime) -> int:
    """Rows dispatched since local midnight, except smoke runs."""
    since = local_midnight(now).astimezone(UTC).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT count(*) FROM runs WHERE dispatched_at >= ? AND task != 'smoke'", (since,)
    ).fetchone()[0]


def kind_name(row: dict) -> str:
    kind = str(row.get("kind"))
    if kind in KIND_NAMES:
        return KIND_NAMES[kind]
    if kind == "weekly_scoped":
        scope = row.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        return f"weekly ({name})" if name else "weekly (scoped)"
    return kind


def parse_time(value: object) -> datetime | None:
    """`resets_at` as an ISO string or a Unix timestamp."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _clock_time(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "unknown"
    local = when.astimezone()
    clock = local.strftime("%-I:%M %p").lower()
    return clock if local.date() == now.astimezone().date() else f"{local:%a} {clock}"


def _threshold(row: dict, limits: Limits) -> float:
    # weekly_scoped and kinds it doesn't know use the weekly threshold.
    return limits.five_hour_threshold if row.get("kind") in FIVE_HOUR_KINDS else limits.weekly_threshold


def _paused_until(reading: state.UsageReading | None, limits: Limits, now: datetime) -> datetime | None:
    """While the reading has a row over its threshold, the earliest of those
    rows' resets still to come: usage only falls at a reset. None otherwise."""
    if reading is None or reading.error or not reading.limits:
        return None
    resets = []
    for row in reading.limits:
        percent = row.get("percent")
        if isinstance(percent, (int, float)) and percent >= _threshold(row, limits):
            when = parse_time(row.get("resets_at"))
            if when is None or when <= now:
                return None  # a reset has passed, or we can't tell: read again
            resets.append(when)
    return min(resets) if resets else None


def reading_due(reading: state.UsageReading | None, limits: Limits, now: datetime) -> bool:
    """Whether to take a new usage reading before dispatching: not while paused
    for usage until its earliest reset, nor within `usage_reuse` of the last one."""
    if reading is None:
        return True
    if _paused_until(reading, limits, now) is not None:
        return False
    read_at = parse_time(reading.read_at)
    return read_at is None or now - read_at >= limits.usage_reuse


def usage_hold(reading: state.UsageReading | None, limits: Limits, now: datetime) -> str | None:
    """Why the usage reading stops dispatching, or None if it doesn't.

    A reading over a threshold holds until its reset, however old it is. Any
    other reading that's unreadable or older than `usage_max_age` is unknown.
    """
    if reading is None or reading.error or not reading.limits:
        return USAGE_UNKNOWN
    if any(not isinstance(row.get("percent"), (int, float)) for row in reading.limits):
        return USAGE_UNKNOWN
    over = [row for row in reading.limits if row["percent"] >= _threshold(row, limits)]
    read_at = parse_time(reading.read_at)
    stale = read_at is None or now - read_at > limits.usage_max_age
    if stale and _paused_until(reading, limits, now) is None:
        return USAGE_UNKNOWN
    if over:
        # It resumes when the last of the rows over their threshold resets.
        resets = [parse_time(row.get("resets_at")) for row in over]
        last = max(range(len(over)), key=lambda i: resets[i] or datetime.max.replace(tzinfo=UTC))
        row = over[last]
        return (f"Claude {kind_name(row)} usage at {row['percent']:g}%, "
                f"resumes about {_clock_time(resets[last], now)}")
    return None


def global_hold(
    conn: sqlite3.Connection, limits: Limits, now: datetime, *, usage: bool = True
) -> str | None:
    """Why nothing is dispatched at all, or None. `usage=False` leaves the
    usage reading out (dispatching reads usage only once the rest allow it)."""
    if state.is_paused(conn):
        return PAUSED
    if attempts.dispatch_paused(conn):
        return CONTRACT_MISMATCH
    refused = loop.refused_until(conn, now)
    if refused is not None:
        return f"{REFUSED}, next try about {_clock_time(refused, now)}"
    if daily_count(conn, now) >= limits.daily_cap:
        return DAILY_CAP
    return usage_hold(state.latest_usage(conn), limits, now) if usage else None


def queue_holds(
    conn: sqlite3.Connection,
    limits: Limits,
    now: datetime,
    held: Mapping[str, str] | None = None,
) -> list[tuple[sqlite3.Row, str]]:
    """Every queued row, oldest first, with why it's waiting. `held` maps a
    repo whose rows aren't dispatched to the reason."""
    held = held or {}
    queued = conn.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY id").fetchall()
    everything = global_hold(conn, limits, now)
    in_flight = conn.execute("SELECT count(*) FROM runs WHERE status = 'dispatched'").fetchone()[0]
    free = max(limits.max_in_flight - in_flight, 0)
    cap_left = limits.daily_cap - daily_count(conn, now)
    holds = []
    for row in queued:
        if row["repo"] in held:
            reason = held[row["repo"]]
        elif everything is not None:
            reason = everything
        elif cap_left <= 0:
            reason = DAILY_CAP
        elif free > 0:
            reason, free, cap_left = NEXT, free - 1, cap_left - 1
        else:
            reason = NO_SLOT
        holds.append((row, reason))
    return holds
