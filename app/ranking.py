"""Choosing the stories for an edition: filter, de-duplicate, score, select."""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from . import text
from .db import parse_iso
from .learning import story_preference

LEAD_COUNT = 3
OPINION_CAP = 2
SAME_STORY = 0.72


@dataclass
class Candidate:
    row: dict[str, Any]
    tokens: set[str]
    folded: str
    published: datetime
    label: str

    @property
    def id(self) -> int:
        return self.row["id"]


@dataclass
class Cluster:
    members: list[Candidate]
    score: float = 0.0
    topic_id: int | None = None

    @property
    def primary(self) -> Candidate:
        return self.members[0]

    @property
    def sources(self) -> set[str]:
        return {m.row["source_name"] for m in self.members}

    def also(self) -> list[int]:
        """Other outlets' versions (one per outlet, excluding the primary's outlet)."""
        seen = {self.primary.row["source_name"]}
        out = []
        for m in self.members[1:]:
            if m.row["source_name"] not in seen:
                seen.add(m.row["source_name"])
                out.append(m.id)
        return out


@dataclass
class Selection:
    items: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def effective_label(row: dict[str, Any]) -> str:
    if row.get("label_override") in text.LABELS:
        return row["label_override"]
    if row.get("treat_as") in text.LABELS:
        return row["treat_as"]
    return row.get("label_auto") or "reporting"


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", text.strip_title_prefix(s).lower()).strip()


def _similar(a_tokens: set[str], a_folded: str, b_tokens: set[str], b_folded: str) -> float:
    if not a_tokens or not b_tokens:
        return 0.0
    inter = len(a_tokens & b_tokens)
    if inter == 0:
        return 0.0
    jaccard = inter / len(a_tokens | b_tokens)
    smaller = min(len(a_tokens), len(b_tokens))
    containment = inter / smaller
    if jaccard < 0.25 and containment < 0.5:
        return jaccard
    best = max(jaccard, containment * 0.9 if smaller >= 4 else 0.0)
    if best >= SAME_STORY:
        return best
    return max(best, SequenceMatcher(None, a_folded, b_folded).ratio())


def make_candidate(row: dict[str, Any], fallback_time: datetime) -> Candidate:
    published = parse_iso(row.get("published_at")) or parse_iso(row.get("fetched_at")) or fallback_time
    return Candidate(row=row, tokens=set(text.tokens(text.strip_title_prefix(row["title"]))),
                     folded=_fold(row["title"]), published=published, label=effective_label(row))


def cluster(cands: list[Candidate]) -> list[Cluster]:
    # Highest-priority, most informative version first so it becomes the primary.
    cands = sorted(cands, key=lambda c: (-(c.row.get("priority") or 3), not c.row.get("summary"), c.published))
    clusters: list[Cluster] = []
    index: dict[str, list[int]] = {}
    for cand in cands:
        possible = {ci for t in cand.tokens for ci in index.get(t, [])}
        match = None
        for ci in sorted(possible):
            p = clusters[ci].primary
            if _similar(cand.tokens, cand.folded, p.tokens, p.folded) >= SAME_STORY:
                match = ci
                break
        if match is None:
            clusters.append(Cluster(members=[cand]))
            for t in cand.tokens:
                index.setdefault(t, []).append(len(clusters) - 1)
        else:
            clusters[match].members.append(cand)
    return clusters


def seen_before(cl: Cluster, history: list[tuple[set[str], str]], hindex: dict[str, list[int]]) -> bool:
    p = cl.primary
    for hi in {i for t in p.tokens for i in hindex.get(t, [])}:
        ht, hf = history[hi]
        if _similar(p.tokens, p.folded, ht, hf) >= SAME_STORY:
            return True
    return False


