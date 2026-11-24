"""Which repos the orchestrator polls, and with what settings.

A repo is polled only when the App is installed on it **and** a valid
`.factory/config.yml` is on the head of its default branch. `refresh` (every
10 minutes) lists the App's installations, reads each repo's config.yml and
records what it found in the `repos` table:

- `enrolled`: polled, with `enrolment_sha` (the commit the config was read at)
  and `config` (the config.yml text).
- `not_enrolled`: installed, but no `.factory/config.yml`. Logged and listed in
  `factory status`; installing the App first and enrolling later is normal.
- `invalid`: config.yml fails `enrolment.schema.json`. Not polled.
- `uninstalled`: the App was removed.

Going from enrolled (or invalid) to `not_enrolled` or `uninstalled` is
unenrolling: queued rows are cancelled, in-flight runs finish and keep their
results but nothing is chained or posted for them, and active workflows are
`stopped`. Labels, branches and PRs are left alone.

A repo whose config can't be read (GitHub erring) keeps its previous state, and
if the installations can't be listed nothing changes at all, so an outage
never looks like unenrolling.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import yaml

from factory import attempts
from factory import state as db_state
from factory.contract import ContractError, Enrolment as Config, parse_enrolment
from factory.github import GitHub, GitHubError

log = logging.getLogger(__name__)

CONFIG_PATH = ".factory/config.yml"
FACTORY_CONFIG_PATH = "factory-config.yml"
GONE = ("not_enrolled", "uninstalled")

# The labels the factory uses, created the first time a repo is polled:
# (name, color, description). `spec:<n>` labels are made with their spec.
LABELS = [
    ("agent:explore", "1d76db", "Factory: explore this ticket"),
    ("agent:implement", "1d76db", "Factory: implement this ticket"),
    ("agent:review", "1d76db", "Factory: review this PR"),
    ("agent:implement-spec", "1d76db", "Factory: build this spec"),
    ("agent:in-progress", "fbca04", "Factory: a run is in progress"),
    ("agent:blocked", "b60205", "Factory: gave up after its attempts"),
    ("agent:built", "0e8a16", "Factory: built on the spec's integration branch"),
    ("needs-a-human", "d93f0b", "Factory: needs a human decision"),
    ("waiting", "c5def5", "Factory: waiting on something outside the repo"),
    ("spec", "5319e7", "Factory: a spec, built with agent:implement-spec"),
]


@dataclass(frozen=True)
class Enrolment:
    repo: str
    sha: str  # the default-branch commit whose .factory/ was read: the dispatch's enrolment_sha
    config: Config


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def refresh(conn: sqlite3.Connection, github: GitHub, *, factory_repo: str) -> str | None:
    """Re-read every installed repo's enrolment, and return (and store) the factory's `owner`.

    Raises if the App's installations can't be listed (nothing is changed then).
    """
    installed = github.installed_repos()
    known = {r["repo"]: r for r in conn.execute("SELECT * FROM repos")}
    for repo in installed:
        try:
            state, sha, config, error = _read(github, repo)
        except (GitHubError, OSError) as e:
            log.error("reading the enrolment of %s failed, keeping what was known: %s", repo, e)
            continue
        _record(conn, known.get(repo), repo, state, sha, config, error)
    for repo, row in known.items():
        if repo not in installed and row["state"] != "uninstalled":
            _record(conn, row, repo, "uninstalled", None, None, None)
    owner = _owner(github, factory_repo)
    if owner is not None:  # if it can't be read, the last one known stays
        db_state.set_owner(conn, owner)
    return owner


def _read(github: GitHub, repo: str) -> tuple[str, str | None, str | None, str | None]:
    """(state, enrolment_sha, config.yml text, validation error) for an installed repo."""
    try:
        head = github.default_branch_head(repo)
    except GitHubError as e:
        if e.status == 404:  # an empty repo has no default branch yet
            return "not_enrolled", None, None, None
        raise
    text = github.read_file(repo, CONFIG_PATH, head)
    if text is None:
        return "not_enrolled", head, None, None
    try:
        parse_enrolment(text)
    except ContractError as e:
        lines = [line.strip() for line in str(e).splitlines()]
        error = "; ".join(line for line in lines if line and not line.startswith("For further"))
        return "invalid", head, text, error
    return "enrolled", head, text, None


def _record(conn, old, repo, state, sha, config, error) -> None:
    old_state = old["state"] if old is not None else None
    with conn:
        conn.execute(
            """INSERT INTO repos (repo, state, enrolment_sha, config, error, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (repo) DO UPDATE SET state = excluded.state,
                 enrolment_sha = excluded.enrolment_sha, config = excluded.config,
                 error = excluded.error, checked_at = excluded.checked_at""",
            (repo, state, sha, config, error, _now()),
        )
    if state == "invalid":
        db_state.open_problem(
            conn, db_state.INVALID_ENROLMENT,
            f"`{CONFIG_PATH}` fails `contracts/enrolment.schema.json`, so this repo "
            f"isn't polled: {error}", repo=repo)
    elif db_state.has_open_problem(conn, db_state.INVALID_ENROLMENT, repo=repo):
        db_state.close_problem(conn, db_state.INVALID_ENROLMENT, repo=repo)
    if state == old_state:
        return
    if state == "enrolled":
        log.info("%s is enrolled (config.yml at %s)", repo, sha)
    elif state == "not_enrolled":
        log.info("%s has the App installed but no %s; not polling it", repo, CONFIG_PATH)
    elif state == "invalid":
        log.error("%s: %s is invalid, not polling it: %s", repo, CONFIG_PATH, error)
    else:
        log.info("the App is no longer installed on %s; not polling it", repo)
    if state in GONE and old_state not in (None, *GONE):
        _unenrol(conn, repo)


def _unenrol(conn: sqlite3.Connection, repo: str) -> None:
    """Cancel queued rows, let in-flight runs finish without acting on them, stop workflows."""
    now = _now()
    with conn:
        cancelled = conn.execute(
            """UPDATE runs SET status = 'done', outcome = 'cancelled', finished_at = ?,
                              handled_at = ?
               WHERE repo = ? AND status = 'queued'""",
            (now, now, repo),
        ).rowcount
        # handled_at set ahead of time: once collected, nothing is chained or posted.
        conn.execute(
            "UPDATE runs SET handled_at = ? WHERE repo = ? AND status = 'dispatched'",
            (now, repo),
        )
        conn.execute(
            "UPDATE workflows SET status = 'stopped' WHERE repo = ? AND status = 'active'", (repo,)
        )
    log.warning("unenrolled %s: cancelled %d queued run(s)", repo, cancelled)


def _owner(github: GitHub, factory_repo: str) -> str | None:
    try:
        text = github.read_file(factory_repo, FACTORY_CONFIG_PATH, "main")
    except (GitHubError, OSError) as e:
        log.error("reading %s from %s failed: %s", FACTORY_CONFIG_PATH, factory_repo, e)
        return None
    if text is None:
        log.error("%s isn't on %s's main (or the App isn't installed there)",
                  FACTORY_CONFIG_PATH, factory_repo)
        return None
    try:
        owner = (yaml.safe_load(text) or {}).get("owner")
    except (yaml.YAMLError, AttributeError):
        owner = None
    if not isinstance(owner, str) or not owner:
        log.error("%s on %s's main has no owner", FACTORY_CONFIG_PATH, factory_repo)
        return None
    return owner


def get(conn: sqlite3.Connection, repo: str) -> Enrolment | None:
    """The repo's enrolment, if it's enrolled."""
    row = conn.execute(
        "SELECT * FROM repos WHERE repo = ? AND state = 'enrolled'", (repo,)
    ).fetchone()
    return None if row is None else _enrolment(row)


def polled(conn: sqlite3.Connection) -> list[Enrolment]:
    """Every enrolled repo, by name."""
    rows = conn.execute("SELECT * FROM repos WHERE state = 'enrolled' ORDER BY repo")
    return [_enrolment(r) for r in rows]


DISABLED = "`enabled: false` in `.factory/config.yml`"
NOT_ENROLLED = "repo not enrolled"
CLAUDE_AUTH = "Claude rejected this repo's token"


def held(conn: sqlite3.Connection) -> dict[str, str]:
    """Repos whose queued runs aren't dispatched, with why: not enrolled,
    `enabled: false`, or held for `claude_auth` (`factory.attempts`, until a
    restart). A dict, so it also works as a collection of repos."""
    rows = conn.execute("SELECT repo FROM repos WHERE state != 'enrolled'").fetchall()
    holds = {r["repo"]: NOT_ENROLLED for r in rows}
    holds.update({e.repo: DISABLED for e in polled(conn) if not e.config.enabled})
    holds.update({repo: CLAUDE_AUTH for repo in attempts.held_repos(conn)})
    return holds


def summary(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Each repo the App is or was installed on, and whether it's polled (for `factory status`)."""
    return [(r["repo"], _describe(r))
            for r in conn.execute("SELECT * FROM repos ORDER BY repo").fetchall()]


def _describe(row: sqlite3.Row) -> str:
    sha = (row["enrolment_sha"] or "")[:7]
    if row["state"] == "enrolled":
        if not parse_enrolment(row["config"]).enabled:
            return f"disabled (enabled: false): polled, nothing dispatched; config.yml at {sha}"
        return f"enrolled: polled; config.yml at {sha}"
    if row["state"] == "invalid":
        return f"invalid config.yml at {sha}, not polled: {row['error']}"
    if row["state"] == "not_enrolled":
        return f"not enrolled: the App is installed but there's no {CONFIG_PATH}"
    return "the App was uninstalled; not polled"


def _enrolment(row: sqlite3.Row) -> Enrolment:
    return Enrolment(row["repo"], row["enrolment_sha"], parse_enrolment(row["config"]))


def ensure_labels(conn: sqlite3.Connection, github: GitHub, repo: str) -> None:
    """The first time a repo is polled, create the factory labels it lacks.

    An existing label is never edited. Done once per repo: a failure is
    raised and tried again on the next poll.
    """
    row = conn.execute("SELECT labels_at FROM repos WHERE repo = ?", (repo,)).fetchone()
    if row is None or row["labels_at"] is not None:
        return
    existing = github.labels(repo)
    for name, color, description in LABELS:
        if name not in existing:
            github.create_label(repo, name, color, description)
            log.info("created the label %s on %s", name, repo)
    with conn:
        conn.execute("UPDATE repos SET labels_at = ? WHERE repo = ?", (_now(), repo))
