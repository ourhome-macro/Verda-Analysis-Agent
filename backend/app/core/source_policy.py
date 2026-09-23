"""Versioned source admission and conservative publisher/origin clustering.

Registry entries are reviewed configuration, never an LLM's guessed hostname.
Unknown brands can still be researched, but unreviewed sites cannot certify a
price or financial fact. Registry extensions belong in code review.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

POLICY_VERSION = "2026-09-23.1"
REGISTRY = {
    "tesla": {"aliases": ["特斯拉", "tesla"], "domains": ["tesla.cn", "tesla.com"],
              "seeds": ["https://www.tesla.cn/model3/design", "https://www.tesla.cn/modely/design"]},
    "byd": {"aliases": ["比亚迪", "byd"], "domains": ["byd.com", "bydauto.com.cn"],
            "seeds": ["https://www.byd.com/cn/parameter-comparison?goodsId=10058", "https://www.byd.com/cn"]},
    "li_auto": {"aliases": ["理想汽车", "理想", "li auto", "lixiang"],
                "domains": ["lixiang.com", "liauto.com"], "seeds": ["https://www.lixiang.com/L6", "https://www.lixiang.com/"]},
    "github": {"aliases": ["github", "copilot"], "domains": ["github.com", "github.blog"], "seeds": []},
    "notion": {"aliases": ["notion"], "domains": ["notion.com", "notion.so"], "seeds": []},
}
REGULATORS = ("sec.gov", "cninfo.com.cn", "sse.com.cn", "szse.cn", "hkexnews.hk")
MEDIA = ("reuters.com", "xinhuanet.com", "news.cn", "people.com.cn", "yicai.com", "caixin.com")
TRACKING = {"spm", "from", "source", "ref", "eqid", "ad_id", "share_token", "sharefrom"}


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def matches_domain(host, domain):
    return host == domain or host.endswith("." + domain)


def profile(brand):
    low = brand.lower().strip()
    return next((v for v in REGISTRY.values() if any(a == low or a in low for a in v["aliases"])),
                {"domains": [], "seeds": []})


def official_for(url, brand):
    host = host_of(url)
    return bool(host) and any(matches_domain(host, d) for d in profile(brand)["domains"])


def canonical_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            return ""
        host = host_of(url)
        host = re.sub(r"^(?:www|m|mobile)\.", "", host)
        port = parts.port
        netloc = host + (f":{port}" if port and port not in (80, 443) else "")
        params = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                        if k.lower() not in TRACKING and not k.lower().startswith("utm_"))
        return urlunsplit(("https", netloc, parts.path.rstrip("/") or "/", urlencode(params), ""))
    except ValueError:
        return ""


def publisher_key(url):
    host = host_of(url)
    for owner, config in REGISTRY.items():
        if any(matches_domain(host, d) for d in config["domains"]):
            return owner
    labels = host.split(".")
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in ("com.cn", "org.cn", "co.uk", "com.au") else suffix


def classify(url, brand):
    host = host_of(url)
    if official_for(url, brand):
        return "official"
    if any(matches_domain(host, d) for d in REGULATORS):
        return "regulatory"
    if any(matches_domain(host, d) for d in MEDIA):
        return "media"
    return "secondary"


def _shingles(text):
    compact = re.sub(r"\s+", "", text.lower())[:20000]
    return {compact[i:i + 12] for i in range(0, max(0, len(compact) - 11), 6)}


def annotate_sources(evidences):
    """Union canonical duplicates, matching attributed originals and near copies.

    Publisher grouping intentionally undercounts independent sources. An origin
    link is only an attribution hint, never proof that a secondary page is official.
    """
    parents = list(range(len(evidences)))
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    def union(i, j):
        parents[root(j)] = root(i)
    signatures, origins, publishers, reasons = [], [], [], []
    for ev in evidences:
        ev.canonical_url = canonical_url(ev.source_url)
        ev.source_tier = classify(ev.source_url, ev.brand)
        ev.source_policy_version = POLICY_VERSION
        ev.content_hash = hashlib.sha256((ev.full_text or ev.excerpt).encode("utf-8")).hexdigest()
        signatures.append(_shingles(ev.full_text or ev.excerpt))
        origins.append(canonical_url(ev.origin_url) or ev.canonical_url)
        publishers.append(publisher_key(ev.source_url))
        reasons.append({"publisher"})
    for i in range(len(evidences)):
        for j in range(i):
            a, b = evidences[i], evidences[j]
            reason = ""
            if publishers[i] and publishers[i] == publishers[j]:
                reason = "same_publisher"
            if a.canonical_url and a.canonical_url == b.canonical_url:
                reason = "canonical_duplicate"
            if origins[i] and origins[i] == origins[j]:
                reason = "shared_original"
            s1, s2 = signatures[i], signatures[j]
            if min(len(s1), len(s2)) >= 20 and len(s1 & s2) / max(1, min(len(s1), len(s2))) >= 0.82:
                reason = "near_duplicate"
            if reason:
                union(i, j)
                reasons[i].add(reason)
                reasons[j].add(reason)
    groups = {}
    for i in range(len(evidences)):
        groups.setdefault(root(i), []).append(i)
    for indices in groups.values():
        key = hashlib.sha256("|".join(sorted({origins[i] for i in indices})).encode()).hexdigest()[:16]
        for i in indices:
            evidences[i].source_group = "origin_" + key
            evidences[i].provenance_reason = ",".join(sorted(reasons[i]))
    return evidences


def admission(claim, supports, by_id):
    """Official first-party evidence is mandatory for pricing/product facts.

    Financial assertions require a regulator or an official financial document.
    Attributed reposts and snippets cannot meet this requirement.
    """
    field = claim.get("field", "")
    from app.core.claim_kinds import claim_kind
    if claim_kind(claim) == "observed_fact":
        external = [p for s in supports for p in by_id.get(s["evidence_id"], [])
                    if p.get("source_tier") in ("media", "regulatory") and p.get("fetch_kind") in ("body", "rendered")]
        return (True, "") if external else (False, "实际成交、实测或第三方评级需要独立记录，厂商标价或宣传不能替代")
    financial = bool(re.search(r"营收|净利|财报|revenue|net income", claim.get("text", ""), re.I))
    if field not in ("pricing_model", "feature_tree") and not financial:
        return True, ""
    if financial:
        allowed = lambda p: p.get("source_tier") == "regulatory" or (
            p.get("source_tier") == "official" and re.search(r"财报|业绩|annual.report|financial|earnings|investor", p.get("title", "") + p.get("source_url", ""), re.I))
    else:
        allowed = lambda p: p.get("source_tier") == "official"
    usable = [p for s in supports for p in by_id.get(s["evidence_id"], [])
              if allowed(p) and p.get("fetch_kind") in ("body", "rendered")]
    referenced_brands = {p.get("brand") for s in supports for p in by_id.get(s["evidence_id"], []) if p.get("brand")}
    primary_brands = {p.get("brand") for p in usable}
    if not usable or not referenced_brands <= primary_brands:
        return False, "来源准入未通过：产品/价格须有对应品牌官方正文，财务须财报或监管原文；摘要和转载不充当一手证据"
    return True, ""
