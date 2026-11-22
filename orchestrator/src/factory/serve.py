"""`factory serve`: run the orchestrator.

The App's identity comes from the environment, never from a flag (so it
doesn't show in `ps`):

- `FACTORY_APP_ID`: the GitHub App's id
- `FACTORY_APP_KEY_FILE`: path to the App's private key (PEM)

The repos to poll are found from the App's installations (see
`factory.enrolment`). Until a real runner exists, `--fake-runner` is required: it finishes every
run at once with a canned output (or the one in `--fake-output`).
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from factory import db, orchestrator
from factory.github import AppAuth, GitHub, urllib_transport
from factory.runner import CannedRunner

# Seams for tests.
TRANSPORT = urllib_transport
SLEEP = time.sleep


def add_parser(commands: argparse._SubParsersAction) -> None:
    serve = commands.add_parser("serve", help="run the orchestrator (polls GitHub every minute)")
    serve.add_argument("--fake-runner", action="store_true",
                       help="finish every run at once with a canned result, without a real runner")
    serve.add_argument("--fake-output", type=Path, metavar="FILE",
                       help="explore output (JSON) for the fake runner to return")


def run(args: argparse.Namespace) -> int:
    app_id = os.environ.get("FACTORY_APP_ID")
    key_file = os.environ.get("FACTORY_APP_KEY_FILE")
    if not app_id or not key_file:
        print("factory serve: set FACTORY_APP_ID and FACTORY_APP_KEY_FILE", file=sys.stderr)
        return 2
    if not args.fake_runner:
        print("factory serve: no real runner yet; pass --fake-runner", file=sys.stderr)
        return 2
    try:
        private_key = Path(key_file).read_text()
        output = json.loads(args.fake_output.read_text()) if args.fake_output else None
    except (OSError, ValueError) as e:
        print(f"factory serve: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db)
    github = GitHub(AppAuth(app_id, private_key, TRANSPORT), TRANSPORT)
    settings = orchestrator.Settings()
    try:
        orchestrator.serve(conn, github, CannedRunner(output), settings, sleep=SLEEP)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
    return 0
