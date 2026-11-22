"""Reading the Claude subscription's usage (orchestrator.md#reading-claude-usage).

`claude -p "/usage" --output-format stream-json --verbose` makes no model call.
With a full `claude auth login` it prints an assistant event carrying
`usage_report.rate_limits.limits[]` (`kind`, `percent`, `resets_at`, ...). The
type is marked experimental, so anything unexpected is "unreadable", and an
unreadable reading stops dispatching (see `factory.brakes`).

`refresh` takes a reading when one is due and stores it with
`state.record_usage`. `READ` is the seam tests replace; it returns
`(limits, None)` or `(None, why it couldn't be read)`.
"""

import json
import logging
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import datetime
from typing import Any

from factory import brakes, state

log = logging.getLogger(__name__)

COMMAND = ["claude", "-p", "/usage", "--output-format", "stream-json", "--verbose"]
TIMEOUT_SECONDS = 60

Reading = tuple[list[dict[str, Any]] | None, str | None]


def read_claude() -> Reading:
    """Run Claude Code's `/usage` and return its limit rows, or why it couldn't."""
    try:
        done = subprocess.run(COMMAND, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"can't run claude: {type(e).__name__}"
    if done.returncode != 0:
        return None, f"claude exited {done.returncode}"
    return parse(done.stdout)


def parse(stdout: str) -> Reading:
    """The `limits[]` rows from `/usage`'s stream-json output."""
    report = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and "usage_report" in event:
            report = event["usage_report"]
    if not isinstance(report, dict):
        return None, "no usage_report"
    rate_limits = report.get("rate_limits")
    if rate_limits is None:
        return None, "rate_limits is null"
    limits = rate_limits.get("limits") if isinstance(rate_limits, dict) else None
    if not isinstance(limits, list) or not limits or not all(_row_ok(r) for r in limits):
        return None, "unexpected usage_report format"
    return limits, None


def _row_ok(row: Any) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("kind"), str)
            and isinstance(row.get("percent"), (int, float)) and not isinstance(row["percent"], bool))


READ: Callable[[], Reading] = read_claude


def refresh(conn: sqlite3.Connection, limits: brakes.Limits, now: datetime, *, force: bool = False) -> None:
    """Take and store a new reading if the last one can't be reused (see
    `brakes.reading_due`), or if `force` (on startup and waking)."""
    if not force and not brakes.reading_due(state.latest_usage(conn), limits, now):
        return
    rows, error = READ()
    if error is not None:
        log.warning("couldn't read Claude usage: %s", error)
    state.record_usage(conn, rows, error=error)
