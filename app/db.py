"""SQLite storage: schema, connection helper, settings and first-run seed data."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    importance INTEGER NOT NULL DEFAULT 3,
    story_count INTEGER NOT NULL DEFAULT 6,
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    topic_id INTEGER REFERENCES topics(id),
    priority INTEGER NOT NULL DEFAULT 3,
    treat_as TEXT NOT NULL DEFAULT 'auto',
    paused INTEGER NOT NULL DEFAULT 0,
    etag TEXT,
    last_modified TEXT,
    last_attempt_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    last_item_count INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER REFERENCES feeds(id) ON DELETE SET NULL,
    source_name TEXT NOT NULL,
    guid TEXT,
    url TEXT NOT NULL,
    norm_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    label_auto TEXT NOT NULL DEFAULT 'reporting',
    label_override TEXT,
    interest INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stories_fetched ON stories(fetched_at);
CREATE INDEX IF NOT EXISTS idx_stories_published ON stories(published_at);
CREATE TABLE IF NOT EXISTS feedback (
    story_id INTEGER PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    value INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    deltas TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_terms (
    term TEXT PRIMARY KEY,
    weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_sources (
    feed_id INTEGER PRIMARY KEY REFERENCES feeds(id) ON DELETE CASCADE,
    weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS editions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,
    built_at TEXT NOT NULL,
    offline INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS edition_items (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    story_id INTEGER NOT NULL REFERENCES stories(id),
    section TEXT NOT NULL,
    topic_id INTEGER,
    position INTEGER NOT NULL,
    wildcard INTEGER NOT NULL DEFAULT 0,
    also TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_items_edition ON edition_items(edition_id);
CREATE TABLE IF NOT EXISTS articles (
    story_id INTEGER PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    byline TEXT,
    lead_image TEXT,
    images TEXT NOT NULL DEFAULT '[]',
    words INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_fetched ON articles(fetched_at);
CREATE TABLE IF NOT EXISTS shown (
    story_id INTEGER NOT NULL,
    edition_date TEXT NOT NULL,
    PRIMARY KEY (story_id, edition_date)
);
"""

DEFAULT_EXCLUSIONS = [
    "celebrity", "celebrities", "celeb", "celebs", "gossip", "showbiz", "paparazzi",
    "red carpet", "a-lister", "a-list", "Kardashian", "Kardashians", "Jenner",
    "reality star", "reality TV", "Love Island", "Married at First Sight", "MAFS",
    "The Bachelor", "The Bachelorette", "I'm a Celebrity", "Big Brother", "influencer",
    "baby bump", "dating rumours", "dating rumors", "wardrobe malfunction", "Met Gala",
    "royal baby", "Hollywood couple", "secret romance", "steps out with", "flaunts",
    "sizzles", "nip slip",
]

DEFAULT_TOPICS = [
    # name, keywords, importance, story_count
    ("Local & Australia",
     "Warrnambool, South West Victoria, south-west Victoria, Port Fairy, Portland, Koroit, "
     "Camperdown, Terang, Hamilton, Colac, Port Campbell, Great Ocean Road, Allansford, "
     "Moyne, Corangamite, Glenelg, Geelong, Ballarat, Victoria, Victorian, Melbourne",
     5, 8),
    ("World", "", 4, 7),
    ("Technology", "AI, Apple, Google, Linux, open source, security, chip, Proxmox, self-hosted", 3, 7),
    ("Motorsport", "Supercars, Formula 1, F1, Bathurst, MotoGP, WEC, IndyCar, rally, Phillip Island", 3, 6),
]

DEFAULT_FEEDS = [
    # name, url, topic name, priority
    ("ABC News", "https://www.abc.net.au/news/feed/10719986/rss.xml", "Local & Australia", 4),
    ("Guardian Australia", "https://www.theguardian.com/australia-news/rss", "Local & Australia", 3),
    ("SBS News", "https://www.sbs.com.au/news/feed", "Local & Australia", 3),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "World", 4),
    ("Guardian World", "https://www.theguardian.com/world/rss", "World", 3),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "World", 3),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "Technology", 4),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "Technology", 3),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "Technology", 3),
    ("Motorsport.com", "https://www.motorsport.com/rss/all/news/", "Motorsport", 3),
    ("Speedcafe", "https://speedcafe.com/feed/", "Motorsport", 4),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "build_time": "05:00",
    "location": {"name": "Warrnambool", "admin1": "Victoria", "country": "Australia",
                 "latitude": -38.3818, "longitude": 142.4880},
    "exclusions": DEFAULT_EXCLUSIONS,
    "max_age_hours": 36,
    "article_keep_days": 60,
    "decay_epoch": 0,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """A connection inside a transaction; commits on success, rolls back on error."""
        conn = self.connect()
        try:
            with self._write_lock:
                with conn:
                    yield conn
        finally:
            conn.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        with self.conn() as c:
            seeded = c.execute("SELECT value FROM settings WHERE key='seeded'").fetchone()
            if not seeded:
                self._seed(c)

    def _seed(self, c: sqlite3.Connection) -> None:
        now = iso(utcnow())
        for key, value in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
        topic_ids = {}
        for pos, (name, keywords, importance, count) in enumerate(DEFAULT_TOPICS):
            cur = c.execute(
                "INSERT INTO topics(name, keywords, importance, story_count, position) VALUES (?,?,?,?,?)",
                (name, keywords, importance, count, pos),
            )
            topic_ids[name] = cur.lastrowid
        for name, url, topic, priority in DEFAULT_FEEDS:
            c.execute(
                "INSERT OR IGNORE INTO feeds(name, url, topic_id, priority, created_at) VALUES (?,?,?,?,?)",
                (name, url, topic_ids[topic], priority, now),
            )
        c.execute("INSERT INTO settings(key, value) VALUES ('seeded', 'true')")

    # --- settings -------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return DEFAULT_SETTINGS.get(key, default) if default is None else default
        return json.loads(row["value"])

    def set_setting(self, key: str, value: Any, conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        if conn is not None:
            conn.execute(sql, (key, json.dumps(value)))
            return
        with self.conn() as c:
            c.execute(sql, (key, json.dumps(value)))

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None
