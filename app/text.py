"""Text helpers: cleaning feed HTML, URL normalisation, headline similarity, labels, exclusions."""
from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LABELS = ("reporting", "analysis", "opinion")
LABEL_NAMES = {"reporting": "Reporting", "analysis": "Analysis", "opinion": "Opinion"}

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having
he her here hers herself him himself his how i if in into is it its itself just me more most my myself new
no nor not now of off on once only or other our ours out over own same says say said she should so some
such than that the their theirs them themselves then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your yours
yourself after amid among says over year years day days week weeks news live update updates latest
first last one two three four five world report reports could may might get gets got make makes made
""".split())

TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|dclid|mc_cid|mc_eid|cmp|cmpid|ref|ref_src|src|"
                             r"ocid|at_medium|at_campaign|at_custom\d|sr_share|smid|taid|rss|cid|ito)$", re.I)


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "figure", "figcaption"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "figure", "figcaption") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(value: str | None) -> str:
    """Turn feed HTML into plain text. Output is plain text; templates still autoescape it."""
    if not value:
        return ""
    parser = _Stripper()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# Newsletter and app promotions that some publishers (notably the Guardian) put in feed summaries.
PROMO = re.compile(
    r"\s*\b(?:Get our [\w’'\- ,]{0,60}?email,? (?:our )?free app,? (?:or|and) (?:our )?daily news podcast"
    r"|Sign up (?:for|to) [^.?!]{0,80}?\b(?:newsletter|email|emails)\b(?: here)?"
    r"|Follow (?:our|the) [^.?!]{0,60}?\blive(?: blog)? for (?:the )?latest (?:updates|news)"
    r"|(?:Download|Get) the (?:free )?Guardian app\b)\s*[.:–—-]?\s*")


def remove_promos(text: str) -> str:
    """Strip publisher promotions. The phrases are matched case-sensitively, as publishers write them,
    so an ordinary sentence like "residents can sign up to email alerts" is left alone."""
    def repl(m: re.Match) -> str:
        before = text[:m.start()].rstrip()
        after = text[m.end():]
        if not before or not after.strip():
            return " "
        # "…Gippsland region Get our … podcast William was found" -> "…Gippsland region. William was found"
        end = before.rstrip("”\"’'")  # look past a closing quote: ‘…tumour’ still needs a full stop
        return " " if end and end[-1] in ".!?…:;" else ". "
    return re.sub(r"\s{2,}", " ", PROMO.sub(repl, text or "")).strip()


def summarise(text: str, limit: int = 260) -> str:
    text = remove_promos(text)
    text = re.sub(r"\s*(The post .* appeared first on .*|Continue reading\.*|Read more\.*|\[…\]|\[\.\.\.\])\s*$",
                  "", text, flags=re.I).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if sentence_end > limit * 0.6:
        return cut[: sentence_end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def normalise_url(url: str) -> str:
    """Canonical form for duplicate detection: no scheme, no www/m/amp, no tracking params or fragment."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "amp.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = re.sub(r"/+", "/", parts.path or "/")
    path = re.sub(r"(/amp|\.amp|/index\.html?)$", "", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not TRACKING_PARAMS.match(k)]
    query.sort()
    return urlunsplit(("", host, path, urlencode(query), "")).lstrip("/")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().replace("’", "'").replace("‘", "'")


def tokens(text: str) -> list[str]:
    """Significant lower-case words, used for headline matching and learned preferences."""
    words = re.findall(r"[a-z0-9][a-z0-9'\-]*[a-z0-9]|[a-z0-9]", _fold(text))
    out = []
    for w in words:
        w = w.replace("'s", "").strip("'-")
        if len(w) < 3 and not w.isdigit():
            continue
        if w in STOPWORDS:
            continue
        out.append(w)
    return out


def title_key(title: str) -> str:
    return " ".join(sorted(set(tokens(title))))


