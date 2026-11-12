"""Building an edition: fetch, select, snapshot. One build at a time."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any

from . import feeds as feedmod
from . import learning, onthisday, ranking, weather
from .config import Config
from .db import Database, iso, utcnow

log = logging.getLogger("paper.edition")

OFFLINE_WINDOW_DAYS = 7
HISTORY_DAYS = 10


class Builder:
    def __init__(self, db: Database, config: Config, client_factory=feedmod.default_client,
                 weather_client=None, onthisday_client=None):
        self.db = db
        self.config = config
        self.client_factory = client_factory
        self.weather_client = weather_client      # callables returning httpx.Client, for tests
        self.onthisday_client = onthisday_client
        self._lock = threading.Lock()
        self.started_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def building(self) -> bool:
        return self._lock.locked()

    def today(self) -> date:
        return datetime.now(self.config.tz).date()

    def start_background(self, reason: str = "manual") -> bool:
        """Start a build in a thread. Returns False if one is already running."""
        if self.building:
            return False
        threading.Thread(target=self.build, kwargs={"reason": reason}, daemon=True, name="build").start()
        return True

    def build(self, reason: str = "scheduled", edition_date: date | None = None) -> dict[str, Any] | None:
        if not self._lock.acquire(blocking=False):
            log.info("Build already running; skipping %s build", reason)
            return None
        self.started_at = utcnow()
        try:
            result = self._build(edition_date or self.today(), reason)
            self.last_error = None
            return result
        except Exception as exc:
            log.exception("Edition build failed")
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.db.set_setting("last_build_error", {"error": self.last_error, "at": iso(utcnow())})
            return None
        finally:
            self.started_at = None
            self._lock.release()

    def _build(self, day: date, reason: str) -> dict[str, Any]:
        log.info("Building edition for %s (%s)", day, reason)
        db = self.db
        now = utcnow()
        learning.decay(db, day.isoformat())

        active = [dict(r) for r in db.query("SELECT * FROM feeds WHERE paused = 0")]
        results = asyncio.run(feedmod.fetch_all(active, self.client_factory)) if active else []
        counts = feedmod.store_results(db, active, results)
        offline = bool(active) and counts["ok"] == 0
        failed_sources = [f["name"] for f, r in zip(active, results) if not r.ok]

        # Weather and On this day. Their failures are handled inside and never stop the edition.
        if self.weather_client:
            weather.refresh(db, self.config.tz_name, self.config.alert_feed_url, self.weather_client)
        else:
            weather.refresh(db, self.config.tz_name, self.config.alert_feed_url)
        otd = onthisday.fetch(day, self.onthisday_client() if self.onthisday_client else None)
        if not otd["ok"]:
            prev = db.query_one("SELECT data FROM editions WHERE date = ?", (day.isoformat(),))
            if prev:
                old = json.loads(prev["data"]).get("onthisday")
                if old and old.get("ok"):
                    otd = old

        max_age = timedelta(days=OFFLINE_WINDOW_DAYS) if offline else \
            timedelta(hours=int(db.get_setting("max_age_hours", 36)))
        cutoff = iso(now - max_age)
        rows = [dict(r) for r in db.query(
            "SELECT s.*, f.priority, f.treat_as, f.topic_id FROM stories s JOIN feeds f ON f.id = s.feed_id "
            "WHERE f.paused = 0 AND COALESCE(s.published_at, s.fetched_at) >= ?", (cutoff,))]
        shown_ids = {r["story_id"] for r in db.query(
            "SELECT story_id FROM shown WHERE edition_date < ?", (day.isoformat(),))}
        history_titles = [r["title"] for r in db.query(
            "SELECT s.title FROM shown sh JOIN stories s ON s.id = sh.story_id "
            "WHERE sh.edition_date < ? AND sh.edition_date >= ?",
            (day.isoformat(), (day - timedelta(days=HISTORY_DAYS)).isoformat()))]
        topics = [dict(t) for t in db.query("SELECT * FROM topics")]
        selection = ranking.select(rows, topics, db.get_setting("exclusions", []), learning.load_weights(db),
                                   shown_ids, history_titles, now, seed=day.isoformat())

        data = {
            "reason": reason,
            "offline": offline,
            "failed_sources": failed_sources,
            "fetch": counts,
            "stats": selection.stats,
            "onthisday": otd,
            "topics": {str(t["id"]): t["name"] for t in topics},
            "snapshot": weather.snapshot(db),
            "build_seconds": round((utcnow() - now).total_seconds(), 1),
        }
        with db.conn() as c:
            c.execute("DELETE FROM editions WHERE date = ?", (day.isoformat(),))
            c.execute("DELETE FROM shown WHERE edition_date = ?", (day.isoformat(),))
            cur = c.execute("INSERT INTO editions(date, built_at, offline, data) VALUES (?,?,?,?)",
                            (day.isoformat(), iso(utcnow()), int(offline), json.dumps(data)))
            edition_id = cur.lastrowid
            for item in selection.items:
                c.execute("INSERT INTO edition_items(edition_id, story_id, section, topic_id, position, wildcard, "
                          "also) VALUES (?,?,?,?,?,?,?)",
                          (edition_id, item["story_id"], item["section"], item["topic_id"], item["position"],
                           int(item["wildcard"]), json.dumps(item["also"])))
                for sid in [item["story_id"], *item["also"]]:
                    c.execute("INSERT OR IGNORE INTO shown(story_id, edition_date) VALUES (?, ?)",
                              (sid, day.isoformat()))
            # Keep the database from growing forever: drop old stories never used in an edition.
            c.execute("DELETE FROM stories WHERE fetched_at < ? AND id NOT IN (SELECT story_id FROM edition_items) "
                      "AND id NOT IN (SELECT story_id FROM shown)", (iso(now - timedelta(days=30)),))
        db.set_setting("last_build_error", None)
        log.info("Edition %s built: %s stories, %s/%s feeds ok%s", day, len(selection.items), counts["ok"],
                 len(active), " (OFFLINE)" if offline else "")
        return {"date": day.isoformat(), "edition_id": edition_id, **data}

    def needs_catch_up(self) -> bool:
        """True if today's edition is missing and today's build time has already passed."""
        now = datetime.now(self.config.tz)
        hh, mm = parse_build_time(self.db.get_setting("build_time", "05:00"))
        if now.time() < datetime.min.replace(hour=hh, minute=mm).time():
            return False
        return self.db.query_one("SELECT 1 FROM editions WHERE date = ?", (now.date().isoformat(),)) is None


def parse_build_time(value: str) -> tuple[int, int]:
    try:
        hh, mm = (int(p) for p in str(value).split(":", 1))
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except ValueError:
        pass
    return 5, 0
