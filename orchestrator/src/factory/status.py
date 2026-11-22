"""`factory status`: the factory at a glance. Reads SQLite only.

So far it lists the repos the App is installed on and whether each is polled
(#35 adds the pause flag, usage, the queue and the rest).
"""

import argparse
import sqlite3

from factory.contract import parse_enrolment


def add_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("status", help="show the factory's state: which repos are polled, ...")


def run(conn: sqlite3.Connection) -> int:
    print("Repos:")
    try:
        rows = conn.execute("SELECT * FROM repos ORDER BY repo").fetchall()
    except sqlite3.OperationalError:  # a database the orchestrator hasn't migrated yet
        rows = []
    if not rows:
        print("  no repos found yet (the orchestrator lists the App's installations every 10 minutes)")
        return 0
    width = max(len(r["repo"]) for r in rows)
    for row in rows:
        print(f"  {row['repo'].ljust(width)}  {_describe(row)}")
    return 0


def _describe(row: sqlite3.Row) -> str:
    state = row["state"]
    sha = (row["enrolment_sha"] or "")[:7]
    if state == "enrolled":
        if _disabled(row["config"]):
            return f"disabled (enabled: false), polled but not dispatched; config.yml at {sha}"
        return f"enrolled, polled; config.yml at {sha}"
    if state == "invalid":
        return f"invalid config.yml at {sha}, not polled: {row['error']}"
    if state == "not_enrolled":
        return "not enrolled: the App is installed but there's no .factory/config.yml"
    return "the App was uninstalled, not polled"


def _disabled(config: str) -> bool:
    return not parse_enrolment(config).enabled
