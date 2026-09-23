"""Evidence strength and original-source corroboration are separate axes.

Levels are rule-based labels, not calibrated probabilities. Authoritative single
source facts can be high only with a complete, time-aligned declared scope.
"""
from __future__ import annotations

from app.core.claim_kinds import claim_kind
from app.core.source_policy import publisher_key

CONFIDENCE_POLICY_VERSION = "2026-09-23.v2"
PRIMARY = {"official", "regulatory"}
INDEPENDENT_AUTHORITIES = {"media", "regulatory"}


def empty_assessment():
    return {"status": "not_verified", "source_count": 0, "full_support_source_count": 0,
            "evidence_count": 0, "credible_independent_source_count": 0}


def assess_confidence(claim, by_id):
    claim["confidence_policy_version"] = CONFIDENCE_POLICY_VERSION
    claim.update(confidence="unverified", cross_validated=False,
                 confidence_reason="支持性或来源准入未通过", independent_verification=empty_assessment())
    verification = claim.get("verification", {})
    if verification.get("verdict") != "supported":
        claim["confidence_reason"] = verification.get("reason") or "支持性或来源准入未通过"
        return claim
    all_groups, full_groups, credible_groups, primary_groups, evidence_ids = set(), set(), set(), set(), set()
    primary_ids = set()
    for s in verification.get("supports", []):
        if s.get("relation") not in ("supports", "partial"):
            continue
        for p in by_id.get(s.get("evidence_id"), []):
            group = p.get("source_group") or publisher_key(p.get("source_url", ""))
            if not group:
                continue
            evidence_ids.add(s["evidence_id"])
            all_groups.add(group)
            body = p.get("fetch_kind") in ("body", "rendered")
            if body and p.get("source_tier") in PRIMARY:
                primary_ids.add(s["evidence_id"])
            if s["relation"] == "supports":
                full_groups.add(group)
                if body and p.get("source_tier") in PRIMARY:
                    primary_groups.add(group)
                if body and p.get("source_tier") in INDEPENDENT_AUTHORITIES:
                    credible_groups.add(group)
    if not evidence_ids:
        return claim
    if len(all_groups) == 1 and evidence_ids <= primary_ids:
        # Joint quotations from one authoritative origin may cover a complete fact,
        # but still count as ONE original source, never independent corroboration.
        full_groups.update(all_groups)
        primary_groups.update(all_groups)
    crossed = len(full_groups) >= 2
    kind = claim_kind(claim)
    if crossed:
        status = "corroborated"
    elif len(all_groups) > 1:
        status = "composite_support"
    elif primary_groups and kind == "declared_fact":
        status = "single_authority"
    elif len(evidence_ids) > 1:
        status = "same_origin"
    else:
        status = "single_source"
    claim["independent_verification"] = {
        "status": status, "source_count": len(all_groups), "full_support_source_count": len(full_groups),
        "evidence_count": len(evidence_ids), "credible_independent_source_count": len(credible_groups),
    }
    claim["cross_validated"] = crossed
    assessment = verification.get("assessment", {})
    complete = assessment.get("scope_complete") is True
    timed = assessment.get("temporal_alignment") == "consistent"
    claim["claim_kind"] = kind
    if complete and timed and kind == "declared_fact" and primary_groups:
        claim["confidence"] = "high"
        claim["confidence_reason"] = "权威一手正文直接支持完整的声明性事实，适用条件与时间口径一致；仅适用于所引用快照"
    elif complete and timed and kind in ("declared_fact", "observed_fact", "comparison") and len(credible_groups) >= 2:
        claim["confidence"] = "high"
        claim["confidence_reason"] = "至少两个可信的独立原始来源各自支持完整结论，适用条件与时间口径一致"
    elif primary_groups or credible_groups or crossed or len(all_groups) >= 2:
        claim["confidence"] = "medium"
        if not complete or not timed:
            claim["confidence_reason"] = "原文支持，但适用条件或时间信息仍不完整，暂不评为高可信"
        elif kind in ("observed_fact", "comparison"):
            claim["confidence_reason"] = "原文支持，但实测、成交或比较结论仍缺少足够的可信独立核验"
        else:
            claim["confidence_reason"] = "原文支持，但尚未满足权威直接事实或可信多源独立核验条件"
    else:
        claim["confidence"] = "low"
        claim["confidence_reason"] = "仅有普通单一来源支持，证据强度有限"
    return claim
