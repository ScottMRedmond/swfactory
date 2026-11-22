"""The read commands only read."""

import hashlib
import sqlite3

import pytest

from factory import db, state

READ_COMMANDS = [["status"], ["runs"], ["runs", "1"], ["runs", "--json"], ["stats"], ["rules"]]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("args", READ_COMMANDS, ids=" ".join)
def test_a_read_command_leaves_the_database_untouched(world, args):
    world.finish(world.queue(task="review"), "review.valid.json")
    state.record_poll(world.conn, "octo/widgets")
    world.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = digest(world.path)

    code, _, err = world.cli(*args)

    assert code == 0, err
    assert digest(world.path) == before


@pytest.mark.parametrize("args", READ_COMMANDS, ids=" ".join)
def test_a_read_command_doesnt_migrate_an_old_database(tmp_path, capsys, args):
    from factory import cli

    path = tmp_path / "factory.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.MIGRATIONS[0] + "; PRAGMA user_version = 1;")

    code = cli.main(["--db", str(path), *args])

    assert code == 1
    assert "start the orchestrator" in capsys.readouterr().err
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
