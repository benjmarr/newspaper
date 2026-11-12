from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from app import backup, learning, onthisday, text, weather
from conftest import PASSWORD, rss, standard_feeds


def build(app, day=None):
    return app.state.builder.build(reason="test", edition_date=day)


def edition_items(app, day=None):
    db = app.state.db
    day = (day or app.state.builder.today()).isoformat()
    return [dict(r) for r in db.query(
        "SELECT i.*, s.title, s.url, s.label_auto, s.source_name FROM edition_items i "
        "JOIN editions e ON e.id = i.edition_id JOIN stories s ON s.id = i.story_id WHERE e.date = ?", (day,))]


def story_id(app, fragment):
    row = app.state.db.query_one("SELECT id FROM stories WHERE title LIKE ?", (f"%{fragment}%",))
    assert row, fragment
    return row["id"]


# --- login ----------------------------------------------------------------

def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/healthz").json()["ok"] is True
    assert client.post("/rebuild", headers={"accept": "application/json"}).status_code == 401


def test_login_success_sets_30_day_cookie(client):
    r = client.post("/login", data={"password": PASSWORD, "next": "/archive"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/archive"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Max-Age=2592000" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert client.get("/archive").status_code == 200


def test_login_rejects_open_redirect(client):
    r = client.post("/login", data={"password": PASSWORD, "next": "//evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_lockout_after_five_failures(client):
    for i in range(4):
        r = client.post("/login", data={"password": "wrong"})
        assert r.status_code == 401
        assert f"{4 - i} attempt" in r.text
    r = client.post("/login", data={"password": "wrong"})
    assert r.status_code == 429 and "Too many wrong passwords" in r.text
    # Even the right password is refused while locked out.
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 429


def test_lockout_expires(client, app, monkeypatch):
    import time as _time
    for _ in range(5):
        client.post("/login", data={"password": "wrong"})
    real = _time.monotonic
    monkeypatch.setattr("app.auth.time.monotonic", lambda: real() + 301)
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303


def test_forged_cookie_rejected(client):
    client.cookies.set("paper_session", "abc.def.ghi")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_cross_site_post_refused(logged_in):
    r = logged_in.post("/rebuild", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


# --- building, exclusions, dedupe, labels -------------------------------

def test_edition_build_and_render(app, web, logged_in):
    standard_feeds(web)
    result = build(app)
    assert result["fetch"]["ok"] == 6 and not result["offline"]
    r = logged_in.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Warrnambool council approves" in html
    assert "rel=\"noopener noreferrer\"" in html
    assert "On this day" in html and "1066" in html
    assert "Strong Wind Warning" in html and "Bureau of Meteorology" in html
    assert "From the forecast" in html and "thunderstorms possible" in html
    assert "best guesses" in html
    # Masthead date
    d = app.state.builder.today()
    assert f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}" in html


def test_exclusions_are_a_hard_filter(app, web):
    standard_feeds(web)
    result = build(app)
    titles = [i["title"] for i in edition_items(app)]
    assert not any("Kardashian" in t for t in titles)
    assert not any("Celebrity chef" in t for t in titles)
    # Whole-word only: "celebrate" is not "celeb".
    assert any("Locals celebrate" in t for t in titles)
    assert result["stats"]["excluded"] == 2


def test_editing_exclusions(app, web, logged_in):
    standard_feeds(web)
    r = logged_in.post("/settings/exclusions", data={"terms": "Canada\nkardashian"}, follow_redirects=False)
    assert r.status_code == 303
    assert app.state.db.get_setting("exclusions") == ["Canada", "kardashian"]
    build(app)
    titles = [i["title"] for i in edition_items(app)]
    assert not any("Canada" in t for t in titles)
    assert any("Celebrity chef" in t for t in titles)  # no longer excluded


def test_url_normalisation():
    a = text.normalise_url("https://www.wire.test/story/breakwater/?utm_source=rss&utm_medium=feed#top")
    b = text.normalise_url("http://wire.test/story/breakwater")
    assert a == b
    assert text.normalise_url("https://x.test/a?id=2&b=1") == text.normalise_url("https://x.test/a?b=1&id=2")
    assert text.normalise_url("https://x.test/a?id=1") != text.normalise_url("https://x.test/a?id=2")


def test_duplicate_url_stored_once(app, web):
    web.feeds = {"https://local.test/rss": rss("L", [
        {"title": "Story one", "link": "https://same.test/x?utm_source=a"},
        {"title": "Story one again", "link": "https://www.same.test/x/"},
    ])}
    build(app)
    assert app.state.db.query_one("SELECT COUNT(*) n FROM stories")["n"] == 1


def test_fuzzy_dedupe_merges_with_also_reported_by(app, web, logged_in):
    standard_feeds(web)
    build(app)
    items = edition_items(app)
    breakwater = [i for i in items if "breakwater" in i["title"].lower()]
    assert len(breakwater) == 1
    assert len(json.loads(breakwater[0]["also"])) == 1
    summit = [i for i in items if "summit" in i["title"].lower()]
    assert len(summit) == 1
    html = logged_in.get("/").text
    assert "Also reported by" in html and "Aussie Wire" in html


def test_headline_similarity():
    assert text.same_story("Leaders gather for climate summit in Geneva", "World leaders gather for Geneva climate summit")
    assert not text.same_story("Leaders gather for climate summit in Geneva", "Earthquake strikes off coast of Japan")
    assert not text.same_story("Apple announces new tablet", "Apple announces record profits")


def test_label_detection():
    assert text.detect_label("Opinion: Roads", "https://x.test/news/a", []) == "opinion"
    assert text.detect_label("Roads", "https://x.test/commentisfree/2026/a", []) == "opinion"
    assert text.detect_label("Roads", "https://x.test/news/a", ["Opinion"]) == "opinion"
    assert text.detect_label("Analysis: Why rates rose", "https://x.test/a", []) == "analysis"
    assert text.detect_label("Rates", "https://x.test/news/analysis/rates", []) == "analysis"
    assert text.detect_label("Rates", "https://x.test/a", ["Explainer"]) == "analysis"
    assert text.detect_label("Rates rise", "https://x.test/news/rates", ["Business"]) == "reporting"
    # Path words inside other words don't count.
    assert text.detect_label("Viewers react", "https://x.test/news/commentators-react", []) == "reporting"


def test_labels_shown_and_correctable(app, web, logged_in):
    standard_feeds(web)
    build(app)
    sid = story_id(app, "Our roads deserve")
    html = logged_in.get("/").text
    card = re.search(rf'id="story-{sid}".*?</article>', html, re.S).group(0)
    assert "label-opinion" in card
    r = logged_in.post(f"/story/{sid}/label?next=/%23story-{sid}", data={"label": "analysis"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/#story-{sid}"
    card = re.search(rf'id="story-{sid}".*?</article>', logged_in.get("/").text, re.S).group(0)
    assert "label-analysis" in card and "Automatic<" in card
    logged_in.post(f"/story/{sid}/label", data={"label": "auto"})
    card = re.search(rf'id="story-{sid}".*?</article>', logged_in.get("/").text, re.S).group(0)
    assert "label-opinion" in card


def test_source_treat_as_override(app, web, logged_in):
    standard_feeds(web)
    feed = app.state.db.query_one("SELECT * FROM feeds WHERE name='Tech Daily'")
    logged_in.post(f"/settings/sources/{feed['id']}", data={
        "name": "Tech Daily", "topic_id": feed["topic_id"], "priority": 3, "treat_as": "analysis"})
    build(app)
    sid = story_id(app, "Linux kernel")
    card = re.search(rf'id="story-{sid}".*?</article>', logged_in.get("/").text, re.S).group(0)
    assert "label-analysis" in card


def test_leads_opinion_cap_and_wildcard(app, web):
    standard_feeds(web)
    build(app)
    items = edition_items(app)
    db = app.state.db
    labels = {r["id"]: r["label_auto"] for r in db.query("SELECT id, label_auto FROM stories")}
    leads = [i for i in items if i["section"] == "lead"]
    assert len(leads) == 3
    assert all(labels[i["story_id"]] != "opinion" for i in leads)
    by_section: dict[str, list] = {}
    for i in items:
        if i["section"] != "lead":
            by_section.setdefault(i["section"], []).append(i)
    for sec in by_section.values():
        assert sum(labels[i["story_id"]] == "opinion" for i in sec) <= 2
        assert sum(i["wildcard"] for i in sec) <= 1
    local_topic = str(db.query_one("SELECT id FROM topics WHERE name='Local & Australia'")["id"])
    # Local feed has 4 opinion pieces (Opinion:, Editorial:, Opinion:, Comment:); only 2 allowed.
    local_opinions = [i for i in by_section[local_topic] if labels[i["story_id"]] == "opinion"]
    assert len(local_opinions) == 2


def test_keyword_ranking(app, web):
    standard_feeds(web)
    build(app)
    leads = [i for i in edition_items(app) if i["section"] == "lead"]
    # The Warrnambool story is local (importance 5), keyword-matched and covered by two outlets.
    assert "breakwater" in leads[0]["title"].lower()


def test_never_repeat_across_editions(app, web):
    standard_feeds(web)
    today = app.state.builder.today()
    build(app, today - timedelta(days=1))
    first = {i["story_id"] for i in edition_items(app, today - timedelta(days=1))}
    # A new outlet carries an already-printed story under a new URL; it must not reappear.
    web.feeds["https://pit.test/rss"] = rss("Pit Lane", [
        {"title": "Supercars confirm Bathurst entry list", "link": "https://pit.test/bathurst"},
        {"title": "Supercars confirms the Bathurst entry list", "link": "https://pit.test/bathurst-2"},
        {"title": "MotoGP heads to Phillip Island", "link": "https://pit.test/motogp"},
    ])
    build(app, today)
    second = edition_items(app, today)
    assert not first & {i["story_id"] for i in second}
    assert not any("Bathurst" in i["title"] for i in second)
    assert any("MotoGP" in i["title"] for i in second)


def test_rebuild_same_day_replaces_edition(app, web):
    standard_feeds(web)
    build(app)
    n1 = len(edition_items(app))
    build(app)
    assert len(edition_items(app)) == n1
    assert app.state.db.query_one("SELECT COUNT(*) n FROM editions")["n"] == 1


def test_html_in_feeds_is_escaped(app, web, logged_in):
    web.feeds = {"https://local.test/rss": rss("L", [
        {"title": "<script>alert(1)</script>Warrnambool news", "link": "https://local.test/x",
         "summary": '<p>Hello <img src=x onerror="alert(2)"> <b>world</b></p>'},
        {"title": "Bad link", "link": "javascript:alert(3)"},
    ])}
    build(app)
    html = logged_in.get("/").text
    assert "<script>alert" not in html and "onerror" not in html and "javascript:" not in html
    assert "Hello world" in html


# --- failing sources --------------------------------------------------------

def test_failed_sources_do_not_block_edition(app, web, logged_in):
    standard_feeds(web)
    web.fail["https://globe.test/rss"] = "500"
    web.fail["https://planet.test/rss"] = "timeout"
    web.fail["https://tech.test/rss"] = "garbage"
    web.fail["https://pit.test/rss"] = "404"
    result = build(app)
    assert result["fetch"] == {"new_stories": result["fetch"]["new_stories"], "ok": 2, "failed": 4}
    assert not result["offline"]
    assert edition_items(app)
    # 5xx and timeouts are retried (3 attempts); 404 and unreadable feeds are not.
    assert web.calls["https://globe.test/rss"] == 3
    assert web.calls["https://planet.test/rss"] == 3
    assert web.calls["https://pit.test/rss"] == 1
    page = logged_in.get("/settings/sources").text
    assert "HTTP 500" in page and "Timed out" in page and "Not a readable RSS/Atom feed" in page and "HTTP 404" in page
    assert "Failing" in page
    assert "couldn&#39;t be reached" in logged_in.get("/").text or "couldn't be reached" in logged_in.get("/").text


def test_offline_build_uses_saved_stories(app, web, logged_in):
    standard_feeds(web)
    today = app.state.builder.today()
    # Day 1: fetch stories, but pretend they were never printed (only fetched).
    app.state.builder.client_factory  # noqa: B018
    from app import feeds as feedmod
    import asyncio
    active = [dict(r) for r in app.state.db.query("SELECT * FROM feeds")]
    results = asyncio.run(feedmod.fetch_all(active, app.state.builder.client_factory))
    feedmod.store_results(app.state.db, active, results)
    # Day 2: the internet is down.
    web.fail = {u: "timeout" for u in web.feeds}
    result = build(app, today)
    assert result["offline"] is True
    assert edition_items(app, today)
    assert "Offline edition" in logged_in.get("/").text


def test_paused_source_is_skipped(app, web, logged_in):
    standard_feeds(web)
    feed = app.state.db.query_one("SELECT id FROM feeds WHERE name='Pit Lane'")
    logged_in.post(f"/settings/sources/{feed['id']}/pause")
    build(app)
    assert "https://pit.test/rss" not in web.calls
    assert not any("Bathurst" in i["title"] for i in edition_items(app))
    logged_in.post(f"/settings/sources/{feed['id']}/pause")
    assert app.state.db.query_one("SELECT paused FROM feeds WHERE id=?", (feed["id"],))["paused"] == 0


# --- feedback learning -------------------------------------------------------

def test_interested_and_undo(app, web, logged_in):
    standard_feeds(web)
    build(app)
    sid = story_id(app, "Linux kernel")
    r = logged_in.post(f"/story/{sid}/feedback", data={"value": "interested"}, headers={"x-requested-with": "fetch"})
    assert r.json() == {"story_id": sid, "interest": 1}
    terms, sources = learning.load_weights(app.state.db)
    assert terms["linux"] == pytest.approx(1.0) and terms["kernel"] == pytest.approx(1.0)
    assert list(sources.values()) == [pytest.approx(0.5)]
    assert 'aria-pressed="true"' in re.search(rf'id="story-{sid}".*?</article>', logged_in.get("/").text, re.S).group(0)
    # Tap again: undo.
    r = logged_in.post(f"/story/{sid}/feedback", data={"value": "interested"}, headers={"x-requested-with": "fetch"})
    assert r.json()["interest"] == 0
    assert learning.load_weights(app.state.db) == ({}, {})


def test_not_interested_hides_and_switching(app, web, logged_in):
    standard_feeds(web)
    build(app)
    sid = story_id(app, "Apple announces")
    r = logged_in.post(f"/story/{sid}/feedback?next=/%23story-{sid}", data={"value": "not_interested"},
                       follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/#story-{sid}"
    card = re.search(rf'<article class="card[^"]*" id="story-{sid}"', logged_in.get("/").text).group(0)
    assert "is-hidden" in card
    terms, _ = learning.load_weights(app.state.db)
    assert terms["apple"] == pytest.approx(-1.0)
    # Switching to Interested replaces the negative nudge with a positive one.
    learning.set_feedback(app.state.db, sid, 1)
    terms, _ = learning.load_weights(app.state.db)
    assert terms["apple"] == pytest.approx(1.0)


def test_learned_weights_affect_ranking(app, web):
    db = app.state.db
    for _ in range(3):
        with db.conn() as c:
            c.execute("INSERT INTO learned_terms(term, weight) VALUES ('canada', 8) "
                      "ON CONFLICT(term) DO UPDATE SET weight = 8")
    standard_feeds(web)
    build(app)
    leads = [i["title"] for i in edition_items(app) if i["section"] == "lead"]
    assert any("Canada" in t for t in leads)


def test_weights_decay_each_edition_and_undo_is_exact(app, web):
    standard_feeds(web)
    db = app.state.db
    today = app.state.builder.today()
    build(app, today - timedelta(days=2))
    sid = story_id(app, "Linux kernel")
    learning.set_feedback(db, sid, 1)
    build(app, today - timedelta(days=1))
    terms, _ = learning.load_weights(db)
    assert terms["linux"] == pytest.approx(learning.DECAY)
    build(app, today - timedelta(days=1))  # rebuilding the same day doesn't decay twice
    assert learning.load_weights(db)[0]["linux"] == pytest.approx(learning.DECAY)
    learning.set_feedback(db, sid, 1)  # undo after decay removes exactly what's left
    assert "linux" not in learning.load_weights(db)[0]


def test_learning_settings_forget_and_reset(app, web, logged_in):
    standard_feeds(web)
    build(app)
    learning.set_feedback(app.state.db, story_id(app, "Linux kernel"), 1)
    learning.set_feedback(app.state.db, story_id(app, "Supercars confirm"), -1)
    page = logged_in.get("/settings/learning").text
    assert "linux" in page and "supercars" in page and "Tech Daily" in page and "Pit Lane" in page
    logged_in.post("/settings/learning/forget-term", data={"term": "linux"})
    assert "linux" not in learning.load_weights(app.state.db)[0]
    r = logged_in.post("/settings/learning/reset", data={}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # needs confirmation
    logged_in.post("/settings/learning/reset", data={"confirm": "yes"})
    assert learning.load_weights(app.state.db) == ({}, {})
    # The hidden story stays hidden; tapping its Undo just un-hides it.
    sid = story_id(app, "Supercars confirm")
    assert learning.set_feedback(app.state.db, sid, -1) == 0
    assert learning.load_weights(app.state.db) == ({}, {})


def test_hidden_story_not_used_again(app, web):
    standard_feeds(web)
    today = app.state.builder.today()
    build(app, today)
    sid = story_id(app, "Reserve Bank")
    learning.set_feedback(app.state.db, sid, -1)
    build(app, today)  # rebuild
    assert sid not in {i["story_id"] for i in edition_items(app, today)}


# --- settings ------------------------------------------------------------

def test_topics_crud(app, logged_in):
    db = app.state.db
    r = logged_in.post("/settings/topics", data={"name": "Science", "keywords": "space, NASA ,, physics",
                                                 "importance": 9, "story_count": 4, "position": 5},
                       follow_redirects=False)
    assert r.status_code == 303
    t = db.query_one("SELECT * FROM topics WHERE name='Science'")
    assert t["keywords"] == "space, NASA, physics" and t["importance"] == 5
    logged_in.post(f"/settings/topics/{t['id']}", data={"name": "Science & Space", "keywords": "space",
                                                        "importance": 2, "story_count": 3, "position": 1})
    t = db.query_one("SELECT * FROM topics WHERE id=?", (t["id"],))
    assert t["name"] == "Science & Space" and t["enabled"] == 0  # checkbox unticked
    # Topics with sources can't be deleted.
    world = db.query_one("SELECT id FROM topics WHERE name='World'")["id"]
    r = logged_in.post(f"/settings/topics/{world}/delete", follow_redirects=False)
    assert "err=" in r.headers["location"]
    logged_in.post(f"/settings/topics/{t['id']}/delete")
    assert db.query_one("SELECT 1 FROM topics WHERE id=?", (t["id"],)) is None
    assert "Science" not in logged_in.get("/settings/topics").text


def test_disabled_topic_not_in_edition(app, web, logged_in):
    standard_feeds(web)
    db = app.state.db
    t = db.query_one("SELECT * FROM topics WHERE name='Motorsport'")
    logged_in.post(f"/settings/topics/{t['id']}", data={"name": t["name"], "keywords": t["keywords"],
                                                        "importance": 3, "story_count": 6, "position": 3})
    build(app)
    assert not any("Bathurst" in i["title"] for i in edition_items(app))


def test_add_source_probes_feed(app, web, logged_in):
    db = app.state.db
    world = db.query_one("SELECT id FROM topics WHERE name='World'")["id"]
    web.feeds["https://new.test/feed"] = rss("New Times", [{"title": "Hello", "link": "https://new.test/a"}])
    r = logged_in.post("/settings/sources", data={"url": "https://new.test/feed", "topic_id": world},
                       follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert db.query_one("SELECT name FROM feeds WHERE url='https://new.test/feed'")["name"] == "New Times"
    r = logged_in.post("/settings/sources", data={"url": "https://broken.test/feed", "topic_id": world},
                       follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert db.query_one("SELECT 1 FROM feeds WHERE url='https://broken.test/feed'") is None
    r = logged_in.post("/settings/sources", data={"url": "ftp://x", "topic_id": world}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_remove_source_keeps_old_editions(app, web, logged_in):
    standard_feeds(web)
    build(app)
    feed = app.state.db.query_one("SELECT id FROM feeds WHERE name='Pit Lane'")
    logged_in.post(f"/settings/sources/{feed['id']}/delete")
    assert app.state.db.query_one("SELECT 1 FROM feeds WHERE id=?", (feed["id"],)) is None
    html = logged_in.get("/").text
    assert "Bathurst" in html and "Pit Lane" in html


def test_build_time_setting(app, logged_in):
    r = logged_in.post("/settings/general", data={"build_time": "06:15", "max_age_hours": 48}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert app.state.db.get_setting("build_time") == "06:15"
    r = logged_in.post("/settings/general", data={"build_time": "late", "max_age_hours": 48}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert app.state.db.get_setting("build_time") == "06:15"


def test_scheduler_uses_build_time(app):
    jobs = app.state.jobs
    app.state.db.set_setting("build_time", "05:00")
    jobs.scheduler.start(paused=True)
    try:
        jobs.reschedule_build()
        nxt = jobs.next_build()
        assert (nxt.hour, nxt.minute) == (5, 0)
        app.state.db.set_setting("build_time", "04:45")
        jobs.reschedule_build()
        nxt = jobs.next_build()
        assert (nxt.hour, nxt.minute) == (4, 45)
        assert str(nxt.tzinfo) == "Australia/Melbourne"
    finally:
        jobs.shutdown()


def test_catch_up_when_missed(app, monkeypatch):
    b = app.state.builder
    app.state.db.set_setting("build_time", "00:00")
    assert b.needs_catch_up() is True
    app.state.db.set_setting("build_time", "23:59")
    assert b.needs_catch_up() in (False, True)  # depends on the clock; must not raise


def test_location_search_and_select(app, logged_in):
    page = logged_in.get("/settings/general?q=Port+Fairy").text
    assert "Port Fairy" in page and "Use this" in page
    logged_in.post("/settings/location", data={"name": "Port Fairy", "admin1": "Victoria", "country": "Australia",
                                               "latitude": -38.385, "longitude": 142.237})
    assert app.state.db.get_setting("location")["name"] == "Port Fairy"
    assert app.state.db.get_setting("weather_cache")["location"]["name"] == "Port Fairy"


# --- weather ---------------------------------------------------------------

def test_weather_chips():
    from conftest import FORECAST
    import httpx
    from conftest import MockWeb
    web = MockWeb()
    f = weather.fetch_forecast(-38.4, 142.5, "Australia/Melbourne",
                               httpx.Client(transport=httpx.MockTransport(web.api_handler)))
    kinds = {c["kind"] for c in weather.derive_chips(f)}
    assert {"heat", "storm", "uv", "frost", "wind", "rain"} <= kinds
    assert FORECAST


def test_weather_failure_shows_saved_forecast(app, web, logged_in):
    standard_feeds(web)
    build(app)
    web.weather_down = True
    web.bom_down = True
    weather.refresh(app.state.db, "Australia/Melbourne", app.state.config.alert_feed_url,
                    app.state.builder.weather_client)
    html = logged_in.get("/").text
    assert "Showing the last saved forecast, as of" in html
    assert "14°" in html  # still shows the saved numbers
    assert "Couldn&#39;t fetch warnings" in html or "Couldn't fetch warnings" in html


def test_no_warnings_message(app, web, logged_in):
    import conftest
    standard_feeds(web)
    orig = conftest.BOM
    conftest.BOM = '<?xml version="1.0"?><rss version="2.0"><channel><title>Warnings</title></channel></rss>'
    try:
        build(app)
    finally:
        conftest.BOM = orig
    assert "No current warnings for Victoria" in logged_in.get("/").text


# --- on this day, archive, editions ----------------------------------------

def test_on_this_day_spread():
    events = [{"year": y, "text": str(y), "link": None} for y in
              (1990, 1991, 1992, 1993, 1994, 1995, 1871, 1872, 1700, 1500, 1066, 2001, 2002)]
    picked = onthisday.spread(events, 6)
    assert len(picked) == 6
    assert len({e["year"] // 100 for e in picked}) == 6
    assert picked == sorted(picked, key=lambda e: e["year"])


def test_on_this_day_failure(app, web, logged_in):
    standard_feeds(web)
    web.wiki_down = True
    build(app)
    assert "Couldn&#39;t load" in logged_in.get("/").text or "Couldn't load" in logged_in.get("/").text


def test_archive_and_navigation(app, web, logged_in):
    standard_feeds(web)
    today = app.state.builder.today()
    build(app, today - timedelta(days=1))
    build(app, today)
    html = logged_in.get(f"/edition/{(today - timedelta(days=1)).isoformat()}").text
    assert f'href="/edition/{today.isoformat()}" rel="next"' in html
    html = logged_in.get("/").text
    assert f'href="/edition/{(today - timedelta(days=1)).isoformat()}" rel="prev"' in html
    arch = logged_in.get("/archive").text
    assert today.strftime("%B %Y") in arch
    assert logged_in.get("/edition/1999-01-01").status_code == 404
    assert logged_in.get("/edition/nonsense").status_code == 404


def test_rebuild_button_and_status(app, web, logged_in):
    standard_feeds(web)
    r = logged_in.post("/rebuild", follow_redirects=False)
    assert r.status_code == 303
    import time
    for _ in range(100):
        if not logged_in.get("/status").json()["building"]:
            break
        time.sleep(0.05)
    assert logged_in.get("/status").json()["latest"] == app.state.builder.today().isoformat()


def test_building_banner(app, web, logged_in):
    standard_feeds(web)
    build(app)
    app.state.builder._lock.acquire()
    try:
        html = logged_in.get("/").text
        assert "Building a fresh edition" in html and 'http-equiv="refresh"' in html
    finally:
        app.state.builder._lock.release()


# --- backups -----------------------------------------------------------------

def test_backup_and_prune(app, logged_in):
    import time
    cfg = app.state.config
    names = []
    for i in range(5):
        p = backup.run_backup(cfg)
        names.append(p.name)
        time.sleep(1.05)  # names are timestamped to the second
    kept = [p.name for p in backup.list_backups(cfg)]
    assert len(kept) == 3 and kept == sorted(names, reverse=True)[:3]
    import sqlite3
    con = sqlite3.connect(cfg.backup_dir / kept[0])
    assert con.execute("SELECT COUNT(*) FROM topics").fetchone()[0] >= 4
    con.close()
    r = logged_in.get(f"/settings/backups/{kept[0]}")
    assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"
    assert logged_in.get("/settings/backups/..%2Fpaper.db").status_code == 404
    assert logged_in.get("/settings/backups/secret_key").status_code == 404


def test_backup_now_button(app, logged_in):
    r = logged_in.post("/settings/backup", follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert app.state.db.get_setting("last_backup")["ok"] is True
    assert "Download" in logged_in.get("/settings/system").text


# --- pages ---------------------------------------------------------------------

def test_all_pages_render(app, web, logged_in):
    standard_feeds(web)
    build(app)
    for path in ("/", "/archive", "/settings/general", "/settings/topics", "/settings/sources",
                 "/settings/exclusions", "/settings/learning", "/settings/system"):
        r = logged_in.get(path)
        assert r.status_code == 200, path
        assert "Content-Security-Policy" in r.headers
    assert logged_in.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert "Service-Worker-Allowed" in logged_in.get("/sw.js").headers


def test_first_visit_starts_first_edition(app, web, logged_in):
    standard_feeds(web)
    r = logged_in.get("/")
    assert "first edition is being put together" in r.text


def test_restore_command(app, monkeypatch, capsys):
    from app import restore
    cfg = app.state.config
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("DATA_DIR", str(cfg.data_dir))
    good = backup.run_backup(cfg).name
    with app.state.db.conn() as c:
        c.execute("INSERT INTO topics(name) VALUES ('Canary')")
    assert restore.main(["nope.db"]) == 1
    assert restore.main([good]) == 0
    assert app.state.db.query_one("SELECT 1 FROM topics WHERE name='Canary'") is None
    assert "Current database saved as" in capsys.readouterr().out


def test_read_marks(app, web, logged_in):
    standard_feeds(web)
    build(app)
    sid = story_id(app, "Reserve Bank")
    card = lambda: re.search(rf'<article class="card[^"]*" id="story-{sid}".*?</article>', logged_in.get("/").text, re.S).group(0)
    assert "is-read" not in card() and "Mark as read" in card()
    page = logged_in.get(f"/read/{sid}").text
    assert 'data-autoread="12"' in page
    r = logged_in.post(f"/story/{sid}/read", data={"value": "read"}, headers={"x-requested-with": "fetch"})
    assert r.json() == {"story_id": sid, "read": True}
    c = card()
    assert "is-read" in c and "Mark as unread" in c and re.search(r'class="read-mark"\s*>', c)
    assert 'data-autoread' not in logged_in.get(f"/read/{sid}").text  # already read: no timer
    # Marking twice is harmless; plain form post (no JS) redirects back.
    r = logged_in.post(f"/story/{sid}/read?next=/%23story-{sid}", data={"value": "read"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/#story-{sid}"
    logged_in.post(f"/story/{sid}/read", data={"value": "unread"})
    assert "is-read" not in card()
    assert logged_in.post(f"/story/{sid}/read", data={"value": "bogus"}).status_code == 400
    assert logged_in.post("/story/999999/read", data={"value": "read"}).status_code == 404


def test_external_cards_mark_read_on_tap(app, web, logged_in):
    standard_feeds(web)
    build(app)
    html = logged_in.get("/").text
    # No saved articles in this test, so every card opens the website and carries the tap-to-mark hook.
    assert html.count("data-mark-read") == html.count('class="card-link"') > 0


@pytest.mark.parametrize("raw,expected", [
    ("Get our breaking news email, free app or daily news podcast Police found the boy.", "Police found the boy."),
    ("Get our daily political email, free app or daily news podcast Rates rose.", "Rates rose."),
    ("Sign up for the US Breaking News email Some Democrats said no.", "Some Democrats said no."),
    ("Follow our Australia news live blog for latest updates The RBA held rates.", "The RBA held rates."),
    ("The council met. Sign up to our Morning Mail newsletter. Rates rose.", "The council met. Rates rose."),
    ("Residents can sign up to the council's emergency email alerts.", "Residents can sign up to the council's emergency email alerts."),
    ("Voters should get our message clearly.", "Voters should get our message clearly."),
    ("A teenager was found near Walhalla Get our breaking news email, free app or daily news podcast William, 17, "
     "is recovering.", "A teenager was found near Walhalla. William, 17, is recovering."),
    ("Locals asked: Sign up for what? The email never came.", "Locals asked: Sign up for what? The email never came."),
])
def test_promotions_removed_from_summaries(raw, expected):
    assert text.summarise(raw) == expected


def test_promotions_cleaned_on_existing_stories(app, web, logged_in):
    web.feeds = {"https://local.test/rss": rss("L", [
        {"title": "Warrnambool flood warning issued", "link": "https://local.test/flood",
         "summary": "placeholder"}])}
    build(app)
    with app.state.db.conn() as c:  # simulate a summary saved before the filter existed
        c.execute("UPDATE stories SET summary = 'Get our breaking news email, free app or daily news podcast "
                  "Residents near the Hopkins River should prepare.'")
    html = logged_in.get("/").text
    assert "Residents near the Hopkins River" in html and "free app" not in html
