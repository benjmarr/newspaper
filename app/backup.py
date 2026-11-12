"""Nightly SQLite backups using SQLite's online backup API (safe while the app is running)."""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import Database, iso, utcnow

log = logging.getLogger("paper.backup")
NAME = re.compile(r"^paper-\d{8}-\d{6}\.db$")


def run_backup(config: Config) -> Path:
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(config.tz).strftime("%Y%m%d-%H%M%S")
    target = config.backup_dir / f"paper-{stamp}.db"
    tmp = target.with_suffix(".db.part")
    src = sqlite3.connect(config.db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA integrity_check")
    finally:
        dst.close()
        src.close()
    tmp.rename(target)
    prune(config)
    log.info("Backup written: %s", target.name)
    return target


def list_backups(config: Config) -> list[Path]:
    if not config.backup_dir.exists():
        return []
    return sorted((p for p in config.backup_dir.iterdir() if NAME.match(p.name)), key=lambda p: p.name,
                  reverse=True)


def prune(config: Config) -> None:
    for old in list_backups(config)[config.backup_keep:]:
        old.unlink(missing_ok=True)
    for part in config.backup_dir.glob("*.part"):
        part.unlink(missing_ok=True)


def backup_path(config: Config, name: str) -> Path | None:
    """Resolve a backup file by name, refusing anything that is not one of ours."""
    if not NAME.match(name):
        return None
    path = config.backup_dir / name
    return path if path.is_file() else None


def run_recorded(db: Database, config: Config) -> Path | None:
    """Run a backup and remember the outcome for the settings page."""
    try:
        path = run_backup(config)
        db.set_setting("last_backup", {"ok": True, "at": iso(utcnow()), "file": path.name})
        return path
    except Exception as exc:
        log.exception("Backup failed")
        db.set_setting("last_backup", {"ok": False, "at": iso(utcnow()), "error": f"{type(exc).__name__}: {exc}"})
        return None
