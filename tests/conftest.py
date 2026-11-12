"""Test fixtures: an app wired to mock feeds, a mock weather API and a mock Wikipedia."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import feeds as feedmod  # noqa: E402
from app.config import Config  # noqa: E402
from app.main import create_app  # noqa: E402

PASSWORD = "correct horse battery staple"


def rss(title: str, items: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    parts = []
    for i, it in enumerate(items):
        pub = it.get("published", now - timedelta(hours=it.get("age_h", 1 + i)))
        cats = "".join(f"<category>{escape(c)}</category>" for c in it.get("categories", []))
        parts.append(
            f"<item><title>{escape(it['title'])}</title><link>{escape(it['link'])}</link>"
            f"<guid>{escape(it.get('guid', it['link']))}</guid>"
            f"<description>{escape(it.get('summary', 'Summary of ' + it['title']))}</description>"
            f"{cats}<pubDate>{format_datetime(pub)}</pubDate></item>")
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>{escape(title)}</title>'
            f"<link>https://example.com</link>{''.join(parts)}</channel></rss>")


FORECAST = {
    "current": {"time": "2026-11-12T06:00", "temperature_2m": 14.2, "apparent_temperature": 12.0,
                "relative_humidity_2m": 80, "weather_code": 3, "wind_speed_10m": 22.0, "wind_gusts_10m": 41.0,
                "wind_direction_10m": 200, "precipitation": 0.0, "is_day": 1},
    "daily": {
        "time": [(datetime.now().date() + timedelta(days=i)).isoformat() for i in range(6)],
        "weather_code": [95, 61, 3, 0, 1, 2],
        "temperature_2m_max": [36.0, 18.0, 20.0, 22.0, 24.0, 19.0],
        "temperature_2m_min": [12.0, 1.0, 9.0, 10.0, 11.0, 8.0],
        "precipitation_probability_max": [40, 90, 10, 0, 5, 20],
        "precipitation_sum": [2.0, 14.0, 0.0, 0.0, 0.0, 0.4],
        "wind_speed_10m_max": [30, 45, 20, 15, 18, 22],
        "wind_gusts_10m_max": [50, 75, 30, 25, 28, 35],
        "wind_direction_10m_dominant": [180, 225, 270, 90, 0, 45],
        "uv_index_max": [9.5, 4.0, 7.0, 8.0, 8.5, 6.0],
        "sunrise": ["2026-11-12T06:05"] * 6, "sunset": ["2026-11-12T20:15"] * 6,
    },
}

BOM = """<?xml version="1.0"?><rss version="2.0"><channel><title>Weather Warnings for Victoria</title>
<item><title>12/16:15 EDT Strong Wind Warning for South West</title><link>http://www.bom.gov.au/vic/warnings/x.shtml</link></item>
</channel></rss>"""

WIKI = {"selected": [{"year": y, "text": f"Event in {y}", "pages": [{"content_urls": {"desktop": {
    "page": f"https://en.wikipedia.org/wiki/{y}"}}}]} for y in (1066, 1492, 1788, 1854, 1901, 1969, 1989, 2001)]}


class MockWeb:
    """Routes for the mock internet. Tests change .feeds / .fail to simulate outages."""

    def __init__(self):
        self.feeds: dict[str, str] = {}
        self.fail: dict[str, str] = {}      # url -> "500" | "timeout" | "404" | "garbage"
        self.calls: dict[str, int] = {}
        self.weather_down = False
        self.bom_down = False
        self.wiki_down = False
        self.pages: dict[str, str] = {}     # article pages
        self.images: dict[str, bytes] = {}  # image files

    def _count(self, url: str) -> None:
        self.calls[url] = self.calls.get(url, 0) + 1

    def feed_handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self._count(url)
        mode = self.fail.get(url)
        if mode == "timeout":
            raise httpx.ConnectTimeout("timed out", request=request)
        if mode == "500":
            return httpx.Response(500, text="oops")
        if mode == "404":
            return httpx.Response(404, text="nope")
        if mode == "garbage":
            return httpx.Response(200, text="<html><body>not a feed")
        if url in self.feeds:
            return httpx.Response(200, text=self.feeds[url], headers={"content-type": "application/rss+xml"})
        if url in self.pages:
            return httpx.Response(200, text=self.pages[url], headers={"content-type": "text/html; charset=utf-8"})
        if url in self.images:
            return httpx.Response(200, content=self.images[url], headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    def api_handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self._count(url.split("?")[0])
        host = request.url.host
        if host == "api.open-meteo.com":
            if self.weather_down:
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json=FORECAST)
        if host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json={"results": [
                {"name": "Port Fairy", "admin1": "Victoria", "country": "Australia",
                 "latitude": -38.385, "longitude": 142.237}]})
        if "bom.gov.au" in host:
            if self.bom_down:
                return httpx.Response(403)
            return httpx.Response(200, text=BOM)
        if host == "en.wikipedia.org":
            if self.wiki_down:
                return httpx.Response(503)
            return httpx.Response(200, json=WIKI if "/selected/" in url else {"events": []})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(feedmod, "RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
def web():
    return MockWeb()


@pytest.fixture
def config(tmp_path):
    return Config(app_password=PASSWORD, data_dir=tmp_path / "data", disable_scheduler=True, backup_keep=3)


@pytest.fixture
def app(config, web):
    kwargs = {
        "client_factory": lambda: httpx.AsyncClient(transport=httpx.MockTransport(web.feed_handler)),
        "weather_client": lambda: httpx.Client(transport=httpx.MockTransport(web.api_handler)),
        "onthisday_client": lambda: httpx.Client(transport=httpx.MockTransport(web.api_handler)),
        "article_client": lambda: httpx.AsyncClient(transport=httpx.MockTransport(web.feed_handler)),
    }
    application = create_app(config, builder_kwargs=kwargs)
    db = application.state.db
    # Replace the seeded real feeds with mock ones (keep the seeded topics).
    with db.conn() as c:
        c.execute("DELETE FROM feeds")
        topics = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM topics")}
        mock_feeds = [
            ("Local Herald", "https://local.test/rss", topics["Local & Australia"], 4, "auto"),
            ("Aussie Wire", "https://wire.test/rss", topics["Local & Australia"], 3, "auto"),
            ("Globe", "https://globe.test/rss", topics["World"], 4, "auto"),
            ("Planet Post", "https://planet.test/rss", topics["World"], 3, "auto"),
            ("Tech Daily", "https://tech.test/rss", topics["Technology"], 3, "auto"),
            ("Pit Lane", "https://pit.test/rss", topics["Motorsport"], 3, "auto"),
        ]
        for name, url, tid, prio, treat in mock_feeds:
            c.execute("INSERT INTO feeds(name, url, topic_id, priority, treat_as, created_at) "
                      "VALUES (?,?,?,?,?, '2026-01-01T00:00:00+00:00')", (name, url, tid, prio, treat))
    return application


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def logged_in(client):
    r = client.post("/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return client


def standard_feeds(web: MockWeb) -> None:
    web.feeds = {
        "https://local.test/rss": rss("Local Herald", [
            {"title": "Warrnambool council approves new breakwater upgrade", "link": "https://local.test/news/breakwater"},
            {"title": "Port Fairy folk festival announces lineup", "link": "https://local.test/news/folk"},
            {"title": "Opinion: Our roads deserve better funding", "link": "https://local.test/opinion/roads"},
            {"title": "Kardashian sister spotted in Melbourne", "link": "https://local.test/news/kardashian"},
            {"title": "Locals celebrate 150 years of the Warrnambool show", "link": "https://local.test/news/show"},
            {"title": "Why housing costs keep climbing in regional Victoria", "link": "https://local.test/analysis/housing",
             "categories": ["Analysis"]},
        ]),
        "https://wire.test/rss": rss("Aussie Wire", [
            {"title": "Warrnambool Council Approves New Breakwater Upgrade!",
             "link": "https://www.wire.test/story/breakwater?utm_source=rss&utm_medium=feed"},
            {"title": "Reserve Bank holds interest rates steady", "link": "https://wire.test/story/rba"},
            {"title": "Editorial: The budget misses the mark", "link": "https://wire.test/story/budget-ed"},
            {"title": "Opinion: Why I love winter", "link": "https://wire.test/story/winter"},
            {"title": "Comment: Parking is a mess", "link": "https://wire.test/comment/parking"},
        ]),
        "https://globe.test/rss": rss("Globe", [
            {"title": "Leaders gather for climate summit in Geneva", "link": "https://globe.test/world/summit"},
            {"title": "Earthquake strikes off coast of Japan", "link": "https://globe.test/world/quake"},
            {"title": "Celebrity chef opens restaurant", "link": "https://globe.test/world/chef"},
        ]),
        "https://planet.test/rss": rss("Planet Post", [
            {"title": "World leaders gather for Geneva climate summit", "link": "https://planet.test/a/summit"},
            {"title": "Elections called in Canada", "link": "https://planet.test/a/canada"},
        ]),
        "https://tech.test/rss": rss("Tech Daily", [
            {"title": "New Linux kernel brings faster file systems", "link": "https://tech.test/linux"},
            {"title": "Apple announces new tablet", "link": "https://tech.test/apple"},
            {"title": "Review: The latest noise-cancelling headphones", "link": "https://tech.test/review-headphones"},
        ]),
        "https://pit.test/rss": rss("Pit Lane", [
            {"title": "Supercars confirm Bathurst entry list", "link": "https://pit.test/bathurst"},
            {"title": "F1 team unveils new car", "link": "https://pit.test/f1"},
        ]),
    }
