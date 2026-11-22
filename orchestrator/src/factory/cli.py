"""The `factory` command. Read commands open the SQLite file read-only."""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from factory import db, serve

DEFAULT_DB = Path.home() / ".factory" / "factory.db"

LIST_COLUMNS = ["id", "repo", "task", "ticket", "attempt", "status", "outcome", "created_at"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("FACTORY_DB", DEFAULT_DB)),
        help="the orchestrator's SQLite file (default: $FACTORY_DB or %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    runs = commands.add_parser("runs", help="list the last runs, or show one")
    runs.add_argument("id", nargs="?", type=int)
    runs.add_argument("--limit", type=int, default=20, help=argparse.SUPPRESS)
    serve.add_parser(commands)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve.run(args)

    try:
        conn = db.connect_readonly(args.db)
    except sqlite3.Error as e:
        print(f"factory: can't open {args.db}: {e}", file=sys.stderr)
        return 1
    with closing(conn):
        if args.id is None:
            return _list_runs(conn, args.limit)
        return _show_run(conn, args.id)


def _list_runs(conn: sqlite3.Connection, limit: int) -> int:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    table = [[c.upper() for c in LIST_COLUMNS]]
    table += [["-" if r[c] is None else str(r[c]) for c in LIST_COLUMNS] for r in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(LIST_COLUMNS))]
    for line in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip())
    return 0


def _show_run(conn: sqlite3.Connection, run_id: int) -> int:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        print(f"factory: no run {run_id}", file=sys.stderr)
        return 1
    for key in row.keys():
        if key not in ("dispatch", "result"):
            print(f"{key}: {'-' if row[key] is None else row[key]}")
    for key in ("dispatch", "result"):
        print(f"\n{key}:")
        print("-" if row[key] is None else json.dumps(json.loads(row[key]), indent=2))
    return 0
