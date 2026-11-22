"""The `factory` command. Read commands open the SQLite file read-only."""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from factory import brakes, cancel, db, enrolment, logs, serve, state, stats

DEFAULT_DB = Path.home() / ".factory" / "factory.db"
DEFAULT_LOGS = Path.home() / ".factory" / "logs"

LIST_COLUMNS = ["id", "repo", "task", "ticket", "attempt", "status", "outcome", "created_at"]


def _since(text: str) -> str:
    """`7d`, `12h`, `30m`, `2w` or an ISO date or time (local if it has no zone), as UTC."""
    units = {"d": "days", "h": "hours", "m": "minutes", "w": "weeks"}
    try:
        if text[-1:] in units and text[:-1].isdigit():
            when = datetime.now(UTC) - timedelta(**{units[text[-1]]: int(text[:-1])})
        else:
            when = datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(f"can't read {text!r}: use e.g. 7d, 12h or 2026-11-01")
    return when.isoformat(timespec="seconds")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("FACTORY_DB", DEFAULT_DB)),
        help="the orchestrator's SQLite file (default: $FACTORY_DB or %(default)s)",
    )
    parser.add_argument(
        "--logs",
        type=Path,
        default=Path(os.environ.get("FACTORY_LOGS", DEFAULT_LOGS)),
        help="the orchestrator's log directory (default: $FACTORY_LOGS or %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    runs = commands.add_parser("runs", help="list the last runs, or show one")
    runs.add_argument("id", nargs="?", type=int)
    runs.add_argument("--repo")
    runs.add_argument("--ticket")
    runs.add_argument("--task")
    runs.add_argument("--outcome")
    runs.add_argument("--since", type=_since, help="e.g. 7d, 12h, or a date like 2026-11-01")
    runs.add_argument("--json", action="store_true", help="print JSON")
    runs.add_argument("--limit", type=int, default=20, help=argparse.SUPPRESS)

    commands.add_parser("status", help="the factory at a glance")

    stat = commands.add_parser("stats", help="per task and repo: outcomes, durations, retries")
    stat.add_argument("--since", type=_since, help="e.g. 30d (default: all runs)")
    stat.add_argument("--json", action="store_true", help="print JSON")

    rules = commands.add_parser("rules", help="every review's missing_rules, grouped by repo")
    rules.add_argument("--since", type=_since, help="e.g. 30d")

    log = commands.add_parser("logs", help="the orchestrator's log as readable lines")
    log.add_argument("--run", type=int)
    log.add_argument("--repo")
    log.add_argument("--level", choices=["debug", "info", "warn", "error"])
    log.add_argument("--follow", "-f", action="store_true")

    serve.add_parser(commands)
    cancel.add_parser(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return serve.run(args)
    if args.command == "cancel":
        return cancel.run(args)
    if args.command == "logs":
        return _logs(args)

    try:
        conn = db.connect_readonly(args.db)
    except sqlite3.Error as e:
        print(f"factory: can't open {args.db}: {e}", file=sys.stderr)
        return 1
    with closing(conn):
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as e:
            print(f"factory: can't read {args.db}: {e}", file=sys.stderr)
            return 1
        if version != len(db.MIGRATIONS):
            print(
                f"factory: {args.db} is at schema version {version}, this factory expects "
                f"{len(db.MIGRATIONS)}; start the orchestrator to migrate it",
                file=sys.stderr,
            )
            return 1
        return COMMANDS[args.command](conn, args)


# --- status ---


def _ago(when: str | None, now: datetime) -> str:
    then = brakes.parse_time(when)
    if then is None:
        return "-"
    seconds = max(int((now - then).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _usage_line(reading: state.UsageReading | None, now: datetime) -> str:
    if reading is None:
        return "usage: unknown (no reading yet)"
    age = f"(read {_ago(reading.read_at, now)} ago)"
    if reading.error or not reading.limits:
        return f"usage: unreadable: {reading.error or 'no limits in the report'} {age}"
    rows = ", ".join(f"{brakes.kind_name(r)} {r.get('percent', '?')}%" for r in reading.limits)
    return f"usage: {rows} {age}"


def _status(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    limits = brakes.Limits()
    print(f"paused: {'yes' if state.is_paused(conn) else 'no'}")
    print(_usage_line(state.latest_usage(conn), now))
    today = brakes.daily_count(conn, now)
    cap = " (cap reached)" if today >= limits.daily_cap else ""
    print(f"today: {today} of {limits.daily_cap} runs dispatched{cap}")

    in_flight = conn.execute("SELECT * FROM runs WHERE status = 'dispatched' ORDER BY id").fetchall()
    print(f"\nin flight: {len(in_flight) or 'none'}")
    if in_flight:
        _table(
            ["  ID", "REPO", "TASK", "TICKET", "ATTEMPT", "RUNNING"],
            [[f"  {r['id']}", r["repo"], r["task"], r["ticket"], r["attempt"],
              _ago(r["dispatched_at"], now)] for r in in_flight],
        )

    held = brakes.queue_holds(conn, limits, now)
    print(f"\nqueue: {f'{len(held)}, oldest first' if held else 'empty'}")
    if held:
        _table(
            ["  ID", "REPO", "TASK", "TICKET", "ATTEMPT", "WAITING", "HELD"],
            [[f"  {r['id']}", r["repo"], r["task"], r["ticket"], r["attempt"],
              _ago(r["created_at"], now), why] for r, why in held],
        )

    repos = enrolment.summary(conn)
    print(f"\nrepos: {'the App is installed on' if repos else 'none found yet'}")
    if repos:
        _table(["", ""], [[f"  {repo}", what] for repo, what in repos], header=False)

    polls = state.last_polls(conn)
    print(f"\npolls: {'last success per repo' if polls else 'none yet'}")
    if polls:
        _table(["", ""], [[f"  {repo}", f"{_ago(at, now)} ago"] for repo, at in polls], header=False)

    problems = state.open_problems(conn)
    print(f"\nfactory problems: {f'{len(problems)} open' if problems else 'none open'}")
    for p in problems:
        where = p["repo"] or "factory"
        issue = f"  {p['issue']}" if p["issue"] else ""
        print(f"  {p['kind']}  {where}  {p['summary']}  (since {_ago(p['opened_at'], now)} ago){issue}")
    return 0


def _runs(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.id is None:
        return _list_runs(conn, args)
    return _show_run(conn, args.id, as_json=args.json)


# --- stats ---


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "-"


def _mins(value: float | None) -> str:
    return "-" if value is None else f"{round(value)}m"


def _stats(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    entries = stats.compute(conn, since=args.since)
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("No runs yet.")
        return 0
    headers = ["TASK", "REPO", "RUNS", "OUTCOMES", "FAILURES", "MEDIAN", "P90", "NEAR TIMEOUT",
               "2ND ATTEMPT"]
    rows = []
    for s in entries:
        firsts, seconds = s["first_attempts"], s["needed_second_attempt"]
        share = f"{round(100 * seconds / firsts)}% ({seconds}/{firsts})" if firsts else "-"
        rows.append([s["task"], s["repo"], s["runs"], _counts(s["outcomes"]),
                     _counts(s["failures"]), _mins(s["median_minutes"]), _mins(s["p90_minutes"]),
                     s["near_timeout"], share])
    _table(headers, rows)
    return 0


# --- rules ---


def _rules(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    sql = "SELECT id, repo, result FROM runs WHERE task = 'review' AND result IS NOT NULL"
    params = []
    if args.since is not None:
        sql += " AND created_at >= ?"
        params.append(args.since)
    by_repo: dict[str, dict[str, list[int]]] = {}
    for row in conn.execute(sql + " ORDER BY id", params):
        output = json.loads(row["result"]).get("output")
        rules = output.get("missing_rules") if isinstance(output, dict) else None
        for rule in rules if isinstance(rules, list) else []:
            by_repo.setdefault(row["repo"], {}).setdefault(str(rule), []).append(row["id"])
    if not by_repo:
        print("No review has reported a missing rule.")
        return 0
    blocks = []
    for repo in sorted(by_repo):
        lines = [repo]
        for rule, ids in by_repo[repo].items():
            runs = f"run {ids[0]}" if len(ids) == 1 else "runs " + ", ".join(map(str, ids))
            lines.append(f"  {rule}  ({runs})")
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
    return 0


# --- logs ---


def _logs(args: argparse.Namespace) -> int:
    if not args.follow and not (args.logs / logs.LOG_NAME).exists():
        print(f"factory: no log in {args.logs} yet", file=sys.stderr)
        return 0
    records = logs.follow(args.logs) if args.follow else logs.read(args.logs)
    try:
        for record in records:
            if args.run is not None and record.get("run") != args.run:
                continue
            if args.repo is not None and record.get("repo") != args.repo:
                continue
            if args.level is not None and not logs.level_at_least(record, args.level):
                continue
            print(logs.format(record), flush=args.follow)
    except KeyboardInterrupt:
        pass
    return 0


# --- runs ---


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("dispatch", "result"):
        if out[key] is not None:
            out[key] = json.loads(out[key])
    return out


def _table(headers: list[str], rows: list[list[Any]], *, header: bool = True) -> None:
    table = [headers] + [["-" if v is None else str(v) for v in r] for r in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(headers))]
    for line in table if header else table[1:]:
        print("  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip())


def _list_runs(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    where, params = [], []
    for column in ("repo", "ticket", "task", "outcome"):
        if getattr(args, column) is not None:
            where.append(f"{column} = ?")
            params.append(getattr(args, column))
    if args.since is not None:
        where.append("created_at >= ?")
        params.append(args.since)
    sql = "SELECT * FROM runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, args.limit)).fetchall()
    if args.json:
        print(json.dumps([_row_dict(r) for r in rows], indent=2))
        return 0
    _table([c.upper() for c in LIST_COLUMNS], [[r[c] for c in LIST_COLUMNS] for r in rows])
    return 0


def _retry_chain(conn: sqlite3.Connection, row: sqlite3.Row) -> list[sqlite3.Row]:
    """Every attempt from the first to the last, oldest first."""
    root = row
    while root["retry_of"] is not None:
        parent = conn.execute("SELECT * FROM runs WHERE id = ?", (root["retry_of"],)).fetchone()
        if parent is None:
            break
        root = parent
    chain = [root]
    while True:
        child = conn.execute(
            "SELECT * FROM runs WHERE retry_of = ? ORDER BY id LIMIT 1", (chain[-1]["id"],)
        ).fetchone()
        if child is None:
            return chain
        chain.append(child)


def _check_tails(result: Any) -> list[tuple[str, str]]:
    if not isinstance(result, dict):
        return []
    names = {"check": "check output", "check_before": "check before", "check_after": "check after"}
    return [
        (label, result[key]["output_tail"])
        for key, label in names.items()
        if isinstance(result.get(key), dict) and isinstance(result[key].get("output_tail"), str)
    ]


def _show_run(conn: sqlite3.Connection, run_id: int, *, as_json: bool = False) -> int:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        print(f"factory: no run {run_id}", file=sys.stderr)
        return 1
    chain = _retry_chain(conn, row)
    data = _row_dict(row)
    if as_json:
        data["retry_chain"] = [
            {"id": r["id"], "attempt": r["attempt"], "status": r["status"], "outcome": r["outcome"]}
            for r in chain
        ]
        print(json.dumps(data, indent=2))
        return 0
    for key in row.keys():
        if key not in ("dispatch", "result", "log_url"):
            print(f"{key}: {'-' if row[key] is None else row[key]}")
    print(f"log: {row['log_url'] or '-'}")
    print("retry chain: " + " -> ".join(f"{r['id']} ({r['outcome'] or r['status']})" for r in chain))
    for label, tail in _check_tails(data["result"]):
        print(f"\n{label} (last lines):\n{tail.rstrip()}")
    for key in ("dispatch", "result"):
        print(f"\n{key}:")
        print("-" if data[key] is None else json.dumps(data[key], indent=2))
    return 0


COMMANDS = {"status": _status, "runs": _runs, "stats": _stats, "rules": _rules}
