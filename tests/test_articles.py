"""Reader view: full articles and photos copied locally at build time."""
from __future__ import annotations

import asyncio
import io
import json
import re
from datetime import timedelta

import httpx
import pytest
from PIL import Image

from app import articles
from app.db import iso, utcnow
from conftest import rss, standard_feeds

PARAS = " ".join(f"<p>Paragraph {i} of the council story with plenty of ordinary words to read.</p>" for i in range(12))


def photo(w=1200, h=800, color=(30, 90, 160), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, fmt)
    return buf.getvalue()


def article_page(title: str, body: str, og: str = "") -> str:
    og_tag = f'<meta property="og:image" content="{og}">' if og else ""
    return (f"<html><head><title>{title}</title>{og_tag}<meta name='author' content='Jane Reporter'>"
            f"<script>window.evil=1</script></head><body><nav>Home | News | Sport</nav>"
            f"<article><h1>{title}</h1>{body}</article><footer>Copyright</footer></body></html>")


def setup_pages(web):
    standard_feeds(web)
    title = "Warrnambool council approves new breakwater upgrade"
    web.pages["https://local.test/news/breakwater"] = article_page(
        title,
        '<p>The council voted <b>unanimously</b> on <a href="javascript:alert(1)">Tuesday</a>.</p>'
        '<figure><img src="/img/harbour.jpg" alt="The harbour at dawn" onerror="alert(2)">'
        '<figcaption>The harbour at dawn</figcaption></figure>'
        '<p style="color:red" onclick="x()">Work starts in March.</p>'
        '<img src="https://local.test/img/pixel.gif" alt="">'
        '<iframe src="https://evil.test"></iframe><script>alert(3)</script>' + PARAS)
    web.images["https://local.test/img/harbour.jpg"] = photo()
    web.images["https://local.test/img/pixel.gif"] = photo(1, 1, fmt="GIF")
    # A page with no inline photo but a social-media lead image.
    web.pages["https://tech.test/linux"] = article_page(
        "New Linux kernel brings faster file systems", PARAS, og="https://tech.test/og/linux.png")
    web.images["https://tech.test/og/linux.png"] = photo(2400, 1600, fmt="PNG")
    # A paywalled page: a teaser only.
    web.pages["https://tech.test/apple"] = article_page("Apple announces new tablet", "<p>Subscribe to read.</p>")


def build(app):
    return app.state.builder.build(reason="test")


def sid(app, fragment):
    return app.state.db.query_one("SELECT id FROM stories WHERE title LIKE ?", (f"%{fragment}%",))["id"]


def test_build_saves_articles_and_photos_locally(app, web, logged_in):
    setup_pages(web)
    result = build(app)
    assert result["articles"]["ok"] >= 2
    s = sid(app, "breakwater")
    page = logged_in.get(f"/read/{s}").text
    assert "Work starts in March." in page and "unanimously" in page
    assert "By Jane Reporter" in page
    # Photos point at our own server, never the publisher's.
    srcs = re.findall(r'<img src="([^"]+)"', page)
    assert srcs and all(src.startswith("/img/") for src in srcs)
    assert 'alt="The harbour at dawn"' in page
    # The 1x1 tracking pixel was not kept.
    assert len(srcs) == 1
    img = logged_in.get(srcs[0])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(img.content)).size == (1200, 800)
    # The page's own headline isn't repeated inside the body.
    assert page.count("Warrnambool council approves new breakwater upgrade</") == 1


def test_article_html_is_sanitised(app, web, logged_in):
    setup_pages(web)
    build(app)
    body = app.state.db.query_one("SELECT body FROM articles WHERE story_id = ?", (sid(app, "breakwater"),))["body"]
    for bad in ("script", "iframe", "onerror", "onclick", "style=", "javascript:", "<a ", "evil", "Copyright",
                "Home | News"):
        assert bad not in body, bad
    assert "<strong>unanimously</strong>" in body


def test_lead_image_resized(app, web, logged_in):
    setup_pages(web)
    build(app)
    row = app.state.db.query_one("SELECT lead_image FROM articles WHERE story_id = ?", (sid(app, "Linux kernel"),))
    assert row["lead_image"]
    data = (app.state.config.image_dir / row["lead_image"]).read_bytes()
    assert max(Image.open(io.BytesIO(data)).size) == articles.IMAGE_MAX_SIDE
    assert f'src="/img/{row["lead_image"]}"' in logged_in.get(f"/read/{sid(app, 'Linux kernel')}").text


def test_cards_link_to_reader_or_source(app, web, logged_in):
    setup_pages(web)
    build(app)
    html = logged_in.get("/").text
    ok = sid(app, "breakwater")
    paywalled = sid(app, "Apple announces")
    ok_card = re.search(rf'id="story-{ok}".*?</article>', html, re.S).group(0)
    assert f'href="/read/{ok}"' in ok_card and "opens website" not in ok_card
    bad_card = re.search(rf'id="story-{paywalled}".*?</article>', html, re.S).group(0)
    assert "/read/" not in bad_card and "opens website ↗" in bad_card and "https://tech.test/apple" in bad_card


def test_failed_article_fallback_and_retry(app, web, logged_in):
    setup_pages(web)
    build(app)
    s = sid(app, "Apple announces")
    page = logged_in.get(f"/read/{s}").text
    assert "couldn&#39;t be saved" in page or "couldn't be saved" in page
    assert "paywalled" in page and "Try fetching it now" in page
    web.pages["https://tech.test/apple"] = article_page("Apple announces new tablet", PARAS)
    r = logged_in.post(f"/read/{s}/fetch", follow_redirects=False)
    assert r.headers["location"] == f"/read/{s}"
    assert "Paragraph 5" in logged_in.get(f"/read/{s}").text


