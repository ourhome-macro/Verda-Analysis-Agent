"""Build report visuals only from verified claims with persisted source records.

Legacy exploratory scores and sentiment percentages have no claim-level proof.
They are deliberately replaced by counts and source-linked facts, rather than
being reintroduced merely because a chart carries an evidence ID.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

from app.core.claim_verifier import supported_claims


VERSION = 1


def _valid_source_url(record: dict[str, Any]) -> bool:
    try:
        parts = urlsplit(str(record.get("source_url") or ""))
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def apply_audited_artifacts(report: dict[str, Any]) -> dict[str, Any]:
    """Populate deterministic visuals for new and previously audited reports."""
    evidence = {e.get("evidence_id"): e for e in report.get("evidence", [])
                if isinstance(e, dict) and e.get("evidence_id")}
    facts = []
    for claim in supported_claims(report.get("claims", [])):
        if not claim.get("claim_id"):
            continue
        ids = claim.get("evidence_ids")
        if not (isinstance(ids, list) and ids and all(isinstance(eid, str) and eid in evidence for eid in ids)):
            continue
        if not all(_valid_source_url(evidence[eid]) for eid in ids):
            continue
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        first = evidence[ids[0]]
        brand = claim.get("brand") or first.get("brand") or "未标明品牌"
        facts.append({
            "claim_id": claim["claim_id"], "brand": brand,
            "dimension": claim.get("dimension") or claim.get("field") or "其他",
            "field": claim.get("field") or "", "text": text,
            "evidence_ids": list(dict.fromkeys(ids)),
            "source_url": first.get("source_url") or "",
        })

    report["structured"] = {"verified_facts": facts} if facts else {}
    for section in report.get("sections", []):
        if section.get("id") in {"summary", "overview", "conclusion", "risk", "trace_note"}:
            section["structured"] = None
            continue
        claim_ids = {c.get("claim_id") for c in section.get("claims", []) if isinstance(c, dict)}
        rows = [fact for fact in facts if fact["claim_id"] in claim_ids]
        section["structured"] = {"type": "verified_facts", "data": rows} if rows else None

    chart = _coverage_chart(report, facts)
    report["charts"] = [chart] if chart else []
    chart_section = _chart_section(report)
    for section in report.get("sections", []):
        section["charts"] = [chart] if chart and section is chart_section else []

    voices = []
    for fact in facts:
        if fact["field"] != "sentiment" or not fact["source_url"]:
            continue
        source = evidence[fact["evidence_ids"][0]]
        if source.get("source_tier") != "community":
            continue
        voices.append({"claim_id": fact["claim_id"], "text": fact["text"],
                       "url": fact["source_url"], "evidence_ids": fact["evidence_ids"]})
    # A verified community claim is one attributed observation, not a vote in
    # a representative sample. No aggregate sentiment percentages are emitted.
    report["sentiment"] = {"verified_voices": voices} if voices else {}
    report["audited_artifact_version"] = VERSION
    return report


def _chart_section(report: dict[str, Any]) -> dict[str, Any] | None:
    sections = report.get("sections", [])
    return next((s for s in sections if s.get("id") == "overview"),
                next((s for s in sections if s.get("id") == "summary"),
                     sections[0] if sections else None))


def _coverage_chart(report: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not facts or not report.get("sections"):
        return None
    brands = list(dict.fromkeys([*report.get("brands", []), *(f["brand"] for f in facts)]))
    dimensions = list(dict.fromkeys(f["dimension"] for f in facts))
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for fact in facts:
        counts[(fact["brand"], fact["dimension"])] += 1
    ids = list(dict.fromkeys(eid for fact in facts for eid in fact["evidence_ids"]))
    return {
        "chart_id": "ch_verified_coverage", "type": "verified_coverage",
        "title": "已核验论点覆盖（按品牌与维度）",
        "evidence_ids": ids,
        "claim_ids": [f["claim_id"] for f in facts],
        "option": {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"bottom": 0},
            "grid": {"left": 48, "right": 24, "top": 24, "bottom": 48, "containLabel": True},
            "xAxis": {"type": "category", "data": brands},
            "yAxis": {"type": "value", "name": "已核验论点数", "minInterval": 1},
            "series": [{"name": dim, "type": "bar", "stack": "verified",
                        "data": [counts[(brand, dim)] for brand in brands]}
                       for dim in dimensions],
        },
    }
