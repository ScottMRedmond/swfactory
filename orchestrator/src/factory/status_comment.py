"""The status comment (orchestrator.md#observability).

Each issue or PR the factory works on gets one comment, marked
`<!-- factory:status -->`, made when its first run is queued and edited in place
after that, never deleted. It has one line per run, newest last. A run belongs
to its ticket and, when its dispatch names a PR, to that PR too. On a spec
issue, a line links the spec PR.

`sync` runs at the end of each pass. It renders every issue or PR with an
active run, or with a run whose final line hasn't been shown yet, and edits the
comment only when the text changed. Editing notifies nobody.
"""

import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from factory import brakes
from factory.github import GitHub, GitHubError
from factory.tickets import STATUS_MARK

log = logging.getLogger(__name__)

# At most this many attempts per ticket per task (orchestrator.md#limits-and-brakes).
MAX_ATTEMPTS = 2


# A spec's workflow kind, whose `pr` is the spec PR (orchestrator.md#implement-spec).
SPEC_KIND = "implement-spec"

Subject = tuple[str, int]  # (repo, issue or PR number)


def sync(
    conn: sqlite3.Connection,
    github: GitHub,
    *,
    limits: brakes.Limits,
    held: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> None:
    """Bring the status comment of every issue or PR whose runs changed up to
    date. A failed GitHub call is logged and tried again on the next pass."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """SELECT runs.* FROM runs JOIN repos USING (repo)
           WHERE repos.state = 'enrolled' AND task != 'smoke'
           AND (status != 'done' OR shown_at IS NULL) ORDER BY id"""
    ).fetchall()
    if not rows:
        return
    holds = _holds(conn, limits, held, now)
    synced: dict[Subject, bool] = {}
    for row in rows:
        for subject in _subjects(row):
            if subject not in synced:
                synced[subject] = _sync_one(conn, github, subject, holds, now)
    shown = [row["id"] for row in rows
             if row["status"] == "done" and all(synced[s] for s in _subjects(row))]
    with conn:
        conn.executemany("UPDATE runs SET shown_at = ? WHERE id = ?",
                         [(_iso(now), run_id) for run_id in shown])


def _subjects(row: sqlite3.Row) -> list[Subject]:
    numbers = [row["ticket"], json.loads(row["dispatch"] or "{}").get("pr_number")]
    return list(dict.fromkeys((row["repo"], int(n)) for n in numbers if n is not None))


def _holds(
    conn: sqlite3.Connection, limits: brakes.Limits, held: Mapping[str, str] | None,
    now: datetime,
) -> dict[int, str]:
    """Why each queued row is held back, by row id ("" for the next to go)."""
    return {row["id"]: "" if reason == brakes.NEXT else reason
            for row, reason in brakes.queue_holds(conn, limits, now, held)}


def _sync_one(
    conn: sqlite3.Connection,
    github: GitHub,
    subject: Subject,
    holds: dict[int, str],
    now: datetime,
) -> bool:
    repo, number = subject
    body = render(conn, repo, number, holds, now)
    known = conn.execute(
        "SELECT * FROM status_comments WHERE repo = ? AND number = ?", (repo, number)
    ).fetchone()
    try:
        if known is None:
            comment_id = github.post_comment(repo, number, body)
        elif known["body"] == body:
            return True
        else:
            comment_id = known["comment_id"]
            try:
                github.edit_comment(repo, comment_id, body)
            except GitHubError as e:
                if e.status != 404:
                    raise
                # Someone deleted it: start a new one.
                comment_id = github.post_comment(repo, number, body)
    except (GitHubError, OSError) as e:
        log.warning("updating the status comment on %s#%s failed: %s", repo, number, e,
                    extra={"repo": repo, "ticket": str(number)})
        return False
    with conn:
        conn.execute(
            """INSERT INTO status_comments (repo, number, comment_id, body) VALUES (?, ?, ?, ?)
               ON CONFLICT (repo, number) DO UPDATE SET comment_id = excluded.comment_id,
                                                        body = excluded.body""",
            (repo, number, comment_id, body),
        )
    return True


def render(
    conn: sqlite3.Connection, repo: str, number: int, holds: dict[int, str], now: datetime
) -> str:
    """The status comment of issue or PR `number`."""
    rows = conn.execute(
        """SELECT * FROM runs WHERE repo = ? AND task != 'smoke'
           AND (ticket = ? OR json_extract(dispatch, '$.pr_number') = ?) ORDER BY id""",
        (repo, str(number), number),
    ).fetchall()
    parts = [STATUS_MARK, "**Factory runs**", ""]
    spec = conn.execute(
        """SELECT pr FROM workflows WHERE repo = ? AND kind = ? AND subject = ?
           AND pr IS NOT NULL ORDER BY id DESC LIMIT 1""",
        (repo, SPEC_KIND, str(number)),
    ).fetchone()
    if spec is not None:
        parts += [f"Spec PR: #{spec['pr']}", ""]
    parts += [_line(row, holds, now) for row in rows]
    return "\n".join(parts) + "\n"


def _line(row: sqlite3.Row, holds: dict[int, str], now: datetime) -> str:
    """`task attempt/max: state[: why] · duration · log · run id`."""
    if row["status"] == "queued":
        why = holds.get(row["id"], "")
        state = f"queued: {why}" if why else "queued"
    elif row["status"] == "dispatched":
        state = "running"
    else:
        state = row["outcome"]
        failure = _failure(row["result"])
        if failure:
            state += f": {failure}"
    segments = [f"- `{row['task']}` {row['attempt']}/{MAX_ATTEMPTS}: {state}"]
    if row["dispatched_at"] is not None:
        until = now if row["finished_at"] is None else datetime.fromisoformat(row["finished_at"])
        segments.append(_duration(until - datetime.fromisoformat(row["dispatched_at"])))
    if row["log_url"]:
        segments.append(f"[log]({row['log_url']})")
    segments.append(f"run {row['id']}")
    return " · ".join(segments)


def _failure(result: str | None) -> str | None:
    if result is None:
        return None
    try:
        data: Any = json.loads(result)
    except json.JSONDecodeError:
        return None
    return data.get("failure") if isinstance(data, dict) else None


def _duration(elapsed: Any) -> str:
    minutes = max(int(elapsed.total_seconds() // 60), 0)
    if minutes < 1:
        return "<1 min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