def test_site_refusal_message(app, web, logged_in):
    setup_pages(web)
    web.fail["https://pit.test/bathurst"] = "500"
    web.pages.pop("https://pit.test/f1", None)
    build(app)
    row = app.state.db.query_one("SELECT status, error FROM articles WHERE story_id = ?", (sid(app, "Bathurst"),))
    assert row["status"] == "failed" and "HTTP 500" in row["error"]


def test_readable_with_internet_down(app, web, logged_in):
    """Built at 5 am, internet gone at 6 am: everything is still served from the server."""
    setup_pages(web)
    build(app)
    s = sid(app, "breakwater")
    web.pages.clear()
    web.images.clear()
    web.fail = {u: "timeout" for u in web.feeds}
    web.weather_down = web.bom_down = web.wiki_down = True
    assert logged_in.get("/").status_code == 200
    page = logged_in.get(f"/read/{s}").text
    assert "Work starts in March." in page
    src = re.search(r'<img src="(/img/[^"]+)"', page).group(1)
    assert logged_in.get(src).status_code == 200


def test_rebuild_does_not_refetch_saved_articles(app, web):
    setup_pages(web)
    build(app)
    before = web.calls.get("https://local.test/news/breakwater", 0)
    result = build(app)
    assert web.calls.get("https://local.test/news/breakwater", 0) == before
    assert result["articles"]["already_saved"] >= 2


def test_images_require_login_and_valid_names(app, web, client):
    setup_pages(web)
    build(app)
    name = app.state.db.query_one("SELECT images FROM articles WHERE status='ok' AND images != '[]'")["images"]
    name = json.loads(name)[0]
    assert client.get(f"/img/{name}", headers={"accept": "application/json"}).status_code == 401
    client.post("/login", data={"password": "correct horse battery staple"})
    assert client.get(f"/img/{name}").status_code == 200
    assert client.get("/img/..%2F/paper.db").status_code == 404
    assert client.get("/img/ab/secret_key").status_code == 404


def test_cleanup_removes_old_articles_and_orphan_photos(app, web):
    setup_pages(web)
    build(app)
    db, image_dir = app.state.db, app.state.config.image_dir
    old = sid(app, "breakwater")
    old_images = json.loads(db.query_one("SELECT images FROM articles WHERE story_id=?", (old,))["images"])
    with db.conn() as c:
        c.execute("UPDATE articles SET fetched_at = ? WHERE story_id = ?", (iso(utcnow() - timedelta(days=90)), old))
    (image_dir / "ab").mkdir(parents=True, exist_ok=True)
    (image_dir / "ab" / "leftover.part").write_bytes(b"x")
    stats = articles.cleanup(db, image_dir, keep_days=60)
    assert stats["articles_removed"] == 1
    assert all(not (image_dir / n).exists() for n in old_images)
    kept = db.query_one("SELECT lead_image FROM articles WHERE story_id=?", (sid(app, "Linux kernel"),))["lead_image"]
    assert (image_dir / kept).exists()
    assert not (image_dir / "ab" / "leftover.part").exists()


def test_keep_days_setting(app, logged_in):
    logged_in.post("/settings/general", data={"build_time": "05:00", "max_age_hours": 36, "article_keep_days": 30})
    assert app.state.db.get_setting("article_keep_days") == 30
    assert "Saved articles" in logged_in.get("/settings/system").text


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.10", "10.0.0.1", "localhost", "nas.local", "[::1]"])
def test_private_addresses_blocked(host):
    async def go():
        async def ok(request):
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(ok),
                                     event_hooks={"request": [articles.block_private_hosts]}) as c:
            await c.get(f"http://{host}/admin")
    with pytest.raises(articles.BlockedAddress):
        asyncio.run(go())


def test_public_address_allowed():
    async def go():
        async def ok(request):
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(ok),
                                     event_hooks={"request": [articles.block_private_hosts]}) as c:
            return await c.get("http://93.184.216.34/")
    assert asyncio.run(go()).status_code == 200


def test_process_image_rejects_junk():
    assert articles.process_image(b"not an image") is None
    assert articles.process_image(photo(100, 100)) is None
    out = articles.process_image(photo(3000, 2000, fmt="PNG"))
    assert Image.open(io.BytesIO(out)).size == (1600, 1067)


def test_page_furniture_removed():
    body = ("<ul><li>Published</li><li>Published12 hours ago</li><li>Updated 3 May 2026</li></ul>"
            "<p>Share</p><p>Image source, Getty Images</p><figcaption>Image caption, Crews at work</figcaption>"
            "<p>Sign up for our Morning Mail newsletter</p><p>The real story starts here.</p>"
            "<ul><li>Published by the council, the plan was adopted.</li></ul>")
    out = articles.clean_body(body)
    assert out == ("<figcaption>Crews at work</figcaption><p>The real story starts here.</p>"
                   "<ul><li>Published by the council, the plan was adopted.</li></ul>")
    assert articles.clean_body(out) == out  # running twice changes nothing


def test_old_saved_articles_cleaned_when_shown(app, web, logged_in):
    setup_pages(web)
    build(app)
    s = sid(app, "breakwater")
    with app.state.db.conn() as c:
        c.execute("UPDATE articles SET body = '<ul><li>Published 1 day ago</li></ul>' || body WHERE story_id = ?", (s,))
    page = logged_in.get(f"/read/{s}").text
    assert "Published 1 day ago" not in page and "Work starts in March." in page
