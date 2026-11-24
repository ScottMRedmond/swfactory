"""`factory backup`: the nightly copy of the SQLite file (deploying.md#backups)."""

import sqlite3
from contextlib import closing
from datetime import datetime

import pytest

from factory import cli, db, loop

DISPATCH = {"contract_version": 1, "repo": "octo/widgets", "task": "smoke"}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "factory.db"


@pytest.fixture
def backups(tmp_path):
    return tmp_path / "backups"


def run_cli(capsys, path, *args):
    code = cli.main(["--db", str(path), *args])
    out, err = capsys.readouterr()
    return code, out, err


def run_ids(file):
    with closing(sqlite3.connect(file)) as conn:
        return [row[0] for row in conn.execute("SELECT id FROM runs ORDER BY id")]


def test_backup_writes_todays_copy_holding_the_runs(path, backups, capsys):
    conn = db.connect(path)
    ids = [loop.enqueue(conn, repo="octo/widgets", task="smoke", dispatch=DISPATCH) for _ in range(3)]

    code, out, _ = run_cli(capsys, path, "backup", "--to", str(backups))

    copy = backups / f"factory-{datetime.now().date().isoformat()}.db"
    assert code == 0
    assert run_ids(copy) == ids
    assert str(copy) in out


def test_backup_keeps_the_newest_copies_and_deletes_older_ones(path, backups, capsys):
    db.connect(path).close()
    backups.mkdir()
    for day in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        (backups / f"factory-{day}.db").write_bytes(b"")
    (backups / "notes.txt").write_text("not a backup")

    code, _, _ = run_cli(capsys, path, "backup", "--to", str(backups), "--keep", "2")

    today = f"factory-{datetime.now().date().isoformat()}.db"
    assert code == 0
    assert sorted(p.name for p in backups.iterdir()) == ["factory-2026-01-03.db", today, "notes.txt"]


def test_backup_of_a_missing_database_fails_and_keeps_the_old_copies(path, backups, capsys):
    backups.mkdir()
    old = backups / "factory-2026-01-01.db"
    old.write_bytes(b"")

    code, _, err = run_cli(capsys, path, "backup", "--to", str(backups), "--keep", "1")

    assert code == 1
    assert "can't open" in err
    assert not path.exists()
    assert [p.name for p in backups.iterdir()] == [old.name]


def test_backup_refuses_to_keep_fewer_than_one_copy(path, backups, capsys):
    db.connect(path).close()

    with pytest.raises(SystemExit) as exit:
        cli.main(["--db", str(path), "backup", "--to", str(backups), "--keep", "0"])

    assert exit.value.code == 2
    assert not backups.exists()


def test_backup_goes_beside_the_database_by_default(path, capsys):
    db.connect(path).close()

    code, _, _ = run_cli(capsys, path, "backup")

    assert code == 0
    assert (path.parent / "backups" / f"factory-{datetime.now().date().isoformat()}.db").exists()
