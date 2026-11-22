"""`factory pause` and `factory resume`: stop and restart dispatching across
all repos (orchestrator.md#limits-and-brakes).

The flag lives in SQLite, so it survives a restart. Like `factory cancel`, the
CLI only writes it; the orchestrator reads it on its next loop, and carries on
polling, collecting, cancelling and posting while paused.
"""

import argparse
import sqlite3
import sys
from contextlib import closing

from factory import db, state


def add_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("pause", help="stop dispatching new runs (runs in flight carry on)")
    commands.add_parser("resume", help="start dispatching again after `factory pause`")


def run(args: argparse.Namespace) -> int:
    paused = args.command == "pause"
    try:
        conn = db.connect_readwrite(args.db)
        current = db.schema_current(conn)
    except sqlite3.Error as e:
        print(f"factory: can't open {args.db}: {e}", file=sys.stderr)
        return 1
    with closing(conn):
        if not current:
            print(f"factory: {args.db} isn't at this factory's schema version; "
                  "start the orchestrator to migrate it", file=sys.stderr)
            return 1
        state.set_paused(conn, paused)
    if paused:
        print("factory paused: nothing new is dispatched; runs in flight carry on")
    else:
        print("factory resumed: queued runs are dispatched on the next loop")
    return 0
