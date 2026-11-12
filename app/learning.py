"""Learned preferences from Interested / Not interested taps.

Each tap nudges the weights of the headline's words and of the source. Weights fade by DECAY
each edition. A tap can be undone exactly: we remember the deltas and the decay epoch they were
applied in, and subtract them scaled by the decay that has happened since.
"""
from __future__ import annotations

import json
from typing import Any

from . import text
from .db import Database, iso, utcnow

DECAY = 0.95
TERM_STEP = 1.0
SOURCE_STEP = 0.5
TERM_CAP = 8.0
SOURCE_CAP = 4.0
PRUNE_BELOW = 0.05


def _story_terms(title: str) -> list[str]:
    return list(dict.fromkeys(text.tokens(text.strip_title_prefix(title))))[:10]


def _add_term(c, term: str, delta: float) -> None:
    c.execute("INSERT INTO learned_terms(term, weight) VALUES (?, ?) "
              "ON CONFLICT(term) DO UPDATE SET weight = MAX(?, MIN(?, weight + excluded.weight))",
              (term, delta, -TERM_CAP, TERM_CAP))


def _add_source(c, feed_id: int, delta: float) -> None:
    c.execute("INSERT INTO learned_sources(feed_id, weight) VALUES (?, ?) "
              "ON CONFLICT(feed_id) DO UPDATE SET weight = MAX(?, MIN(?, weight + excluded.weight))",
              (feed_id, delta, -SOURCE_CAP, SOURCE_CAP))


def _prune(c) -> None:
    c.execute("DELETE FROM learned_terms WHERE ABS(weight) < ?", (PRUNE_BELOW,))
    c.execute("DELETE FROM learned_sources WHERE ABS(weight) < ?", (PRUNE_BELOW,))


def _undo(c, row, epoch: int) -> None:
    deltas = json.loads(row["deltas"])
    factor = DECAY ** max(0, epoch - row["epoch"])
    for term, d in deltas.get("terms", {}).items():
        c.execute("UPDATE learned_terms SET weight = weight - ? WHERE term = ?", (d * factor, term))
    if deltas.get("source"):
        feed_id, d = deltas["source"]
        c.execute("UPDATE learned_sources SET weight = weight - ? WHERE feed_id = ?", (d * factor, feed_id))
    c.execute("DELETE FROM feedback WHERE story_id = ?", (row["story_id"],))


def set_feedback(db: Database, story_id: int, value: int) -> int:
    """Apply Interested (+1) or Not interested (-1). Tapping the active choice again undoes it.
    Returns the story's resulting interest state (-1, 0 or 1)."""
    epoch = db.get_setting("decay_epoch", 0)
    with db.conn() as c:
        story = c.execute("SELECT id, feed_id, title, interest FROM stories WHERE id = ?", (story_id,)).fetchone()
        if story is None:
            raise KeyError(story_id)
        existing = c.execute("SELECT * FROM feedback WHERE story_id = ?", (story_id,)).fetchone()
        if existing is None and story["interest"] == value:
            # e.g. a story hidden before "reset learning": undo the state without touching weights
            c.execute("UPDATE stories SET interest = 0 WHERE id = ?", (story_id,))
            return 0
        if existing is not None:
            _undo(c, existing, epoch)
            if existing["value"] == value:      # same button again = undo
                c.execute("UPDATE stories SET interest = 0 WHERE id = ?", (story_id,))
                _prune(c)
                return 0
        terms = {t: TERM_STEP * value for t in _story_terms(story["title"])}
        for term, d in terms.items():
            _add_term(c, term, d)
        deltas: dict[str, Any] = {"terms": terms}
        if story["feed_id"]:
            _add_source(c, story["feed_id"], SOURCE_STEP * value)
            deltas["source"] = [story["feed_id"], SOURCE_STEP * value]
        c.execute("INSERT INTO feedback(story_id, value, epoch, deltas, created_at) VALUES (?,?,?,?,?)",
                  (story_id, value, epoch, json.dumps(deltas), iso(utcnow())))
        c.execute("UPDATE stories SET interest = ? WHERE id = ?", (value, story_id))
        _prune(c)
    return value


def decay(db: Database, edition_date: str) -> bool:
    """Fade learned weights once per edition date. Returns True if a decay happened."""
    if db.get_setting("last_decay_date", "") == edition_date:
        return False
    with db.conn() as c:
        c.execute("UPDATE learned_terms SET weight = weight * ?", (DECAY,))
        c.execute("UPDATE learned_sources SET weight = weight * ?", (DECAY,))
        _prune(c)
        row = c.execute("SELECT value FROM settings WHERE key='decay_epoch'").fetchone()
        epoch = json.loads(row["value"]) if row else 0
        db.set_setting("decay_epoch", epoch + 1, conn=c)
        db.set_setting("last_decay_date", edition_date, conn=c)
    return True


def load_weights(db: Database) -> tuple[dict[str, float], dict[int, float]]:
    terms = {r["term"]: r["weight"] for r in db.query("SELECT term, weight FROM learned_terms")}
    sources = {r["feed_id"]: r["weight"] for r in db.query("SELECT feed_id, weight FROM learned_sources")}
    return terms, sources


def forget_term(db: Database, term: str) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM learned_terms WHERE term = ?", (term,))


def forget_source(db: Database, feed_id: int) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM learned_sources WHERE feed_id = ?", (feed_id,))


def reset_all(db: Database) -> None:
    """Forget every learned weight. Hidden stories stay hidden; tap history is cleared."""
    with db.conn() as c:
        c.execute("DELETE FROM learned_terms")
        c.execute("DELETE FROM learned_sources")
        c.execute("DELETE FROM feedback")
        c.execute("UPDATE stories SET interest = 0 WHERE interest = 1")


def story_preference(terms: dict[str, float], sources: dict[int, float], title: str,
                     feed_id: int | None) -> float:
    words = _story_terms(title)
    term_score = sum(terms.get(w, 0.0) for w in words)
    term_score = max(-6.0, min(6.0, term_score))
    return term_score + sources.get(feed_id or -1, 0.0)
