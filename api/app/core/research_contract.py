"""One versioned research contract shared by retrieval, audit and publication.

Coverage is measured over supported brand/dimension cells, never URL counts.
Publication dates are source metadata, not collection times or proof of an event.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

VERSION = "matrix-v1"
DIMENSIONS = {
    "feature_tree": {"label": "功能对比", "aliases": ("功能对比", "产品", "功能", "产品功能"), "terms": ("功能", "特性", "配置", "feature", "features", "support"), "query": "功能 使用说明 官方文档", "source": "official"},
    "pricing_model": {"label": "定价策略", "aliases": ("定价策略", "定价", "价格"), "terms": ("定价", "价格", "收费", "套餐", "pricing", "billing", "年付", "月付"), "query": "定价 价格 收费 条款", "source": "official"},
    "sentiment": {"label": "用户口碑", "aliases": ("用户口碑", "口碑", "舆情", "舆情趋势", "评价"), "terms": ("口碑", "评价", "体验", "吐槽", "review", "reviews", "experience"), "query": "用户 体验 评价 社区 讨论", "source": "community"},
    "ecosystem": {"label": "生态壁垒", "aliases": ("生态壁垒", "生态", "集成"), "terms": ("生态", "插件", "集成", "开放平台", "API", "plugin", "integration", "developer"), "query": "插件 集成 API 开发者文档 开放平台", "source": "official"},
    "architecture": {"label": "技术架构", "aliases": ("技术架构", "架构"), "terms": ("架构", "存储", "同步", "加密", "本地", "云端", "数据", "storage", "sync", "security", "privacy", "encryption"), "query": "技术架构 数据存储 同步 安全 隐私 官方文档", "source": "official"},
    "user_persona": {"label": "用户画像", "aliases": ("用户画像", "画像", "人群", "场景"), "terms": ("画像", "人群", "场景", "persona", "users", "teams"), "query": "适用用户 场景 使用案例", "source": "mixed"},
    "market_share": {"label": "市场份额", "aliases": ("市场份额", "份额"), "terms": ("市场份额", "占有率", "销量", "market share"), "query": "市场份额 统计 报告 数据口径", "source": "mixed"},
    "trend": {"label": "增长趋势", "aliases": ("增长趋势", "趋势", "发展", "增长"), "terms": ("趋势", "增长", "更新", "trend", "growth", "release"), "query": "更新日志 增长 最新动态", "source": "mixed"},
    "swot": {"label": "SWOT", "aliases": ("swot", "优势", "劣势"), "terms": ("swot", "优势", "劣势", "风险", "strength", "risk"), "query": "能力 限制 风险 评测", "source": "mixed"},
    "business_model": {"label": "商业模式", "aliases": ("商业模式",), "terms": ("商业模式", "收入", "营收", "business", "revenue"), "query": "商业模式 财报 收入 官方披露", "source": "mixed"},
}


def field_for(label):
    low = str(label).strip().lower()
    if low in DIMENSIONS:
        return low
    # Exact aliases first: 用户口碑 must never resolve to 用户画像.
    for key, spec in DIMENSIONS.items():
        if low in spec["aliases"]:
            return key
    for key, spec in DIMENSIONS.items():
        if any(alias in low for alias in spec["aliases"]):
            return key
    return ""


def dimension(label):
    key = field_for(label) or "custom_" + hashlib.sha256(label.encode()).hexdigest()[:10]
    spec = DIMENSIONS.get(key, {"label": label, "terms": (label,), "query": label, "source": "mixed"})
    return {"key": key, "label": label, "query": spec["query"], "source": spec["source"]}


def build_contract(brands, focus, clar=None, query="", now=None):
    clar = clar or {}
    now = now or datetime.now(timezone.utc)
    freshness = str(clar.get("freshness") or "不限时间")
    days = 30 if "一月" in freshness or "一个月" in freshness else None if "不限" in freshness else 365
    dims = list({dimension(f)["key"]: dimension(f) for f in focus}.values())
    from app.core.source_policy import profile
    industry = "automotive" if brands and all(profile(b).get("industry") == "automotive" for b in brands) else "general"
    return {"version": VERSION, "brands": list(dict.fromkeys(brands)), "dimensions": dims,
            "as_of": now.isoformat(), "window_days": days,
            "since": (now - timedelta(days=days)).isoformat() if days else "",
            "freshness": "oneMonth" if days == 30 else "oneYear" if days else "noLimit",
            "market": str(clar.get("market", "")), "user": str(clar.get("user", "")),
            "industry": industry}


def cell_id(brand, key):
    return "cell_" + hashlib.sha256((brand + "\0" + key).encode()).hexdigest()[:16]


def source_time(published_at, contract):
    try:
        date = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
        now = datetime.fromisoformat(contract["as_of"])
        days = (now - date).total_seconds() / 86400
    except (ValueError, TypeError, AttributeError):
        return "unknown"
    if days < 0:
        return "future"
    return "recent_month" if days <= 30 else "recent_quarter" if days <= 90 else "background"


def within_window(claim, contract):
    if contract["window_days"] is None:
        return True
    dates = claim.get("temporal", {}).get("published_at", [])
    if not dates:
        return False
    try:
        now = datetime.fromisoformat(contract["as_of"])
        for value in dates:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
            if not 0 <= (now - date).total_seconds() <= contract["window_days"] * 86400:
                return False
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def stamp_claim(claim, evidences, contract):
    by_id = {e.evidence_id: e for e in evidences}
    # Only anchored supporting sources can establish the time label.
    ids = {s["evidence_id"] for s in claim.get("verification", {}).get("supports", [])
           if s.get("relation") in ("supports", "partial")}
    dates = [by_id[e].published_at for e in ids if e in by_id]
    labels = [source_time(d, contract) for d in dates]
    label = ("future" if "future" in labels else "unknown" if not labels or "unknown" in labels
             else "background" if "background" in labels else "recent_quarter" if "recent_quarter" in labels else "recent_month")
    claim["temporal"] = {"label": label, "published_at": sorted(set(dates)), "as_of": contract["as_of"],
                         "note": "来源发布时间分组，不等于产品变更发生时间；采集时间不替代发布日期"}
    if label == "future":
        from app.core.confidence_policy import empty_assessment
        claim.update(confidence="unverified", cross_validated=False)
        claim.update(confidence_reason="支持来源日期晚于研究截止时间", independent_verification=empty_assessment())
        claim["verification"] = {**claim.get("verification", {}), "verdict": "insufficient", "reason": "支持来源日期晚于研究截止时间"}
    return claim


def build_matrix(contract, claims, evidences):
    by_id = {e.evidence_id: e for e in evidences}
    cells = []
    for brand in contract["brands"]:
        for dim in contract["dimensions"]:
            cid = cell_id(brand, dim["key"])
            facts = []
            for c in claims:
                if c.get("verification", {}).get("verdict") != "supported" or c.get("field") != dim["key"]:
                    continue
                refs = [by_id[e] for e in c.get("evidence_ids", []) if e in by_id]
                # Legacy claims may infer one brand, never count a multi-brand citation for all brands.
                cb = c.get("brand") or (refs[0].brand if refs and len({e.brand for e in refs}) == 1 else "")
                if cb == brand and refs and all(e.brand == brand for e in refs):
                    facts.append(c)
            recent = [c for c in facts if c.get("temporal", {}).get("label") == "recent_month"]
            state = "missing" if not facts else "background_only" if not any(within_window(c, contract) for c in facts) else "covered"
            cells.append({"cell_id": cid, "brand": brand, "dimension": dim["key"], "label": dim["label"],
                          "status": state, "claim_ids": [c["claim_id"] for c in facts],
                          "recent_claim_ids": [c["claim_id"] for c in recent],
                          "high_count": sum(c.get("confidence") == "high" for c in facts),
                          "gap": "缺少本品牌本维度的已核验事实" if not facts else f"仅有窗口外或日期不明资料，缺近{contract['window_days']}天来源" if state == "background_only" else ""})
    return {"contract": contract, "cells": cells, "total": len(cells),
            "covered": sum(c["status"] == "covered" for c in cells),
            "fact_covered": sum(bool(c["claim_ids"]) for c in cells),
            "recent_claim_ids": list(dict.fromkeys(i for c in cells for i in c["recent_claim_ids"]))}


def section_fields(sid, focus):
    selected = [dimension(f)["key"] for f in focus]
    aliases = {"feature": "feature_tree", "pricing": "pricing_model", "persona": "user_persona"}
    key = aliases.get(sid, sid)
    return [key] if key in selected else selected


def merge_rework(previous, revised, targets):
    """Keep a verified source snapshot when a retry yields no replacement proof.

    Contradiction is a hard exception: never rescue that cell with its old facts.
    Retained claims keep their original dates; this cannot fill a recency gap.
    """
    result = [c for c in previous if c.get("cell_id") not in targets]
    retained = []
    for cid in sorted(targets):
        new = [c for c in revised if c.get("cell_id") == cid]
        verdicts = {c.get("verification", {}).get("verdict") for c in new}
        if not verdicts & {"supported", "contradicted"}:
            old = [c for c in previous if c.get("cell_id") == cid and c.get("verification", {}).get("verdict") == "supported"]
            result.extend(old)
            retained.extend(c["claim_id"] for c in old)
        result.extend(new)
    return result, retained


def section_plan(focus):
    aliases = {"feature_tree": "feature", "pricing_model": "pricing", "user_persona": "persona"}
    return [("summary", "研究摘要")] + [(aliases.get(d["key"], d["key"]), d["label"]) for d in
                                          {dimension(f)["key"]: dimension(f) for f in focus}.values()] + [("conclusion", "结论与证据边界")]
