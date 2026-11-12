"""'On this day' from Wikipedia's featured (selected) events feed, spread across the centuries."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from . import text
from .feeds import USER_AGENT

log = logging.getLogger("paper.onthisday")

BASE = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/{kind}/{mm:02d}/{dd:02d}"
TARGET = 6


def spread(events: list[dict[str, Any]], n: int = TARGET) -> list[dict[str, Any]]:
    """Pick about n events spread across history: evenly spaced through the timeline,
    avoiding two picks from the same century where possible. Earlier entries in the
    list (Wikipedia's featured ones) win ties."""
    if len(events) <= n:
        return sorted(events, key=lambda e: e["year"])
    order = {id(e): i for i, e in enumerate(events)}
    timeline = sorted(events, key=lambda e: (e["year"], order[id(e)]))
    picked: list[dict[str, Any]] = []
    used_centuries: set[int] = set()
    used: set[int] = set()
    for k in range(n):
        target = round(k * (len(timeline) - 1) / (n - 1))
        best = None
        for dist in range(len(timeline)):
            for idx in (target - dist, target + dist):
                if 0 <= idx < len(timeline) and idx not in used:
                    if timeline[idx]["year"] // 100 not in used_centuries:
                        best = idx
                        break
            if best is not None:
                break
        if best is None:  # every century already used: take the nearest unused event
            best = min((i for i in range(len(timeline)) if i not in used), key=lambda i: abs(i - target))
        used.add(best)
        used_centuries.add(timeline[best]["year"] // 100)
        picked.append(timeline[best])
    return sorted(picked, key=lambda e: e["year"])


def _parse(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    out = []
    for item in payload.get(key) or []:
        year = item.get("year")
        desc = text.strip_html(item.get("text") or "")
        if not isinstance(year, int) or not desc:
            continue
        link = None
        pages = item.get("pages") or []
        if pages:
            link = ((pages[0].get("content_urls") or {}).get("desktop") or {}).get("page")
        out.append({"year": year, "text": desc, "link": link if link and link.startswith("https://") else None})
    return out


def fetch(day: date, client: httpx.Client | None = None) -> dict[str, Any]:
    """Wikipedia's curated 'selected' anniversaries first; the wider 'events' list fills in older centuries."""
    client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True,
                                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    selected: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    with client as c:
        for kind, bucket in (("selected", selected), ("events", extra)):
            try:
                resp = c.get(BASE.format(kind=kind, mm=day.month, dd=day.day))
                resp.raise_for_status()
                bucket.extend(_parse(resp.json(), kind))
            except Exception as exc:
                log.warning("On this day (%s) failed: %s", kind, exc)
    if not selected and not extra:
        return {"ok": False, "items": [], "source": "Wikipedia"}
    seen = {e["year"] for e in selected}
    centuries = {e["year"] // 100 for e in selected}
    # Only borrow from the long list for centuries the featured list doesn't cover.
    pool = selected + [e for e in extra if e["year"] // 100 not in centuries and e["year"] not in seen]
    return {"ok": True, "items": spread(pool), "source": "Wikipedia"}
