"""`factory cancel <run>`: ask the orchestrator to cancel one run.

The CLI never talks to the runner or GitHub. It only writes the request to
SQLite; the orchestrator cancels the run in its next loop.
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime

from factory import db


def add_parser(commands: argparse._SubParsersAction) -> None:
    cancel = commands.add_parser(
        "cancel", help="ask the orchestrator to cancel a queued or in-flight run"
    )
    cancel.add_argument("run", type=int, help="the factory run id")


def run(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"factory: no database at {args.db}", file=sys.stderr)
        return 1
    try:
        conn = db.connect(args.db)
    except sqlite3.Error as e:
        print(f"factory: can't open {args.db}: {e}", file=sys.stderr)
        return 1
    with closing(conn):
        row = conn.execute("SELECT status FROM runs WHERE id = ?", (args.run,)).fetchone()
        if row is None:
            print(f"factory: no run {args.run}", file=sys.stderr)
            return 1
        if row["status"] == "done":
            print(f"factory: run {args.run} is already done", file=sys.stderr)
            return 1
        with conn:
            conn.execute(
                """UPDATE runs SET cancel_requested_at = coalesce(cancel_requested_at, ?)
                   WHERE id = ?""",
                (datetime.now(UTC).isoformat(timespec="seconds"), args.run),
            )
    print(f"asked to cancel run {args.run}; the orchestrator cancels it in its next loop")
    return 0
