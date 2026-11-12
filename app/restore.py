"""Restore the database from a backup. Run with the app stopped:

    docker compose stop
    docker compose run --rm paper python -m app.restore list
    docker compose run --rm paper python -m app.restore paper-20261112-023000.db
    docker compose start

The current database is backed up first, so a restore can itself be undone.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys

from .backup import backup_path, list_backups, run_backup
from .config import load_config


def check(path) -> str | None:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "integrity check failed"
            con.execute("SELECT COUNT(*) FROM topics").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return str(exc)
    return None


def main(argv: list[str]) -> int:
    config = load_config()
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    if argv[0] == "list":
        for p in list_backups(config):
            print(p.name)
        return 0
    source = backup_path(config, argv[0])
    if source is None:
        print(f"No backup called {argv[0]!r} in {config.backup_dir}. Try: python -m app.restore list")
        return 1
    problem = check(source)
    if problem:
        print(f"{source.name} doesn't look like a good backup: {problem}")
        return 1
    staged = config.data_dir / "restore.tmp"
    shutil.copyfile(source, staged)          # stage first: the safety backup may prune old files
    if config.db_path.exists():
        safety = run_backup(config)
        print(f"Current database saved as {safety.name}")
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(f"{config.db_path}{suffix}")
        except FileNotFoundError:
            pass
    os.replace(staged, config.db_path)
    print(f"Restored {source.name}. Start the app again with: docker compose start")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
