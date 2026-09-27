"""Bounded, inspectable search plans for the fixed research contract."""
from __future__ import annotations

import re
from datetime import datetime

from app.core.research_contract import field_for


def _topic(value: object) -> str:
    if not isinstance(value, str) or "http://" in value or "https://" in value:
        return ""
    return re.sub(r"\s+", " ", value).strip()[:140]


def initial_topics(plan: dict, contract: dict) -> dict[str, str]:
    """Use dimension-specific LLM queries; map legacy angles when available."""
    dimensions = {d["key"]: d for d in contract["dimensions"]}
    topics: dict[str, str] = {}
    for row in plan.get("search_plan") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("dimension", "")).strip().lower()
        key = next((k for k, d in dimensions.items()
                    if name in (k.lower(), d["label"].lower())), field_for(name))
        value = _topic(row.get("query"))
        if key in dimensions and value:
            topics[key] = value
    angles = [_topic(x) for x in plan.get("angles") or []]
    angles = [x for x in angles if x]
    for key, dim in dimensions.items():
        words = [w for w in re.split(r"\s+", dim["query"] + " " + dim["label"]) if len(w) >= 2]
        matched = next((a for a in angles if any(w in a for w in words)), "")
        base = topics.get(key) or dim["query"]
        topics[key] = _topic(f"{base[:100]} {matched}" if matched and matched not in base else base)
    return topics


def revise_plan(contract: dict, matrix: dict, cells: list[dict],
                initial: dict[str, str], review: dict, round_number: int) -> dict:
    """Revise queries from observed gaps without changing user scope or freshness."""
    by_cell = {c["cell_id"]: c for c in matrix.get("cells", [])}
    by_dim = {d["key"]: d for d in contract["dimensions"]}
    as_of = datetime.fromisoformat(contract["as_of"])
    date_hint = f"{as_of.year}年{as_of.month}月" if contract.get("window_days") == 30 else str(as_of.year)
    topics: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for target in cells:
        cid = target.get("cell_id")
        current = by_cell.get(cid)
        if not current or current["brand"] != target.get("brand") or current["dimension"] != target.get("dimension"):
            continue
        dim = by_dim[current["dimension"]]
        base = initial.get(dim["key"]) or dim["query"]
        state = current["status"]
        if state == "background_only":
            suffix = f"{date_hint} 更新日志 发布公告" if round_number == 1 else f"{date_hint} 最新发布 变更记录"
            reason = "已有核验事实，但缺研究窗口内发布日期"
        elif state == "missing":
            suffix = (f"{date_hint} 社区 真实使用评价" if dim["source"] == "community"
                      else f"{date_hint} 官方说明 原始资料")
            if round_number == 2:
                suffix += " 版本 适用范围"
            reason = "缺少本品牌本维度的已核验事实"
        else:
            suffix = ("车型 版本 含税 计费口径" if dim["key"] == "pricing_model"
                      else "产品规格 同口径比较" if dim["key"] == "feature_tree"
                      else "原始资料 适用范围")
            if round_number == 2:
                suffix += f" {date_hint}"
            reason = "质检要求提高可比性或来源质量"
        topics[cid] = _topic(f"{base[:85]} {suffix}")
        reasons[cid] = target.get("reason") or reason
    return {"version": round_number + 1, "round": round_number,
            "contract_version": contract["version"], "as_of": contract["as_of"],
            "reason": "依据矩阵缺口和质检结果调整目标单元的检索词",
            "review_verdict": review.get("verdict", ""),
            "targets": list(topics), "search_topics": topics, "target_reasons": reasons}
