"""Parallel RSS/Atom fetching with timeouts and retries. A failing feed never raises out of here."""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import feedparser
import httpx

from . import text
from .db import Database, iso, utcnow

log = logging.getLogger("paper.feeds")

USER_AGENT = "Mozilla/5.0 (compatible; PersonalNewspaper/1.0; +self-hosted)"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
RETRY_DELAYS = (1.0, 3.0)        # two retries after the first attempt
MAX_ENTRIES = 60
MAX_BYTES = 5 * 1024 * 1024
CONCURRENCY = 8

ClientFactory = Callable[[], httpx.AsyncClient]


def default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5",
    })


@dataclass
class FetchResult:
    feed_id: int
    ok: bool
    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    feed_title: str | None = None


class FeedError(Exception):
    pass


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def parse_feed(content: bytes) -> tuple[str | None, list[dict[str, Any]]]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        reason = getattr(parsed, "bozo_exception", None)
        raise FeedError(f"Not a readable RSS/Atom feed ({type(reason).__name__ if reason else 'unknown'})")
    now = utcnow()
    entries = []
    for e in parsed.entries[:MAX_ENTRIES]:
        link = (e.get("link") or "").strip()
        title = text.strip_html(e.get("title") or "")
        if not link or not title or not link.startswith(("http://", "https://")):
            continue
        raw_summary = e.get("summary") or ""
        if not raw_summary and e.get("content"):
            raw_summary = e["content"][0].get("value", "")
        categories = [t.get("term") or t.get("label") or "" for t in e.get("tags", []) or []]
        published = _entry_time(e)
        if published and published > now + timedelta(hours=1):
            published = now
        entries.append({
            "guid": e.get("id") or link,
            "url": link,
            "title": title,
            "summary": text.summarise(text.strip_html(raw_summary)),
            "categories": [c for c in categories if c][:12],
            "published_at": published,
        })
    title = text.strip_html(parsed.feed.get("title") or "") or None
    return title, entries


async def fetch_one(client: httpx.AsyncClient, feed: dict[str, Any]) -> FetchResult:
    headers = {}
    if feed.get("etag"):
        headers["If-None-Match"] = feed["etag"]
    if feed.get("last_modified"):
        headers["If-Modified-Since"] = feed["last_modified"]
    last_error = "unknown error"
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            resp = await client.get(feed["url"], headers=headers)
            if resp.status_code == 304:
                return FetchResult(feed["id"], ok=True, not_modified=True,
                                   etag=feed.get("etag"), last_modified=feed.get("last_modified"))
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # a 404/403 will not fix itself on retry
            else:
                content = resp.content[:MAX_BYTES]
                title, entries = await asyncio.to_thread(parse_feed, content)
                return FetchResult(feed["id"], ok=True, entries=entries, feed_title=title,
                                   etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified"))
        except FeedError as exc:
            last_error = str(exc)
            break
        except httpx.TimeoutException:
            last_error = "Timed out"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:300] or type(exc).__name__
        except Exception as exc:  # never let one feed break the edition
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("Unexpected error fetching %s", feed["url"])
            break
        if attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])
    return FetchResult(feed["id"], ok=False, error=last_error)


async def fetch_all(feeds: list[dict[str, Any]], client_factory: ClientFactory = default_client,
                    overall_timeout: float = 120.0) -> list[FetchResult]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async with client_factory() as client:
        async def guarded(feed):
            async with sem:
                return await fetch_one(client, feed)

        tasks = [asyncio.create_task(guarded(f)) for f in feeds]
        done, pending = await asyncio.wait(tasks, timeout=overall_timeout) if tasks else (set(), set())
        for t in pending:
            t.cancel()
    results = []
    for feed, task in zip(feeds, tasks):
        if task in done and not task.cancelled() and task.exception() is None:
            results.append(task.result())
        else:
            results.append(FetchResult(feed["id"], ok=False, error="Timed out (whole fetch took too long)"))
    return results


def store_results(db: Database, feeds: list[dict[str, Any]], results: list[FetchResult]) -> dict[str, int]:
    """Save new stories and per-feed status. Returns counts."""
    by_id = {f["id"]: f for f in feeds}
    now = iso(utcnow())
    new_count = 0
    with db.conn() as c:
        for r in results:
            feed = by_id[r.feed_id]
            if r.ok:
                c.execute(
                    "UPDATE feeds SET last_attempt_at=?, last_ok_at=?, consecutive_failures=0, etag=?, "
                    "last_modified=?, last_item_count=COALESCE(?, last_item_count) WHERE id=?",
                    (now, now, r.etag, r.last_modified, None if r.not_modified else len(r.entries), r.feed_id))
            else:
                c.execute(
                    "UPDATE feeds SET last_attempt_at=?, last_error=?, last_error_at=?, "
                    "consecutive_failures=consecutive_failures+1 WHERE id=?",
                    (now, r.error, now, r.feed_id))
            for e in r.entries:
                label = text.detect_label(e["title"], e["url"], e["categories"])
                cur = c.execute(
                    "INSERT OR IGNORE INTO stories(feed_id, source_name, guid, url, norm_url, title, summary, "
                    "categories, published_at, fetched_at, label_auto) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (r.feed_id, feed["name"], e["guid"], e["url"], text.normalise_url(e["url"]), e["title"],
                     e["summary"], json.dumps(e["categories"]), iso(e["published_at"]), now, label))
                new_count += cur.rowcount
    return {"new_stories": new_count, "ok": sum(r.ok for r in results), "failed": sum(not r.ok for r in results)}


async def probe_feed(url: str, client_factory: ClientFactory = default_client) -> FetchResult:
    """Fetch a feed once (used when adding a source) without touching the database."""
    async with client_factory() as client:
        return await fetch_one(client, {"id": 0, "url": url})
