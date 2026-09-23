"""Bounded, query-aware passage selection without embeddings or a vector database.

Offsets refer to extracted plain text, not the original HTML. Token budgeting uses
UTF-8 bytes as a conservative upper bound for byte-based tokenizers; prompt framing
must have a separate allowance. Scores are relevance heuristics, not truth scores.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, List


from app.core.research_contract import DIMENSIONS as SPECS, field_for

DIMENSIONS = {key: spec["terms"] for key, spec in SPECS.items()}


def terms(text: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", text.lower()))
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(word[i:i + 2] for i in range(max(1, len(word) - 1)))
    return result


def expanded_terms(text: str) -> set[str]:
    result = terms(text)
    field = field_for(text)
    if field:
        for word in DIMENSIONS[field]:
            result.update(terms(word))
    return result


def value(evidence: Any, name: str, default=""):
    return evidence.get(name, default) if isinstance(evidence, dict) else getattr(evidence, name, default)


def evidence_text(evidence: Any) -> str:
    return (value(evidence, "full_text") or value(evidence, "_full_text")
            or value(evidence, "excerpt") or "")[:48000]


@dataclass(frozen=True)
class EvidenceContext:
    passages: List[dict]
    digest: str

    @property
    def evidence_ids(self) -> set[str]:
        return {p["evidence_id"] for p in self.passages}


def select_evidence(evidences, *, query: str = "", brands=None, focus=None,
                    limit: int = 28, max_chars: int = 18000,
                    max_tokens: int = 24000, prefer_ids=None) -> EvidenceContext:
    """Fair selection across brand/dimension groups, then relevance fill.

    Every candidate (including newly appended evidence) is rescored on each call.
    At most two overlapping-window passages per document enter the context.
    """
    brands = list(dict.fromkeys(brands or []))
    focus = list(dict.fromkeys(focus or []))
    prefer_ids = set(prefer_ids or [])
    if limit <= 0 or max_chars <= 0 or max_tokens <= 0:
        return EvidenceContext([], "")
    candidates = []
    df = Counter()
    seen = set()
    strict_primary = bool(focus) and all(field_for(f) in ("pricing_model", "feature_tree", "ecosystem", "architecture") for f in focus)
    primary_brands = {value(e, "brand") for e in evidences if value(e, "source_tier") in ("official", "regulatory")
                      and value(e, "fetch_kind") in ("body", "rendered")}
    for ev in evidences:
        if strict_primary and value(ev, "brand") in primary_brands and not (
                value(ev, "source_tier") in ("official", "regulatory") and value(ev, "fetch_kind") in ("body", "rendered")):
            continue  # Secondary leads must not crowd out admissible first-party facts.
        eid = value(ev, "evidence_id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        text = evidence_text(ev)
        for start in range(0, len(text), 700):
            chunk = text[start:start + 900]
            if not chunk.strip():
                continue
            tokens = terms(chunk)
            df.update(tokens)
            candidates.append({"evidence_id": eid, "brand": value(ev, "brand"),
                               "source_tier": value(ev, "source_tier", "unclassified"),
                               "source_group": value(ev, "source_group"),
                               "fetch_kind": value(ev, "fetch_kind", "snippet"),
                               "published_at": value(ev, "published_at"),
                               "captured_at": value(ev, "captured_at"),
                               "title": value(ev, "title")[:200],
                               "source_url": value(ev, "source_url")[:500],
                               "start": start, "end": start + len(chunk),
                               "text": chunk, "tokens": tokens,
                               "credibility": value(ev, "credibility", 0)})
    if not candidates:
        return EvidenceContext([], "")
    query_terms = terms(query)
    dimension_terms = [expanded_terms(dim) for dim in focus]
    all_terms = query_terms | set().union(*dimension_terms)
    n = len(candidates)
    for p in candidates:
        hits = p["tokens"] & all_terms
        p["score"] = sum(math.log(1 + n / (1 + df[t])) for t in hits)
        # Dimension matches must outrank generic brand repetitions.
        p["score"] += 3 * sum(bool(p["tokens"] & ts) for ts in dimension_terms)
        p["score"] += 0.15 * len(terms(p["title"]) & all_terms)
        p["score"] += min(100, max(0, float(p["credibility"] or 0))) / 1000
        if p["source_tier"] in ("official", "regulatory") and hits:
            p["score"] += 5
        if any(field_for(f) == "pricing_model" for f in focus) and re.search(r"[￥¥]|\d+(?:\.\d+)?\s*(?:万元|元起)", p["text"]):
            p["score"] += 4
    ranked = sorted(candidates, key=lambda p: (-p["score"], p["evidence_id"], p["start"]))
    selected, lines = [], []
    doc_counts = Counter()
    used, chars, tokens = set(), 0, 0

    def add(p):
        nonlocal chars, tokens
        key = (p["evidence_id"], p["start"])
        if key in used or doc_counts[p["evidence_id"]] >= 2 or len(selected) >= limit:
            return False
        # Do not duplicate overlapping excerpts of the same source.
        if any(s["evidence_id"] == p["evidence_id"] and
               max(s["start"], p["start"]) < min(s["end"], p["end"]) for s in selected):
            return False
        clean = {k: p[k] for k in ("evidence_id", "brand", "title", "source_url", "start", "end", "text",
                                    "source_tier", "source_group", "fetch_kind", "published_at", "captured_at")}
        line = json.dumps(clean, ensure_ascii=False)
        if chars + len(line) + 1 > max_chars or tokens + len(line.encode("utf-8")) + 1 > max_tokens:
            return False
        selected.append(clean)
        lines.append(line)
        used.add(key)
        doc_counts[p["evidence_id"]] += 1
        chars += len(line) + 1
        tokens += len(line.encode("utf-8")) + 1
        return True

    # Reserve one relevant recent passage per cell before older keyword-rich pages.
    for brand in (brands or [""]):
        for dim_terms in (dimension_terms or [query_terms]):
            for p in ranked:
                if p["evidence_id"] not in prefer_ids or (brand and p["brand"] != brand):
                    continue
                if dim_terms and not p["tokens"] & dim_terms:
                    continue
                if add(p):
                    break
    # Dimension-first round robin ensures later brands compete before repeats.
    for dim_terms in (dimension_terms or [query_terms]):
        for brand in (brands or [""]):
            for p in ranked:
                if brand and p["brand"] != brand and brand.lower() not in (p["title"] + p["text"]).lower():
                    continue
                if dim_terms and not p["tokens"] & dim_terms:
                    continue
                if add(p):
                    break
    for p in ranked:
        if all_terms and not (p["tokens"] | terms(p["title"])) & all_terms:
            continue
        add(p)
    return EvidenceContext(selected, "\n".join(lines))
