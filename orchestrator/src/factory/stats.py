"""`factory stats`: computed from `runs` on demand, with no metrics store."""

import json
import math
import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from typing import Any

NEAR_TIMEOUT = 0.9  # a run that took 90% or more of its timeout


def _minutes(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60


def _p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile."""
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def compute(conn: sqlite3.Connection, since: str | None = None) -> list[dict[str, Any]]:
    """One entry per (task, repo), for runs created since `since` (a UTC timestamp)."""
    sql = "SELECT * FROM runs"
    params: list[str] = []
    if since is not None:
        sql += " WHERE created_at >= ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    retried = {r["retry_of"] for r in rows if r["retry_of"] is not None}

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["task"], row["repo"]), []).append(row)

    out = []
    for (task, repo), members in sorted(groups.items()):
        durations, near = [], 0
        failures: Counter[str] = Counter()
        for row in members:
            minutes = _minutes(row["dispatched_at"], row["finished_at"])
            if minutes is not None:
                durations.append(minutes)
                timeout = (json.loads(row["dispatch"] or "{}") or {}).get("timeout_minutes")
                if isinstance(timeout, (int, float)) and minutes >= NEAR_TIMEOUT * timeout:
                    near += 1
            result = json.loads(row["result"]) if row["result"] else None
            if isinstance(result, dict) and isinstance(result.get("failure"), str):
                failures[result["failure"]] += 1
        firsts = [r for r in members if r["attempt"] == 1]
        out.append(
            {
                "task": task,
                "repo": repo,
                "runs": len(members),
                "outcomes": dict(Counter(r["outcome"] for r in members if r["outcome"]).most_common()),
                "failures": dict(failures.most_common()),
                "median_minutes": statistics.median(durations) if durations else None,
                "p90_minutes": _p90(durations) if durations else None,
                "near_timeout": near,
                "first_attempts": len(firsts),
                "needed_second_attempt": sum(1 for r in firsts if r["id"] in retried),
            }
        )
    return out
