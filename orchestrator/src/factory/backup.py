"""`factory backup`: copy the SQLite file to `<dir>/factory-<date>.db`.

Run nightly by a systemd timer (docs/deploying.md#backups). It uses SQLite's online
backup, so the copy is consistent while the orchestrator is writing.
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from factory import db


def _at_least_one(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"must keep at least one copy, not {text!r}")
    return int(text)


def add_parser(commands: argparse._SubParsersAction) -> None:
    backup = commands.add_parser("backup", help="copy the database to a dated file")
    backup.add_argument("--to", type=Path,
                        help="the backups directory (default: backups/ beside the database)")
    backup.add_argument("--keep", type=_at_least_one, default=14,
                        help="how many daily copies to keep (default: %(default)s)")


def run(args: argparse.Namespace) -> int:
    folder = args.to or args.db.parent / "backups"
    target = folder / f"factory-{datetime.now().date().isoformat()}.db"
    # Read-only, so a wrong path fails instead of creating an empty database
    # whose copies would push the good ones out.
    try:
        source = db.connect_readonly(args.db)
    except sqlite3.Error as e:
        print(f"factory: can't open {args.db}: {e}", file=sys.stderr)
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    # Written beside the target and renamed, so a dated file is always complete.
    partial = target.with_name(target.name + ".partial")
    with closing(source):
        try:
            with closing(sqlite3.connect(partial)) as copy:
                source.backup(copy)
        except sqlite3.Error as e:
            partial.unlink(missing_ok=True)
            print(f"factory: can't back up {args.db}: {e}", file=sys.stderr)
            return 1
    partial.replace(target)
    print(f"backed up {args.db} to {target}")
    # ISO dates sort by name, oldest first.
    for old in sorted(folder.glob("factory-*.db"))[:-args.keep]:
        old.unlink()
    return 0
