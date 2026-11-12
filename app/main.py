"""FastAPI app: routes, auth middleware, template helpers."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import articles, backup, learning, text, weather
from . import feeds as feedmod
from .auth import COOKIE, MAX_AGE, Auth, load_secret
from .config import Config, load_config
from .db import Database, parse_iso, utcnow
from .edition import Builder, parse_build_time
from .ranking import effective_label
from .scheduler import Jobs

log = logging.getLogger("paper")
HERE = Path(__file__).parent
PUBLIC_PATHS = {"/login", "/healthz", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}
TREAT_AS = ("auto", "reporting", "analysis", "opinion")


def create_app(config: Config | None = None, builder_kwargs: dict[str, Any] | None = None) -> FastAPI:
    config = config or load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)
    db.init()
    auth = Auth(config.app_password, load_secret(config.data_dir))
    builder = Builder(db, config, **(builder_kwargs or {}))
    jobs = Jobs(builder)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not config.disable_scheduler:
            jobs.start()
        yield
        jobs.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config, app.state.db, app.state.builder, app.state.jobs, app.state.auth = config, db, builder, jobs, auth
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(paper_name=config.paper_name, label_names=text.LABEL_NAMES)

    # --- template helpers ----------------------------------------------
    def local(value: Any) -> datetime | None:
        dt = parse_iso(value) if isinstance(value, str) else value
        return dt.astimezone(config.tz) if dt else None

    def fmt_time(value: Any, ref: date | None = None) -> str:
        dt = local(value)
        if not dt:
            return ""
        t = dt.strftime("%I:%M %p").lstrip("0").lower()
        if ref is not None and dt.date() == ref:
            return t
        if ref is not None and (ref - dt.date()).days == 1:
            return f"Yesterday {t}"
        return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}, {t}"

    def long_date(d: date | str) -> str:
        d = date.fromisoformat(d) if isinstance(d, str) else d
        return f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}"

    def num(v: Any, digits: int = 0) -> str:
        if v is None:
            return "–"
        return f"{float(v):.{digits}f}"

    env.filters.update(fmt_time=fmt_time, long_date=long_date, local=local, num=num)

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg", "")[:300])
        ctx.setdefault("err", request.query_params.get("err", "")[:300])
        ctx["building"] = builder.building
        ctx["build_phase"] = builder.phase
        ctx["path"] = request.url.path
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def back(request: Request, default: str = "/", **params: str) -> RedirectResponse:
        target = default
        nxt = request.query_params.get("next") or ""
        if safe_next(nxt):
            target = nxt
        if params:
            frag = ""
            if "#" in target:
                target, frag = target.split("#", 1)
                frag = "#" + frag
            sep = "&" if "?" in target else "?"
            target += sep + "&".join(f"{k}={quote(v)}" for k, v in params.items()) + frag
        return RedirectResponse(target, status_code=303)

    # --- middleware ----------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                if urlsplit(origin).netloc != request.headers.get("host", ""):
                    return Response("Cross-site request refused", status_code=403)
        if not (path in PUBLIC_PATHS or path.startswith("/static/")):
            if not auth.valid_cookie(request.cookies.get(COOKIE)):
                wants_json = request.headers.get("accept", "").startswith("application/json") or is_fetch(request)
                if request.method == "GET" and not wants_json:
                    nxt = path + (f"?{request.url.query}" if request.url.query else "")
                    return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
                return JSONResponse({"error": "login required"}, status_code=401)
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path not in ("/sw.js",) and not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def client_id(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # --- public --------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        db.query_one("SELECT 1")
        return {"ok": True, "building": builder.building}

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({
            "name": config.paper_name, "short_name": config.paper_name[:12], "start_url": "/",
            "scope": "/", "display": "standalone", "background_color": "#f7f3ea", "theme_color": "#1d1b17",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }, media_type="application/manifest+json")

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(HERE / "static" / "icon-192.png", media_type="image/png")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if auth.valid_cookie(request.cookies.get(COOKIE)):
            return back(request)
        return render(request, "login.html", locked=auth.locked_for(client_id(request)),
                      next=request.query_params.get("next", "/"))

    @app.post("/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/")):
        cid = client_id(request)
        nxt = next if safe_next(next) else "/"
        locked = auth.locked_for(cid)
        if locked:
            return render(request, "login.html", status=429, locked=locked, next=nxt)
        if auth.check_password(password):
            auth.record_success(cid)
            resp = RedirectResponse(nxt, status_code=303)
            resp.set_cookie(COOKIE, auth.make_cookie(), max_age=MAX_AGE, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")
            return resp
        left = auth.record_failure(cid)
        if left == 0:
            return render(request, "login.html", status=429, locked=auth.locked_for(cid), next=nxt)
        return render(request, "login.html", status=401, next=nxt, locked=0,
                      error=f"That password didn't work. {left} attempt{'s' if left != 1 else ''} left "
                            f"before a 5-minute lockout.")

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # --- editions ------------------------------------------------------
    def latest_date() -> str | None:
        row = db.query_one("SELECT date FROM editions ORDER BY date DESC LIMIT 1")
        return row["date"] if row else None

    def edition_context(ed_row) -> dict[str, Any]:
        data = json.loads(ed_row["data"])
        ed_date = date.fromisoformat(ed_row["date"])
        items = [dict(r) for r in db.query(
            "SELECT i.*, s.title, s.url, s.summary, s.published_at, s.fetched_at, s.label_auto, s.label_override, "
            "s.interest, s.feed_id, COALESCE(f.name, s.source_name) AS source, f.treat_as, a.status AS article_status "
            "FROM edition_items i JOIN stories s ON s.id = i.story_id LEFT JOIN feeds f ON f.id = s.feed_id "
            "LEFT JOIN articles a ON a.story_id = s.id "
            "WHERE i.edition_id = ? ORDER BY i.section = 'lead' DESC, i.position", (ed_row["id"],))]
        also_ids = {sid for it in items for sid in json.loads(it["also"])}
        also_rows = {}
        if also_ids:
            marks = ",".join("?" * len(also_ids))
            for r in db.query(f"SELECT s.id, s.url, COALESCE(f.name, s.source_name) AS source FROM stories s "
                              f"LEFT JOIN feeds f ON f.id = s.feed_id WHERE s.id IN ({marks})", tuple(also_ids)):
                also_rows[r["id"]] = dict(r)
        for it in items:
            it["label"] = effective_label(it)
            it["label_corrected"] = it.get("label_override") in text.LABELS
            it["also_list"] = [also_rows[s] for s in json.loads(it["also"]) if s in also_rows]
        topic_rows = {str(t["id"]): dict(t) for t in db.query("SELECT * FROM topics")}
        names = data.get("topics", {})
        sections: dict[str, dict[str, Any]] = {}
        for it in items:
            if it["section"] == "lead":
                continue
            sec = sections.setdefault(it["section"], {
                "name": (topic_rows.get(it["section"]) or {}).get("name") or names.get(it["section"]) or "Other",
                "position": (topic_rows.get(it["section"]) or {}).get("position", 99), "items": []})
            sec["items"].append(it)
        is_latest = ed_row["date"] == latest_date()
        snap = weather.snapshot(db) if is_latest else data.get("snapshot", {})
        prev_row = db.query_one("SELECT date FROM editions WHERE date < ? ORDER BY date DESC LIMIT 1",
                                (ed_row["date"],))
        next_row = db.query_one("SELECT date FROM editions WHERE date > ? ORDER BY date ASC LIMIT 1",
                                (ed_row["date"],))
        return {
            "edition": dict(ed_row), "data": data, "ed_date": ed_date,
            "leads": [it for it in items if it["section"] == "lead"],
            "sections": sorted(sections.values(), key=lambda s: s["position"]),
            "weather": weather.view(snap, config.tz, today=ed_date),
            "onthisday": data.get("onthisday") or {},
            "prev_date": prev_row["date"] if prev_row else None,
            "next_date": next_row["date"] if next_row else None,
            "is_latest": is_latest,
            "is_today": ed_date == builder.today(),
            "built_at": local(ed_row["built_at"]),
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        d = latest_date()
        if d is None:
            if not builder.building:
                builder.start_background(reason="first edition")
            return render(request, "empty.html", building=True)
        return show_edition(request, d)

    @app.get("/edition/{day}", response_class=HTMLResponse)
    def show_edition(request: Request, day: str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(404)
        row = db.query_one("SELECT * FROM editions WHERE date = ?", (day,))
        if row is None:
            raise HTTPException(404, "No edition for that date")
        return render(request, "edition.html", **edition_context(row))

    @app.get("/archive", response_class=HTMLResponse)
    def archive(request: Request):
        rows = db.query("SELECT e.date, e.offline, (SELECT COUNT(*) FROM edition_items i WHERE i.edition_id = e.id) "
                        "AS n FROM editions e ORDER BY e.date DESC")
        months: list[dict[str, Any]] = []
        for r in rows:
            d = date.fromisoformat(r["date"])
            label = d.strftime("%B %Y")
            if not months or months[-1]["label"] != label:
                months.append({"label": label, "days": []})
            months[-1]["days"].append({"date": r["date"], "d": d, "n": r["n"], "offline": r["offline"]})
        return render(request, "archive.html", months=months)

    @app.post("/rebuild")
    def rebuild(request: Request):
        started = builder.start_background(reason="rebuild now")
        if is_fetch(request):
            return JSONResponse({"building": True, "started": started})
        return RedirectResponse("/?msg=" + quote("Rebuilding today's edition…"), status_code=303)

    @app.get("/status")
    def status():
        return {"building": builder.building, "latest": latest_date()}

    # --- story feedback ------------------------------------------------
    @app.post("/story/{story_id}/feedback")
    def feedback(request: Request, story_id: int, value: str = Form(...)):
        v = {"interested": 1, "not_interested": -1}.get(value)
        if v is None:
            raise HTTPException(400, "Unknown feedback")
        try:
            state = learning.set_feedback(db, story_id, v)
        except KeyError:
            raise HTTPException(404)
        if is_fetch(request):
            return JSONResponse({"story_id": story_id, "interest": state})
        return back(request, f"/#story-{story_id}")

    @app.post("/story/{story_id}/label")
    def correct_label(request: Request, story_id: int, label: str = Form(...)):
        if label not in (*text.LABELS, "auto"):
            raise HTTPException(400, "Unknown label")
        with db.conn() as c:
            cur = c.execute("UPDATE stories SET label_override = ? WHERE id = ?",
                            (None if label == "auto" else label, story_id))
            if cur.rowcount == 0:
                raise HTTPException(404)
        return back(request, f"/#story-{story_id}")

    # --- reader view -----------------------------------------------------
    @app.get("/read/{story_id}", response_class=HTMLResponse)
    def read_story(request: Request, story_id: int):
        row = db.query_one(
            "SELECT s.*, s.id AS story_id, COALESCE(f.name, s.source_name) AS source, f.treat_as, "
            "a.status AS article_status, a.body, a.byline, a.lead_image, a.words, a.error AS article_error, "
            "a.fetched_at AS article_fetched_at FROM stories s LEFT JOIN feeds f ON f.id = s.feed_id "
            "LEFT JOIN articles a ON a.story_id = s.id WHERE s.id = ?", (story_id,))
        if row is None:
            raise HTTPException(404, "That story isn't stored any more.")
        it = dict(row)
        it["label"] = effective_label(it)
        it["label_corrected"] = it.get("label_override") in text.LABELS
        placed = db.query_one(
            "SELECT e.date, i.also, i.wildcard FROM edition_items i JOIN editions e ON e.id = i.edition_id "
            "WHERE i.story_id = ? ORDER BY e.date DESC LIMIT 1", (story_id,))
        it["also_list"], it["wildcard"] = [], 0
        ed_date = builder.today()
        back_url = f"/#story-{story_id}"
        if placed:
            ed_date = date.fromisoformat(placed["date"])
            it["wildcard"] = placed["wildcard"]
            if placed["date"] != latest_date():
                back_url = f"/edition/{placed['date']}#story-{story_id}"
            ids = json.loads(placed["also"])
            if ids:
                marks = ",".join("?" * len(ids))
                it["also_list"] = [dict(r) for r in db.query(
                    f"SELECT s.id, s.url, COALESCE(f.name, s.source_name) AS source FROM stories s "
                    f"LEFT JOIN feeds f ON f.id = s.feed_id WHERE s.id IN ({marks})", tuple(ids))]
        return render(request, "read.html", it=it, ed_date=ed_date, back_url=back_url,
                      body=Markup(it["body"]) if it.get("article_status") == "ok" else None)

    @app.post("/read/{story_id}/fetch")
    async def fetch_story_now(story_id: int):
        row = db.query_one("SELECT id, url, title FROM stories WHERE id = ?", (story_id,))
        if row is None:
            raise HTTPException(404)
        await articles.fetch_many(db, [dict(row)], config.image_dir, builder.article_client, overall_timeout=60)
        status = db.query_one("SELECT status FROM articles WHERE story_id = ?", (story_id,))
        if status and status["status"] == "ok":
            return RedirectResponse(f"/read/{story_id}", status_code=303)
        return RedirectResponse(f"/read/{story_id}?err=" + quote("Still couldn't fetch the full article."),
                                status_code=303)

    @app.get("/img/{prefix}/{name}")
    def stored_image(prefix: str, name: str):
        rel = f"{prefix}/{name}"
        if not articles.IMAGE_NAME.match(rel):
            raise HTTPException(404)
        path = config.image_dir / rel
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    # --- settings ------------------------------------------------------
    @app.get("/settings")
    def settings_root():
        return RedirectResponse("/settings/general", status_code=303)

    @app.get("/settings/general", response_class=HTMLResponse)
    def settings_general(request: Request, q: str = ""):
        results, search_error = [], None
        if q.strip():
            try:
                results = weather.geocode(q, builder.weather_client() if builder.weather_client else None)
            except Exception as exc:
                search_error = f"Location search failed ({type(exc).__name__}). Check the server's internet access."
        return render(request, "settings_general.html", tab="general",
                      build_time=db.get_setting("build_time", "05:00"),
                      max_age=db.get_setting("max_age_hours", 36),
                      keep_days=db.get_setting("article_keep_days", 60),
                      location=db.get_setting("location"), q=q, results=results, search_error=search_error,
                      next_build=jobs.next_build(), tz_name=config.tz_name)

    @app.post("/settings/general")
    def save_general(build_time: str = Form(...), max_age_hours: int = Form(36), article_keep_days: int = Form(60)):
        if not re.fullmatch(r"\d{1,2}:\d{2}", build_time.strip()):
            return redirect_settings("general", err="Build time must look like 05:00")
        hh, mm = parse_build_time(build_time.strip())
        db.set_setting("build_time", f"{hh:02d}:{mm:02d}")
        db.set_setting("max_age_hours", max(6, min(168, max_age_hours)))
        db.set_setting("article_keep_days", max(3, min(365, article_keep_days)))
        if jobs.scheduler.running:
            jobs.reschedule_build()
        return redirect_settings("general", msg=f"Saved. The paper will be built daily at {hh:02d}:{mm:02d}.")

    @app.post("/settings/location")
    def save_location(name: str = Form(...), admin1: str = Form(""), country: str = Form(""),
                      latitude: float = Form(...), longitude: float = Form(...)):
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return redirect_settings("general", err="That location's coordinates look wrong.")
        db.set_setting("location", {"name": name[:80], "admin1": admin1[:80], "country": country[:80],
                                    "latitude": latitude, "longitude": longitude})
        db.set_setting("weather_cache", None)
        db.set_setting("weather_error", None)
        weather.refresh(db, config.tz_name, config.alert_feed_url, builder.weather_client or weather._client)
        return redirect_settings("general", msg=f"Weather location set to {name}.")

    # topics
    @app.get("/settings/topics", response_class=HTMLResponse)
    def settings_topics(request: Request):
        topics = [dict(t) for t in db.query(
            "SELECT t.*, (SELECT COUNT(*) FROM feeds f WHERE f.topic_id = t.id) AS feed_count "
            "FROM topics t ORDER BY t.position, t.id")]
        return render(request, "settings_topics.html", tab="topics", topics=topics)

    def topic_fields(name: str, keywords: str, importance: int, story_count: int, position: int):
        name = name.strip()[:60]
        if not name:
            raise ValueError("A topic needs a name.")
        kws = ", ".join(text.split_list(keywords))[:2000]
        return name, kws, max(1, min(5, importance)), max(1, min(20, story_count)), max(0, min(99, position))

    @app.post("/settings/topics")
    def add_topic(name: str = Form(...), keywords: str = Form(""), importance: int = Form(3),
                  story_count: int = Form(6), position: int = Form(50)):
        try:
            fields = topic_fields(name, keywords, importance, story_count, position)
        except ValueError as exc:
            return redirect_settings("topics", err=str(exc))
        with db.conn() as c:
            c.execute("INSERT INTO topics(name, keywords, importance, story_count, position) VALUES (?,?,?,?,?)",
                      fields)
        return redirect_settings("topics", msg=f"Added topic “{fields[0]}”. Now give it some sources.")

    @app.post("/settings/topics/{topic_id}")
    def update_topic(topic_id: int, name: str = Form(...), keywords: str = Form(""), importance: int = Form(3),
                     story_count: int = Form(6), position: int = Form(0), enabled: str = Form("")):
        try:
            fields = topic_fields(name, keywords, importance, story_count, position)
        except ValueError as exc:
            return redirect_settings("topics", err=str(exc))
        with db.conn() as c:
            c.execute("UPDATE topics SET name=?, keywords=?, importance=?, story_count=?, position=?, enabled=? "
                      "WHERE id=?", (*fields, 1 if enabled else 0, topic_id))
        return redirect_settings("topics", msg=f"Saved “{fields[0]}”. Changes apply from the next build.")

    @app.post("/settings/topics/{topic_id}/delete")
    def delete_topic(topic_id: int):
        n = db.query_one("SELECT COUNT(*) AS n FROM feeds WHERE topic_id = ?", (topic_id,))["n"]
        if n:
            return redirect_settings("topics", err=f"That topic still has {n} source(s). "
                                                   f"Move or remove them on the Sources tab first.")
        with db.conn() as c:
            c.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        return redirect_settings("topics", msg="Topic removed.")

    # sources
    @app.get("/settings/sources", response_class=HTMLResponse)
    def settings_sources(request: Request):
        feeds = [dict(f) for f in db.query(
            "SELECT f.*, t.name AS topic_name FROM feeds f LEFT JOIN topics t ON t.id = f.topic_id "
            "ORDER BY t.position, f.name COLLATE NOCASE")]
        topics = [dict(t) for t in db.query("SELECT id, name FROM topics ORDER BY position, id")]
        return render(request, "settings_sources.html", tab="sources", feeds=feeds, topics=topics,
                      treat_options=TREAT_AS)

    @app.post("/settings/sources")
    async def add_source(url: str = Form(...), name: str = Form(""), topic_id: int = Form(...),
                         priority: int = Form(3), treat_as: str = Form("auto")):
        url = url.strip()
        if not re.match(r"^https?://[^\s/]+", url):
            return redirect_settings("sources", err="The feed address must start with http:// or https://")
        if db.query_one("SELECT 1 FROM feeds WHERE url = ?", (url,)):
            return redirect_settings("sources", err="That feed is already in your list.")
        if not db.query_one("SELECT 1 FROM topics WHERE id = ?", (topic_id,)):
            return redirect_settings("sources", err="Pick a topic for the new source.")
        result = await feedmod.probe_feed(url, builder.client_factory)
        if not result.ok:
            return redirect_settings("sources", err=f"Couldn't read that feed: {result.error}. Nothing was added.")
        final_name = (name.strip() or result.feed_title or urlsplit(url).hostname or url)[:80]
        with db.conn() as c:
            c.execute("INSERT INTO feeds(name, url, topic_id, priority, treat_as, created_at) VALUES (?,?,?,?,?,?)",
                      (final_name, url, topic_id, max(1, min(5, priority)),
                       treat_as if treat_as in TREAT_AS else "auto", utcnow().isoformat(timespec="seconds")))
        return redirect_settings("sources", msg=f"Added “{final_name}” ({len(result.entries)} items in the feed "
                                                f"right now). It will be used from the next build.")

    @app.post("/settings/sources/{feed_id}")
    def update_source(feed_id: int, name: str = Form(...), topic_id: int = Form(...), priority: int = Form(3),
                      treat_as: str = Form("auto")):
        if not db.query_one("SELECT 1 FROM topics WHERE id = ?", (topic_id,)):
            return redirect_settings("sources", err="Unknown topic.")
        with db.conn() as c:
            c.execute("UPDATE feeds SET name=?, topic_id=?, priority=?, treat_as=? WHERE id=?",
                      (name.strip()[:80] or "Untitled", topic_id, max(1, min(5, priority)),
                       treat_as if treat_as in TREAT_AS else "auto", feed_id))
        return redirect_settings("sources", msg="Source saved.")

    @app.post("/settings/sources/{feed_id}/pause")
    def toggle_pause(feed_id: int):
        with db.conn() as c:
            c.execute("UPDATE feeds SET paused = 1 - paused WHERE id = ?", (feed_id,))
        row = db.query_one("SELECT name, paused FROM feeds WHERE id = ?", (feed_id,))
        if not row:
            raise HTTPException(404)
        return redirect_settings("sources", msg=f"“{row['name']}” {'paused' if row['paused'] else 'resumed'}.")

    @app.post("/settings/sources/{feed_id}/delete")
    def delete_source(feed_id: int):
        row = db.query_one("SELECT name FROM feeds WHERE id = ?", (feed_id,))
        if not row:
            raise HTTPException(404)
        with db.conn() as c:
            c.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        return redirect_settings("sources", msg=f"Removed “{row['name']}”. Its past stories stay in old editions.")

    @app.post("/settings/sources/{feed_id}/test")
    async def test_source(feed_id: int):
        row = db.query_one("SELECT * FROM feeds WHERE id = ?", (feed_id,))
        if not row:
            raise HTTPException(404)
        feed = dict(row)
        feed["etag"] = feed["last_modified"] = None
        async with builder.client_factory() as client:
            result = await feedmod.fetch_one(client, feed)
        await asyncio.to_thread(feedmod.store_results, db, [feed], [result])
        if result.ok:
            return redirect_settings("sources", msg=f"“{feed['name']}” is working: {len(result.entries)} items.")
        return redirect_settings("sources", err=f"“{feed['name']}” failed: {result.error}")

    # exclusions
    @app.get("/settings/exclusions", response_class=HTMLResponse)
    def settings_exclusions(request: Request):
        latest = db.query_one("SELECT data FROM editions ORDER BY date DESC LIMIT 1")
        excluded = json.loads(latest["data"]).get("stats", {}).get("excluded") if latest else None
        return render(request, "settings_exclusions.html", tab="exclusions",
                      terms="\n".join(db.get_setting("exclusions", [])), excluded=excluded)

    @app.post("/settings/exclusions")
    def save_exclusions(terms: str = Form("")):
        cleaned = list(dict.fromkeys(t.strip()[:80] for t in terms.splitlines() if t.strip()))[:500]
        db.set_setting("exclusions", cleaned)
        return redirect_settings("exclusions", msg=f"Saved {len(cleaned)} excluded terms. "
                                                   f"They apply from the next build (use Rebuild now to apply today).")

    # learning
    @app.get("/settings/learning", response_class=HTMLResponse)
    def settings_learning(request: Request):
        terms = [dict(r) for r in db.query("SELECT term, weight FROM learned_terms ORDER BY weight DESC")]
        sources = [dict(r) for r in db.query(
            "SELECT l.feed_id, l.weight, f.name FROM learned_sources l JOIN feeds f ON f.id = l.feed_id "
            "ORDER BY l.weight DESC")]
        hidden = db.query_one("SELECT COUNT(*) AS n FROM stories WHERE interest = -1")["n"]
        taps = db.query_one("SELECT COUNT(*) AS n FROM feedback")["n"]
        return render(request, "settings_learning.html", tab="learning",
                      liked=[t for t in terms if t["weight"] > 0],
                      disliked=sorted([t for t in terms if t["weight"] < 0], key=lambda t: t["weight"]),
                      sources=sources, hidden=hidden, taps=taps, decay=learning.DECAY)

    @app.post("/settings/learning/forget-term")
    def forget_term(term: str = Form(...)):
        learning.forget_term(db, term)
        return redirect_settings("learning", msg=f"Forgot “{term}”.")

    @app.post("/settings/learning/forget-source")
    def forget_source(feed_id: int = Form(...)):
        learning.forget_source(db, feed_id)
        return redirect_settings("learning", msg="Forgot that source preference.")

    @app.post("/settings/learning/reset")
    def reset_learning(confirm: str = Form("")):
        if confirm != "yes":
            return redirect_settings("learning", err="Tick the box to confirm the reset.")
        learning.reset_all(db)
        return redirect_settings("learning", msg="Everything learned has been reset.")

    @app.post("/settings/learning/unhide")
    def unhide_all():
        with db.conn() as c:
            c.execute("UPDATE stories SET interest = 0 WHERE interest = -1 AND id NOT IN "
                      "(SELECT story_id FROM feedback)")
        return redirect_settings("learning", msg="Un-hid stories that had no remaining feedback.")

    # system / backups
    @app.get("/settings/system", response_class=HTMLResponse)
    def settings_system(request: Request):
        latest = db.query_one("SELECT * FROM editions ORDER BY date DESC LIMIT 1")
        img_count, img_bytes = articles.storage_used(config.image_dir)
        art = db.query_one("SELECT COUNT(*) AS n, SUM(status = 'ok') AS ok FROM articles")
        files = [{"name": p.name, "size": p.stat().st_size,
                  "when": datetime.fromtimestamp(p.stat().st_mtime, config.tz)} for p in backup.list_backups(config)]
        return render(request, "settings_system.html", tab="system", backups=files,
                      last_backup=db.get_setting("last_backup", None), keep=config.backup_keep,
                      latest=dict(latest) if latest else None,
                      latest_data=json.loads(latest["data"]) if latest else {},
                      build_error=db.get_setting("last_build_error", None), next_build=jobs.next_build(),
                      tz_name=config.tz_name, alert_url=config.alert_feed_url,
                      db_size=config.db_path.stat().st_size if config.db_path.exists() else 0,
                      img_count=img_count, img_bytes=img_bytes, art_ok=art["ok"] or 0, art_total=art["n"],
                      keep_days=db.get_setting("article_keep_days", 60))

    @app.post("/settings/backup")
    async def backup_now():
        path = await asyncio.to_thread(backup.run_recorded, db, config)
        if path:
            return redirect_settings("system", msg=f"Backup saved as {path.name}.")
        return redirect_settings("system", err="Backup failed; see the error below.")

    @app.get("/settings/backups/{name}")
    def download_backup(name: str):
        path = backup.backup_path(config, name)
        if not path:
            raise HTTPException(404)
        return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if is_fetch(request) or request.method != "GET":
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", status=exc.status_code, code=exc.status_code, detail=exc.detail)

    return app


def is_fetch(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def safe_next(value: str) -> bool:
    return bool(value) and value.startswith("/") and not value.startswith("//") and "\\" not in value \
        and "\n" not in value and "\r" not in value


def redirect_settings(tab: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/settings/{tab}{q}", status_code=303)


def app_factory() -> FastAPI:
    return create_app()