def strip_title_prefix(title: str) -> str:
    return re.sub(r"^\s*(opinion|comment|analysis|editorial|explainer|explained|live|watch|video|listen)\s*[:|–—-]\s*",
                  "", title, flags=re.I)


def headline_similarity(a: str, b: str) -> float:
    """0..1 similarity between two headlines (token overlap blended with character matching)."""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    containment = len(ta & tb) / min(len(ta), len(tb))
    if jaccard < 0.25 and containment < 0.5:
        return jaccard
    ratio = SequenceMatcher(None, _fold(strip_title_prefix(a)), _fold(strip_title_prefix(b))).ratio()
    score = max(jaccard, ratio, containment * 0.9 if min(len(ta), len(tb)) >= 4 else 0)
    return score


def same_story(a: str, b: str, threshold: float = 0.72) -> bool:
    return headline_similarity(a, b) >= threshold


# --- labels -------------------------------------------------------------
OPINION_URL = re.compile(r"/(opinion|opinions|comment|commentisfree|op-?ed|oped|editorials?|columns?|columnists?|"
                         r"perspectives?|letters|blogs?/opinion|the-conversation)(/|$|-)", re.I)
ANALYSIS_URL = re.compile(r"/(analysis|news-analysis|explainers?|explained|in-depth|indepth|long-reads?|"
                          r"the-big-picture|features?/analysis)(/|$|-)", re.I)
OPINION_WORDS = {"opinion", "opinions", "comment", "commentisfree", "editorial", "editorials", "op-ed", "oped",
                 "column", "columns", "columnist", "columnists", "letters", "perspective", "review", "reviews"}
ANALYSIS_WORDS = {"analysis", "news analysis", "explainer", "explainers", "explained", "in depth", "in-depth",
                  "long read", "long reads", "fact check", "factcheck", "deep dive"}
OPINION_PREFIX = re.compile(r"^\s*(opinion|comment|editorial|op-?ed|column|view|letters?|review)\s*[:|–—-]", re.I)
ANALYSIS_PREFIX = re.compile(r"^\s*(analysis|explainer|explained|fact ?check|in depth|the big picture)\s*[:|–—-]",
                             re.I)


def detect_label(title: str, url: str, categories: list[str]) -> str:
    """Best guess at Reporting / Analysis / Opinion from what the feed tells us."""
    cats = {_fold(c).strip() for c in categories if c}
    path = urlsplit(url).path if url else ""
    if OPINION_PREFIX.match(title) or OPINION_URL.search(path) or cats & OPINION_WORDS:
        return "opinion"
    if ANALYSIS_PREFIX.match(title) or ANALYSIS_URL.search(path) or cats & ANALYSIS_WORDS:
        return "analysis"
    return "reporting"


# --- exclusions ---------------------------------------------------------
def compile_exclusions(terms: list[str]) -> re.Pattern | None:
    cleaned = sorted({t.strip() for t in terms if t and t.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    parts = [r"(?<![\w])" + re.escape(_fold(t)) + r"(?![\w])" for t in cleaned]
    return re.compile("|".join(parts), re.I)


def excluded_by(pattern: re.Pattern | None, *texts: str) -> str | None:
    """Return the matching exclusion term, or None."""
    if pattern is None:
        return None
    for text in texts:
        if not text:
            continue
        m = pattern.search(_fold(text))
        if m:
            return m.group(0)
    return None


def keyword_hits(keywords: list[str], title: str, summary: str) -> tuple[int, int]:
    """(title hits, summary hits) of topic keywords, whole-word and case-insensitive."""
    ft, fs = _fold(title), _fold(summary)
    th = sh = 0
    for kw in keywords:
        k = _fold(kw.strip())
        if not k:
            continue
        pat = r"(?<![\w])" + re.escape(k) + r"(?![\w])"
        if re.search(pat, ft):
            th += 1
        elif re.search(pat, fs):
            sh += 1
    return th, sh


def split_list(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", value or "") if p.strip()]