def score_cluster(cl: Cluster, topic: dict[str, Any], keywords: list[str], weights, now: datetime) -> float:
    p = cl.primary
    th, sh = text.keyword_hits(keywords, p.row["title"], p.row.get("summary") or "")
    keyword_score = min(6.0, th * 2.0 + sh * 1.0)
    priority = max(m.row.get("priority") or 3 for m in cl.members)
    terms, sources = weights
    preference = story_preference(terms, sources, p.row["title"], p.row.get("feed_id"))
    age_hours = max(0.0, (now - max(m.published for m in cl.members)).total_seconds() / 3600)
    freshness = 4.0 * 0.5 ** (age_hours / 12.0)
    coverage = 1.5 * min(3, len(cl.sources) - 1)
    return topic["importance"] * 2.0 + keyword_score + priority * 0.6 + preference + freshness + coverage


def select(rows: list[dict[str, Any]], topics: list[dict[str, Any]], exclusions: list[str],
           weights, shown_ids: set[int], history_titles: list[str], now: datetime, seed: str) -> Selection:
    """rows: candidate stories joined with their feed (priority, treat_as, topic_id)."""
    stats = {"candidates": len(rows), "excluded": 0, "hidden": 0, "repeats": 0, "merged": 0}
    topic_by_id = {t["id"]: t for t in topics if t["enabled"]}
    pattern = text.compile_exclusions(exclusions)

    cands = []
    for row in rows:
        if row["id"] in shown_ids:
            stats["repeats"] += 1
            continue
        if row.get("interest") == -1:
            stats["hidden"] += 1
            continue
        cats = " ".join(json.loads(row.get("categories") or "[]"))
        if text.excluded_by(pattern, row["title"], row.get("summary") or "", cats):
            stats["excluded"] += 1
            continue
        cands.append(make_candidate(row, now))

    history = []
    hindex: dict[str, list[int]] = {}
    for title in history_titles:
        toks = set(text.tokens(text.strip_title_prefix(title)))
        history.append((toks, _fold(title)))
        for t in toks:
            hindex.setdefault(t, []).append(len(history) - 1)

    clusters = []
    for cl in cluster(cands):
        stats["merged"] += len(cl.members) - 1
        if seen_before(cl, history, hindex):
            stats["repeats"] += 1
            continue
        cl.topic_id = cl.primary.row.get("topic_id")
        topic = topic_by_id.get(cl.topic_id)
        if topic is None:
            continue
        cl.score = score_cluster(cl, topic, text.split_list(topic["keywords"]), weights, now)
        clusters.append(cl)
    clusters.sort(key=lambda c: -c.score)

    items: list[dict[str, Any]] = []
    used: set[int] = set()

    def take(cl: Cluster, section: str, position: int, wildcard: bool = False) -> None:
        used.add(id(cl))
        items.append({"story_id": cl.primary.id, "section": section, "topic_id": cl.topic_id,
                      "position": position, "wildcard": wildcard, "also": cl.also(), "score": round(cl.score, 2)})

    leads = [c for c in clusters if c.primary.label != "opinion"][:LEAD_COUNT]
    for i, cl in enumerate(leads):
        take(cl, "lead", i)

    rng = random.Random(seed)
    for topic in sorted(topic_by_id.values(), key=lambda t: (t["position"], t["id"])):
        pool = [c for c in clusters if c.topic_id == topic["id"] and id(c) not in used]
        picked, opinions = 0, 0
        for cl in pool:
            if picked >= topic["story_count"]:
                break
            if cl.primary.label == "opinion":
                if opinions >= OPINION_CAP:
                    continue
                opinions += 1
            take(cl, str(topic["id"]), picked)
            picked += 1
        rest = [c for c in pool if id(c) not in used and
                story_preference(*weights, c.primary.row["title"], c.primary.row.get("feed_id")) >= 0 and
                (c.primary.label != "opinion" or opinions < OPINION_CAP)]
        if rest:
            take(rng.choice(rest), str(topic["id"]), picked, wildcard=True)

    stats["selected"] = len(items)
    return Selection(items=items, stats=stats)
