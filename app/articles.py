"""Full articles for the reader view: fetched at build time, text extracted, photos copied locally.

Everything a story page needs is stored on the server (text in SQLite, photos under /data/images),
so the paper stays readable on the LAN even when the internet connection is down.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from . import text
from .db import Database, iso, utcnow
from .feeds import USER_AGENT

log = logging.getLogger("paper.articles")

MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGES = 8
MIN_IMAGE_SIDE = 160            # skip icons, logos and tracking pixels
IMAGE_MAX_SIDE = 1600           # photos are scaled down to this for tablets
JPEG_QUALITY = 82
MIN_WORDS = 40                  # less than this and we probably hit a paywall, video or live page
CONCURRENCY = 6
IMAGE_NAME = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{40}\.jpg$")

ClientFactory = Callable[[], httpx.AsyncClient]


# --- network safety -----------------------------------------------------------
class BlockedAddress(httpx.RequestError):
    pass


def _is_public(host: str, infos: list[tuple]) -> bool:
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


async def block_private_hosts(request: httpx.Request) -> None:
    """Article links come from feeds, so never let them reach machines on your own network."""
    host = request.url.host
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise BlockedAddress(f"Refused to fetch from a private address ({host})", request=request)
        return
    except ValueError:
        pass
    if host.lower() in ("localhost",) or host.lower().endswith((".local", ".lan", ".internal", ".home.arpa")):
        raise BlockedAddress(f"Refused to fetch from a local host name ({host})", request=request)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, request.url.port or 443,
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror:
        return  # can't resolve: the request itself will fail (or go via a configured proxy)
    if not _is_public(host, infos):
        raise BlockedAddress(f"Refused to fetch from a private address ({host})", request=request)


def default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=True, max_redirects=5,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                 "Accept-Language": "en-AU,en;q=0.8"},
        event_hooks={"request": [block_private_hosts]})


# --- sanitising -------------------------------------------------------------------
ALLOWED = {"p", "h2", "h3", "h4", "blockquote", "ul", "ol", "li", "em", "strong", "br", "figure",
           "figcaption", "img", "pre", "code", "hr"}
RENAME = {"b": "strong", "i": "em", "h1": "h2", "h5": "h4", "h6": "h4", "q": "em"}
VOID = {"br", "img", "hr"}
DROP_CONTENT = {"script", "style", "noscript", "iframe", "object", "embed", "svg", "math", "form",
                "button", "select", "textarea", "template", "video", "audio", "head", "title"}
BLOCK = {"p", "h2", "h3", "h4", "blockquote", "ul", "ol", "li", "figure", "pre", "hr"}


class _Sanitiser(HTMLParser):
    """Rebuilds HTML from an allow-list: no attributes survive except an image's alt text,
    and image sources are swapped for placeholders that are later pointed at local copies."""

    def __init__(self, base_url: str, skip_heading: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_heading = _norm(skip_heading)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.images: list[str] = []
        self._drop = 0
        self._heading_buf: list[str] | None = None
        self._heading_start = 0

    def handle_starttag(self, tag, attrs):
        if self._drop or tag in DROP_CONTENT:
            if tag in DROP_CONTENT:
                self._drop += 1
            return
        if tag == "h1" and self._heading_buf is None:
            self._heading_buf = []
            self._heading_start = len(self.out)
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED:
            return
        if tag == "img":
            a = dict(attrs)
            src = a.get("src") or a.get("data-src") or ""
            src = urljoin(self.base_url, src.strip())
            if not src.startswith(("http://", "https://")):
                return
            if len(self.images) >= MAX_IMAGES and src not in self.images:
                return
            if src not in self.images:
                self.images.append(src)
            idx = self.images.index(src)
            alt = html.escape(a.get("alt") or "", quote=True)[:300]
            self.out.append(f'<img data-i="{idx}" alt="{alt}">')
            return
        if tag in VOID:
            self.out.append(f"<{tag}>")
            return
        self.stack.append(tag)
        self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            if self._drop:
                self._drop -= 1
            return
        if self._drop:
            return
        orig = tag
        tag = RENAME.get(tag, tag)
        if tag in ALLOWED and tag not in VOID and tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break
        if orig == "h1" and self._heading_buf is not None:
            # The page's own headline duplicates the title we already show: drop it.
            if self.skip_heading and _norm("".join(self._heading_buf)) == self.skip_heading:
                del self.out[self._heading_start:]
            self._heading_buf = None

    def handle_data(self, data):
        if self._drop:
            return
        if self._heading_buf is not None:
            self._heading_buf.append(data)
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        body = "".join(self.out)
        body = re.sub(r"<(p|h2|h3|h4|li|blockquote|figcaption|strong|em)>\s*</\1>", "", body)
        return body.strip()


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s or "").strip().lower()


def sanitise(fragment: str, base_url: str, title: str = "") -> tuple[str, list[str]]:
    s = _Sanitiser(base_url, title)
    s.feed(fragment)
    s.close()
    return s.result(), s.images


# Page furniture that sometimes survives extraction (e.g. the BBC's "Published 12 hours ago").
JUNK_BLOCK = re.compile(
    r"<(li|p|h4)>\s*(?:Published|Updated|Last updated)(?:\s*(?:on\s+)?[\w ,:]{0,30}?(?:ago|20\d\d))?\s*</\1>"
    r"|<(li|p)>\s*(?:Share|Save|Share this|Share this article|Advertisement|Skip advertisement|Image source,[^<]{0,80}"
    r"|Getty Images|Watch|Listen|Read more|Related topics?|More on this story)\s*</\2>", re.I)


def clean_body(body: str) -> str:
    """Remove left-over page furniture and promotions. Safe to run more than once."""
    body = JUNK_BLOCK.sub("", body)
    body = re.sub(r"<(p|li)>([^<]*)</\1>",
                  lambda m: f"<{m.group(1)}>{text.remove_promos(m.group(2))}</{m.group(1)}>"
                  if text.PROMO.search(m.group(2)) else m.group(0), body)
    body = re.sub(r"(<(?:figcaption|p)>)\s*Image caption,\s*", r"\1", body)
    for _ in range(2):
        body = re.sub(r"<(p|li|h2|h3|h4|blockquote|figcaption|strong|em)>\s*</\1>", "", body)
        body = re.sub(r"<(ul|ol)>\s*</\1>", "", body)
    return body.strip()


def word_count(body: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", body).split())


# --- extraction -----------------------------------------------------------------
@dataclass
class Extracted:
    body: str = ""
    images: list[str] = field(default_factory=list)
    byline: str | None = None
    lead_image: str | None = None
    words: int = 0


def extract(page: str, url: str, title: str) -> Extracted:
    import trafilatura  # imported lazily: it is large and only needed during builds

    fragment = trafilatura.extract(page, url=url, output_format="html", include_images=True,
                                   include_links=False, include_formatting=True, include_tables=False,
                                   include_comments=False, favor_recall=True) or ""
    body, images = sanitise(fragment, url, title)
    meta = trafilatura.extract_metadata(page, default_url=url)
    lead = None
    if not images and meta is not None and meta.image:
        candidate = urljoin(url, meta.image)
        if candidate.startswith(("http://", "https://")):
            lead = candidate
    byline = (meta.author if meta is not None else None) or None
    if byline:
        byline = re.sub(r"\s*;\s*", ", ", byline)[:200]
    return Extracted(body=body, images=images, byline=byline, lead_image=lead, words=word_count(body))


# --- images ---------------------------------------------------------------------
def image_name(src: str) -> str:
    digest = hashlib.sha1(src.encode()).hexdigest()
    return f"{digest[:2]}/{digest}.jpg"


def process_image(data: bytes) -> bytes | None:
    """Re-encode as a plain JPEG no bigger than tablets need. Returns None for icons and non-images."""
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 60_000_000
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if min(im.size) < MIN_IMAGE_SIDE:
                return None
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                background = Image.new("RGB", im.size, (255, 255, 255))
                background.paste(im, mask=im.getchannel("A"))
                im = background
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            return out.getvalue()
    except Exception:
        return None


async def fetch_image(client: httpx.AsyncClient, src: str, image_dir: Path) -> str | None:
    name = image_name(src)
    path = image_dir / name
    if path.exists():
        return name
    try:
        async with client.stream("GET", src, headers={"Accept": "image/avif,image/webp,image/*;q=0.8"}) as resp:
            if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
                return None
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    return None
                chunks.append(chunk)
    except httpx.HTTPError:
        return None
    jpeg = await asyncio.to_thread(process_image, b"".join(chunks))
    if jpeg is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(jpeg)
    tmp.replace(path)
    return name


# --- fetching an article ---------------------------------------------------------------
@dataclass
class ArticleResult:
    story_id: int
    ok: bool
    body: str = ""
    byline: str | None = None
    lead_image: str | None = None
    images: list[str] = field(default_factory=list)
    words: int = 0
    error: str | None = None


async def fetch_article(client: httpx.AsyncClient, story: dict[str, Any], image_dir: Path) -> ArticleResult:
    sid = story["id"]
    try:
        resp = await client.get(story["url"])
    except BlockedAddress as exc:
        return ArticleResult(sid, False, error=str(exc))
    except httpx.TimeoutException:
        return ArticleResult(sid, False, error="Timed out")
    except httpx.HTTPError as exc:
        return ArticleResult(sid, False, error=f"Couldn't connect ({type(exc).__name__})")
    if resp.status_code in (401, 402, 403, 451):
        return ArticleResult(sid, False, error=f"The site refused the request (HTTP {resp.status_code}); "
                                               f"it may block automated readers or need a subscription")
    if resp.status_code >= 400:
        return ArticleResult(sid, False, error=f"HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "")
    if ctype and "html" not in ctype:
        return ArticleResult(sid, False, error=f"Not a web page ({ctype.split(';')[0]})")
    page = resp.content[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    try:
        ex = await asyncio.to_thread(extract, page, str(resp.url), story["title"])
    except Exception as exc:
        log.warning("Extraction failed for %s: %s", story["url"], exc)
        return ArticleResult(sid, False, error="Couldn't extract the article text")
    if ex.words < MIN_WORDS:
        return ArticleResult(sid, False, error="Couldn't find the article text (it may be paywalled, "
                                               "a video or a live page)")

    sources = ([ex.lead_image] if ex.lead_image else []) + ex.images
    names = await asyncio.gather(*(fetch_image(client, s, image_dir) for s in sources))
    local = dict(zip(sources, names))
    lead = local.get(ex.lead_image) if ex.lead_image else None

    def swap(m: re.Match) -> str:
        name = local.get(ex.images[int(m.group(1))])
        return f'<img src="/img/{name}" alt="{m.group(2)}" loading="lazy">' if name else ""

    body = re.sub(r'<img data-i="(\d+)" alt="([^"]*)">', swap, ex.body)
    body = re.sub(r"<figure>\s*(<figcaption>.*?</figcaption>)?\s*</figure>", "", body, flags=re.S)
    body = clean_body(body)
    return ArticleResult(sid, True, body=body, byline=ex.byline, lead_image=lead,
                         images=[n for n in names if n], words=ex.words)


def save(db: Database, result: ArticleResult) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO articles(story_id, status, body, byline, lead_image, images, words, error, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(story_id) DO UPDATE SET status=excluded.status, "
            "body=excluded.body, byline=excluded.byline, lead_image=excluded.lead_image, images=excluded.images, "
            "words=excluded.words, error=excluded.error, fetched_at=excluded.fetched_at",
            (result.story_id, "ok" if result.ok else "failed", result.body, result.byline, result.lead_image,
             json.dumps(result.images), result.words, result.error, iso(utcnow())))


async def fetch_many(db: Database, stories: list[dict[str, Any]], image_dir: Path,
                     client_factory: ClientFactory = default_client, overall_timeout: float = 300.0) -> dict[str, int]:
    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"ok": 0, "failed": 0, "images": 0}

    async with client_factory() as client:
        async def one(story):
            async with sem:
                try:
                    result = await fetch_article(client, story, image_dir)
                except Exception as exc:  # one bad page must never break the build
                    log.exception("Article fetch crashed for %s", story.get("url"))
                    result = ArticleResult(story["id"], False, error=f"Unexpected error ({type(exc).__name__})")
                await asyncio.to_thread(save, db, result)
                stats["ok" if result.ok else "failed"] += 1
                stats["images"] += len(result.images) + (1 if result.lead_image else 0)

        tasks = [asyncio.create_task(one(s)) for s in stories]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=overall_timeout)
            for t in pending:
                t.cancel()
            stats["failed"] += len(pending)
    return stats


def stories_needing_fetch(db: Database, story_ids: list[int]) -> list[dict[str, Any]]:
    if not story_ids:
        return []
    marks = ",".join("?" * len(story_ids))
    return [dict(r) for r in db.query(
        f"SELECT s.id, s.url, s.title FROM stories s LEFT JOIN articles a ON a.story_id = s.id "
        f"WHERE s.id IN ({marks}) AND (a.status IS NULL OR a.status != 'ok')", tuple(story_ids))]


def cleanup(db: Database, image_dir: Path, keep_days: int) -> dict[str, int]:
    """Forget stored articles older than keep_days and delete photos nothing refers to any more."""
    cutoff = iso(utcnow() - timedelta(days=keep_days))
    with db.conn() as c:
        removed = c.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,)).rowcount
    referenced: set[str] = set()
    for r in db.query("SELECT lead_image, images FROM articles WHERE status = 'ok'"):
        if r["lead_image"]:
            referenced.add(r["lead_image"])
        referenced.update(json.loads(r["images"] or "[]"))
    deleted = 0
    if image_dir.exists():
        for path in image_dir.glob("*/*"):
            name = f"{path.parent.name}/{path.name}"
            if path.suffix == ".part" or name not in referenced:
                path.unlink(missing_ok=True)
                deleted += 1
    return {"articles_removed": removed, "images_deleted": deleted}


def storage_used(image_dir: Path) -> tuple[int, int]:
    count = size = 0
    if image_dir.exists():
        for path in image_dir.glob("*/*.jpg"):
            count += 1
            size += path.stat().st_size
    return count, size
