"""Why a queued run is held back: the global limits and brakes.

`factory status` uses this to explain the queue. #19 wires the same rules into
dispatching and the status comment, and moves `Limits` into the config file.
The `contract_mismatch` pause and `claude_auth` holds come from
`factory.attempts`. Other per-repo holds (`enabled: false`, per-issue and
per-spec concurrency) are later tickets' to add.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from factory import attempts, state


@dataclass(frozen=True)
class Limits:
    max_in_flight: int = 2
    daily_cap: int = 40
    five_hour_threshold: float = 60  # percent
    weekly_threshold: float = 50  # percent; weekly_scoped and unknown kinds too
    usage_max_age: timedelta = timedelta(minutes=60)


FIVE_HOUR_KINDS = {"session"}
KIND_NAMES = {"session": "5-hour", "weekly_all": "weekly"}

PAUSED = "factory paused"
DAILY_CAP = "daily cap reached"
USAGE_UNKNOWN = "Claude usage unknown"
CONTRACT_MISMATCH = "dispatching paused: the runner rejected the contract version"
CLAUDE_AUTH = "Claude rejected this repo's token"
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
    group = row.get("group")
    return f"weekly ({group})" if kind == "weekly_scoped" and group else kind


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


def _about(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "unknown"
    local = when.astimezone()
    clock = local.strftime("%-I:%M %p").lower()
    return clock if local.date() == now.astimezone().date() else f"{local:%a} {clock}"


def usage_hold(reading: state.UsageReading | None, limits: Limits, now: datetime) -> str | None:
    """Why the usage reading stops dispatching, or None if it doesn't."""
    if reading is None or reading.error or not reading.limits:
        return USAGE_UNKNOWN
    read_at = parse_time(reading.read_at)
    if read_at is None or now - read_at > limits.usage_max_age:
        return USAGE_UNKNOWN
    for row in reading.limits:
        percent = row.get("percent")
        if not isinstance(percent, (int, float)):
            return USAGE_UNKNOWN
        threshold = (
            limits.five_hour_threshold if row.get("kind") in FIVE_HOUR_KINDS else limits.weekly_threshold
        )
        if percent >= threshold:
            resets = _about(parse_time(row.get("resets_at")), now)
            return f"Claude {kind_name(row)} usage at {percent:g}%, resumes about {resets}"
    return None


def queue_holds(
    conn: sqlite3.Connection, limits: Limits, now: datetime
) -> list[tuple[sqlite3.Row, str]]:
    """Every queued row, oldest first, with why it's waiting."""
    queued = conn.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY id").fetchall()
    if state.is_paused(conn):
        return [(row, PAUSED) for row in queued]
    if attempts.dispatch_paused(conn):
        return [(row, CONTRACT_MISMATCH) for row in queued]
    auth_held = attempts.held_repos(conn)
    held = [(row, CLAUDE_AUTH) for row in queued if row["repo"] in auth_held]
    rest = _global_holds(conn, [r for r in queued if r["repo"] not in auth_held], limits, now)
    return sorted(held + rest, key=lambda pair: pair[0]["id"])


def _global_holds(
    conn: sqlite3.Connection, queued: list[sqlite3.Row], limits: Limits, now: datetime
) -> list[tuple[sqlite3.Row, str]]:
    if daily_count(conn, now) >= limits.daily_cap:
        return [(row, DAILY_CAP) for row in queued]
    usage = usage_hold(state.latest_usage(conn), limits, now)
    if usage is not None:
        return [(row, usage) for row in queued]
    in_flight = conn.execute("SELECT count(*) FROM runs WHERE status = 'dispatched'").fetchone()[0]
    free = max(limits.max_in_flight - in_flight, 0)
    return [(row, NEXT if i < free else NO_SLOT) for i, row in enumerate(queued)]
