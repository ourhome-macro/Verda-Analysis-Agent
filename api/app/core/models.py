"""核心数据模型（第 3 章）：Evidence / Claim / Envelope / 报告结构。

Python 3.9 兼容：使用 typing.Optional / List，避免 `X | None` 运行期解析问题。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Evidence:
    evidence_id: str
    source_url: str
    source_type: str  # official|news|douyin|xiaohongshu|bilibili|weibo|zhihu|review|financial_report
    title: str
    excerpt: str
    captured_at: str
    credibility: float  # 0-100 整数（由 credibility.score_evidence 计算，精确到个位、有差异）
    collected_by: str
    screenshot_path: str = ""
    image_urls: List[str] = field(default_factory=list)
    lang: str = "zh"
    brand: str = ""
    domain: str = ""
    freshness_days: Optional[int] = None  # 距今天数，None=无法解析
    full_text: str = ""  # 保留抽取正文，供按问题选段与报告重载使用
    source_tier: str = "unclassified"
    source_policy_version: str = ""
    canonical_url: str = ""
    origin_url: str = ""
    source_group: str = ""
    content_hash: str = ""
    provenance_reason: str = ""
    fetch_kind: str = "snippet"
    published_at: str = ""
    research_dimensions: List[str] = field(default_factory=list)
    search_freshness: str = ""
    research_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Claim:
    claim_id: str
    text: str
    field: str  # feature_tree|pricing_model|user_persona|swot|sentiment|overview
    evidence_ids: List[str]
    confidence: str  # high|medium|low|unverified
    cross_validated: bool
    author: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Issue:
    issue_id: str
    target: str  # claim_id / section
    severity: str  # high|medium|low
    reason: str
    raised_by: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Envelope:
    msg_id: str
    sender: str
    receiver: str
    task_type: str  # PRODUCE | REWORK | PASS
    payload: Dict[str, Any] = field(default_factory=dict)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    trace_ref: str = ""


def make_claim(
    claim_id: str,
    text: str,
    field_name: str,
    evidence_ids: List[str],
    author: str,
    independent_domains: int = 0,
) -> Claim:
    """Create an unverified proposal. Only the verifier/confidence policy may promote it.

    independent_domains is retained for older callers; counts alone establish no trust.
    """
    return Claim(claim_id, text, field_name, list(dict.fromkeys(evidence_ids)), "unverified", False, author)
