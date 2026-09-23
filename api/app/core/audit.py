"""质检与反馈闭环（对应需求 12：真实可触发的返工闭环）。

evaluate_quality：基于 claims/evidences/structured 计算可量化质量指标 + 暴露问题（Issue）。
decide_rework：根据质量指标决定是否打回 collect（补采）或 analyze（重分析），产出 Envelope。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.core.fetcher import domain_of
from app.core.models import Envelope
from app.core.research_contract import build_contract, build_matrix
from app.core.claim_verifier import supported_claims


@dataclass
class QualityReport:
    coverage_by_dimension: Dict[str, bool] = field(default_factory=dict)
    coverage_by_brand: Dict[str, Dict[str, int]] = field(default_factory=dict)
    confidence_ratio: float = 0.0
    independent_verification_ratio: float = 0.0
    schema_completeness: float = 0.0
    dimension_coverage_rate: float = 0.0
    brand_coverage_rate: float = 0.0
    freshness_coverage_rate: float = 0.0
    issues: List[Dict[str, Any]] = field(default_factory=list)
    research_matrix: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coverage_by_dimension": self.coverage_by_dimension,
            "coverage_by_brand": self.coverage_by_brand,
            "confidence_ratio": self.confidence_ratio,
            "independent_verification_ratio": self.independent_verification_ratio,
            "schema_completeness": self.schema_completeness,
            "dimension_coverage_rate": self.dimension_coverage_rate,
            "brand_coverage_rate": self.brand_coverage_rate,
            "freshness_coverage_rate": self.freshness_coverage_rate,
            "issues": self.issues,
            "research_matrix": self.research_matrix,
        }

    def summary(self) -> Dict[str, Any]:
        """供前端返工卡片展示的精简指标。"""
        return {
            "confidence_ratio": round(self.confidence_ratio * 100),
            "independent_verification_ratio": round(self.independent_verification_ratio * 100),
            "dimension_coverage": round(self.dimension_coverage_rate * 100),
            "brand_coverage": round(self.brand_coverage_rate * 100),
            "freshness_coverage": round(self.freshness_coverage_rate * 100),
            "schema_completeness": round(self.schema_completeness * 100),
        }


def evaluate_quality(brands, focus, claims, evidences, structured, *, min_indep_domains=2) -> QualityReport:
    qr = QualityReport()
    verified = supported_claims(claims)
    qr.confidence_ratio = round(sum(c.get("confidence") == "high" for c in verified) / max(1, len(claims)), 3)
    qr.independent_verification_ratio = round(sum(bool(c.get("cross_validated")) for c in verified) / max(1, len(claims)), 3)
    contract = structured.get("research_matrix", {}).get("contract") or build_contract(brands, focus)
    matrix = build_matrix(contract, claims, evidences)
    qr.research_matrix = matrix
    for dim in contract["dimensions"]:
        cells = [c for c in matrix["cells"] if c["dimension"] == dim["key"]]
        qr.coverage_by_dimension[dim["label"]] = bool(cells) and all(c["claim_ids"] for c in cells)
    for brand in brands:
        evs = [e for e in evidences if e.brand == brand]
        cells = [c for c in matrix["cells"] if c["brand"] == brand]
        qr.coverage_by_brand[brand] = {
            "evidence": len(evs), "domains": len({domain_of(e.source_url) for e in evs}),
            "independent_sources": len({e.source_group or domain_of(e.source_url) for e in evs}),
            "primary_sources": sum(e.source_tier in ("official", "regulatory") and e.fetch_kind in ("body", "rendered") for e in evs),
            "verified_claims": sum(len(c["claim_ids"]) for c in cells),
            "covered_cells": sum(bool(c["claim_ids"]) for c in cells), "required_cells": len(cells)}
    qr.dimension_coverage_rate = round(sum(qr.coverage_by_dimension.values()) / max(1, len(contract["dimensions"])), 3)
    qr.brand_coverage_rate = round(sum(v["covered_cells"] == v["required_cells"] and v["required_cells"] > 0
                                         for v in qr.coverage_by_brand.values()) / max(1, len(brands)), 3)
    # Schema completeness now means required cells, not unrelated pricing/persona schemas.
    qr.schema_completeness = round(matrix["fact_covered"] / max(1, matrix["total"]), 3)
    qr.freshness_coverage_rate = round(matrix["covered"] / max(1, matrix["total"]), 3)
    for cell in matrix["cells"]:
        if cell["status"] != "covered":
            qr.issues.append({"issue_id": cell["cell_id"], "target": "cell:" + cell["cell_id"],
                              "severity": "high" if cell["status"] == "missing" else "medium",
                              "reason": f"{cell['brand']} × {cell['label']}：{cell['gap']}",
                              "brand": cell["brand"], "dimension": cell["dimension"],
                              "cell_id": cell["cell_id"], "raised_by": "L3-003"})
    return qr


def llm_quality_review(
    query: str,
    brands: List[str],
    focus: List[str],
    claims: List[Dict[str, Any]],
    structured: Dict[str, Any],
    qr: "QualityReport",
    model: str = None,
) -> Dict[str, Any]:
    """质检官用 LLM 对当前分析做真实『审阅』（非纯规则）：逐维度打分 + 指出问题 + 给改进建议。

    产出结构化评审意见，让「质检审裁」阶段有真实的对比、审阅与可执行的调优建议
    （对应评分维度：反馈闭环真实可触发、重做后有改善）。失败时退回基于规则指标的兜底意见。
    """
    from app.core.llm import chat_json

    claim_lines = "\n".join(
        f"- [{c.get('brand','')}|{c.get('confidence','?')}|{c.get('field','')}|{c.get('verification',{}).get('verdict')}|{c.get('temporal',{}).get('label','unknown')}] {c.get('text','')}"
        for c in claims
    ) or "（暂无论点）"
    sc_dims = "、".join(f"{k}:{'已覆盖' if v else '缺失'}"
                       for k, v in qr.coverage_by_dimension.items()) or "无"
    brand_cov = "、".join(f"{b}({v.get('covered_cells',0)}/{v.get('required_cells',0)}格，{v.get('verified_claims',0)}条核验事实)"
                         for b, v in qr.coverage_by_brand.items()) or "无"
    fallback = {
        "verdict": "pass" if not qr.issues else "rework",
        "scores": {
            "证据充分性": round(qr.brand_coverage_rate * 100),
            "维度完整性": round(qr.dimension_coverage_rate * 100),
            "结论置信度": round(qr.confidence_ratio * 100),
            "结构化完整度": round(qr.schema_completeness * 100),
        },
        "review": f"基于规则指标：维度覆盖 {round(qr.dimension_coverage_rate*100)}%、"
                  f"品牌覆盖 {round(qr.brand_coverage_rate*100)}%、"
                  f"高置信占比 {round(qr.confidence_ratio*100)}%。",
        "issues": [i.get("reason", "") for i in qr.issues[:6]],
        "suggestions": [],
    }
    try:
        data = chat_json(
            [
                {"role": "system", "content": (
                    "你是竞品分析报告的质检官（L3 决策层）。请对下面这份『分析中间产物』做严格的质量审阅，"
                    "此阶段是原子事实供给，不要求提前写出战略判断。官方一手正文足以支撑该品牌价格/配置事实，"
                    "不要仅因单源导致低置信或缺少跨域引用就判返工；检查是否覆盖每个品牌及用户要求维度。"
                    "事实可信程度与独立验证程度是不同指标：官方标价可以高可信且只有一个原始来源，不得混为事实准确率。"
                    "严格区分事实覆盖与时效覆盖：background_only格已有核验事实，只缺窗口内日期，不能说该品牌或维度没有事实。"
                    "像券商内核/主编终审一样，逐维度打分（0-100 整数，要有真实差异、不要清一色整十），"
                    "指出具体问题，并给出可执行的改进建议。最后给整体结论 pass（达标）或 rework（需返工）。"
                    '只输出 JSON：{"verdict":"pass|rework",'
                    '"scores":{"证据充分性":int,"维度完整性":int,"结论置信度":int,"结构化完整度":int,"交叉验证":int},'
                    '"review":"一段总体评审意见（点明亮点与短板）",'
                    '"issues":["具体问题1","具体问题2"],'
                    '"rework_cells":[{"brand":"指定品牌","dimension":"契约中的维度key","reason":"具体补采目标"}],'
                    '"suggestions":["可执行改进建议1","改进建议2"]}。只输出 JSON。'
                )},
                {"role": "user", "content": (
                    f"调研主题：{query}\n竞品：{'、'.join(brands)}\n重点维度：{'、'.join(focus)}\n"
                    f"研究契约与截止日期：{qr.research_matrix.get('contract', {})}\n"
                    "需要返工时必须输出rework_cells定位到品牌与维度。不得扩大用户维度；"
                    "年份款号不等于发布日期；当前截止日期之前的发布不是未来信息。"
                    "完整的来源声明与可跨品牌直接比较不是一回事；不应因单一官方来源本身要求返工。\n"
                    f"规则侧指标 → 维度覆盖：{sc_dims}；品牌证据覆盖：{brand_cov}；"
                    f"高置信占比：{round(qr.confidence_ratio*100)}%；结构化完整度：{round(qr.schema_completeness*100)}%\n"
                    f"矩阵缺口：{[i['reason'] for i in qr.issues]}\n已提炼论点：\n{claim_lines}"
                )},
            ],
            max_tokens=2000, temperature=0.3, model=model,
            purpose="质检官审阅：逐维度打分+问题+改进建议",
        )
        if isinstance(data, dict) and data.get("scores"):
            scores = {str(k): _clamp_score(v) for k, v in (data.get("scores") or {}).items()}
            return {
                "verdict": "rework" if str(data.get("verdict")) == "rework" else "pass",
                "scores": scores or fallback["scores"],
                "review": str(data.get("review") or fallback["review"]),
                "issues": [str(x) for x in (data.get("issues") or []) if str(x).strip()][:8],
                "suggestions": [str(x) for x in (data.get("suggestions") or []) if str(x).strip()][:8],
                "rework_cells": data.get("rework_cells", []) if isinstance(data.get("rework_cells"), list) else [],
            }
    except Exception:
        pass
    return fallback


def _clamp_score(v) -> int:
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def decide_rework(qr: QualityReport, review=None) -> List[Envelope]:
    """One bounded envelope explicitly names missing brand/dimension cells."""
    cells = [{k: issue[k] for k in ("cell_id", "brand", "dimension")}
             for issue in qr.issues if issue.get("target", "").startswith("cell:")]
    allowed = {(c["brand"], c["dimension"]): c for c in qr.research_matrix.get("cells", [])}
    for item in (review or {}).get("rework_cells", []):
        if not isinstance(item, dict):
            continue
        cell = allowed.get((item.get("brand"), item.get("dimension")))
        if cell and not any(c["cell_id"] == cell["cell_id"] for c in cells):
            cells.append({**{k: cell[k] for k in ("cell_id", "brand", "dimension")},
                          "reason": str(item.get("reason", ""))[:400]})
    if not cells:
        return []
    return [Envelope(msg_id="env_" + uuid.uuid4().hex[:8], sender="L3-003", receiver="collect",
                     task_type="REWORK", payload={"cells": cells, "reason": "按品牌×维度缺口定向补采并重新核验"},
                     issues=qr.issues)]
