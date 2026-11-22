"""The orchestrator's log: JSON lines in a file it rotates itself.

`setup(log_dir)` attaches a handler to the `factory` logger. Each line is one
JSON object with `time`, `level`, `logger`, `msg`, the record's `run`, `repo`
and `ticket` when it's about a run (pass `extra=about_run(...)`), and `exc`.
Every string is passed through the token scan first. The file rotates at local
midnight and 30 days are kept.

`read`, `follow` and `format` are for `factory logs`.
"""

import json
import logging
import logging.handlers
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory import tokenscan

LOG_NAME = "factory.log"
KEEP_DAYS = 30
CONTEXT = ("run", "repo", "ticket")
LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40, "critical": 50}


def about_run(*, id: int, repo: str, ticket: str | None = None) -> dict[str, Any]:
    """The `extra` for a log call about a run."""
    return {"run": id, "repo": repo, "ticket": ticket}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in CONTEXT:
            value = getattr(record, key, None)
            if value is not None:
                line[key] = value
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(
            {k: tokenscan.redact(v) if isinstance(v, str) else v for k, v in line.items()}
        )


class Handler(logging.handlers.TimedRotatingFileHandler):
    """Rotates at local midnight to `factory.log.YYYY-MM-DD`, keeping 30."""

    def __init__(self, path: Path):
        super().__init__(path, when="midnight", backupCount=KEEP_DAYS, encoding="utf-8")
        self.setFormatter(JsonFormatter())


def setup(log_dir: str | Path, level: int = logging.INFO) -> Handler:
    """Send the `factory` loggers' records to `<log_dir>/factory.log`."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("factory")
    for old in [h for h in logger.handlers if isinstance(h, Handler)]:
        logger.removeHandler(old)
        old.close()
    handler = Handler(log_dir / LOG_NAME)
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler


def _files(log_dir: Path) -> list[Path]:
    rotated = sorted(log_dir.glob(f"{LOG_NAME}.*"))
    current = log_dir / LOG_NAME
    return rotated + ([current] if current.exists() else [])


def _parse(line: str) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return {"msg": line}
    return record if isinstance(record, dict) else {"msg": line}


def read(log_dir: str | Path) -> Iterator[dict[str, Any]]:
    """Every record in the log, oldest first. A line that isn't JSON comes back as its `msg`."""
    for path in _files(Path(log_dir)):
        yield from read_file(path)


def read_file(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                yield _parse(line.rstrip("\n"))


def follow(
    log_dir: str | Path, *, interval: float = 1.0, stop: threading.Event | None = None
) -> Iterator[dict[str, Any]]:
    """Every record so far, then new ones as they're written, across rotations."""
    log_dir = Path(log_dir)
    path = log_dir / LOG_NAME
    stop = stop or threading.Event()
    for rotated in _files(log_dir):
        if rotated != path:
            yield from read_file(rotated)
    f = None
    inode = None
    partial = ""
    try:
        while not stop.is_set():
            if f is not None:
                chunk = f.read()
                if chunk:
                    partial += chunk
                    *complete, partial = partial.split("\n")
                    for line in complete:
                        if line.strip():
                            yield _parse(line)
                            if stop.is_set():
                                return
                    continue
            # Created or rotated since: (re)open it from the start.
            if path.exists() and path.stat().st_ino != inode:
                if f is not None:
                    f.close()
                f = path.open(encoding="utf-8", errors="replace")
                inode = path.stat().st_ino
                partial = ""
                continue
            time.sleep(interval)
    finally:
        if f is not None:
            f.close()


def level_at_least(record: dict[str, Any], level: str) -> bool:
    return LEVELS.get(str(record.get("level", "info")).lower(), 20) >= LEVELS[level]


def format(record: dict[str, Any]) -> str:
    """One record as a readable line (and its traceback, if any, below it)."""
    when = str(record.get("time", "")).replace("T", " ").removesuffix("+00:00")
    level = str(record.get("level", "")).upper()
    context = []
    if record.get("run") is not None:
        context.append(f"run {record['run']}")
    if record.get("repo"):
        ticket = record.get("ticket")
        context.append(f"{record['repo']}#{ticket}" if ticket else str(record["repo"]))
    parts = [p for p in (when, level, " ".join(context), str(record.get("msg", ""))) if p]
    text = "  ".join(parts)
    if record.get("exc"):
        text += "\n" + str(record["exc"])
    return text
