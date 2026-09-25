"""通用搜索采集（第 16 章）。

- 主用博查 Bocha Web Search API（有 key 时），对中文/国内站点友好。
  Endpoint: POST https://api.bocha.cn/v1/web-search
- multi_search：一次跑多条查询并按 URL 去重，用于深度调研多角度检索。
- 相关性过滤：剔除标题/摘要完全不含关键词的结果（避免题不对版）。
- 尽力而为：单条查询失败不抛断，返回已得结果。
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from typing import List, Optional

import httpx
from app.core.source_policy import host_of, matches_domain, canonical_url

from app.core.config import get_settings
from app.core import outbound_limit, performance, trace
from app.core.search_extra import (SearchProviderError, cached, search_anysearch,
                                   search_grok, search_grok_planned, search_grok_x, valid_url, x_post_url)

# 博查异常码 → 人话提示
_BOCHA_ERR = {
    400: "请求参数错误（如缺少 query）",
    401: "博查 API Key 无效或缺失",
    403: "博查账户余额不足，请充值",
    429: "博查请求频率超限，请稍后重试",
    500: "博查搜索服务内部异常",
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_relevant(query: str, title: str, snippet: str) -> bool:
    """简易相关性过滤：检查搜索 query 的核心词是否出现在标题或摘要中。

    避免搜 "Notion 功能对比" 返回汽水音乐之类完全不相关的结果。
    提取 query 中的英文品牌词/中文关键词做匹配。
    """
    if not query:
        return True
    text = f"{title} {snippet}".lower()
    q = query.lower()

    # 提取英文单词（品牌名等），3 个字符以上的都要在结果中出现至少一个
    english_words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", q)
    # 提取中文关键词（2 个字以上的中文字符串）
    chinese_words = re.findall(r"[\u4e00-\u9fa5]{2,}", q)

    must_match = []
    # 英文品牌词（第一个英文词通常是品牌名，必须匹配）
    if english_words:
        must_match.append(english_words[0])
    # 中文第一个名词短语也尽量匹配
    if chinese_words:
        must_match.append(chinese_words[0])

    if not must_match:
        return True

    # 至少要有一个核心词命中（英文不区分大小写）
    for kw in must_match:
        if kw.lower() in text:
            return True

    # 宽松二次校验：只要有任意 2 个查询词命中即可
    all_kw = english_words + chinese_words
    hits = sum(1 for kw in all_kw if kw.lower() in text)
    return hits >= 2


def _search_bocha_uncached(
    query: str,
    *,
    num: int = 10,
    site: Optional[str] = None,
    freshness: str = "noLimit",
) -> list[dict]:
    """用博查 Bocha Web Search 做网页搜索。

    - `site`: 可选，形如 "douyin.com" / "zhihu.com"，映射到博查 include 限定域名。
    - `freshness`: 时效性过滤（noLimit/oneDay/oneWeek/oneMonth/oneYear），聚焦最新数据。
    - 返回的每条都带 title + url + snippet + source + captured_at，便于后续抓取正文。
    - 自动做相关性过滤，剔除明显不相关的结果。
    """
    settings = get_settings()
    if not settings.bocha_api_key:
        raise RuntimeError("未配置 BOCHA_API_KEY，搜索暂不可用")

    # count 取值范围 1-50
    count = max(1, min(int(num), 50))
    # 多取一些以预留过滤余量（相关性过滤会淘汰一部分）
    fetch_count = min(count * 2, 50)
    payload = {
        "query": query,
        "summary": True,
        "freshness": freshness or "noLimit",
        "count": fetch_count,
    }
    if site:
        # 博查用 include 限定网站范围（多个用 | 分隔）
        payload["include"] = site

    endpoint = f"{settings.bocha_base_url.rstrip('/')}/web-search"
    headers = {
        "Authorization": f"Bearer {settings.bocha_api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(
        connect=8, read=settings.search_timeout, write=5, pool=5
    )
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        started = time.perf_counter()
        with outbound_limit.permit(
            outbound_limit.scope("bocha", settings.bocha_base_url, settings.bocha_api_key),
            per_second=settings.bocha_requests_per_second,
            max_in_flight=settings.search_max_in_flight,
            max_wait=settings.search_max_queue_wait,
        ):
            r = client.post(endpoint, headers=headers, json=payload)
        performance.record("search", "bocha", (time.perf_counter() - started) * 1000)
        if r.status_code != 200:
            msg = _BOCHA_ERR.get(r.status_code, f"博查接口返回 HTTP {r.status_code}")
            raise SearchProviderError("bocha", r.status_code, msg)
        body = r.json()

    # 博查在 HTTP 200 时仍可能在 body 内返回错误码
    code = body.get("code")
    if code is not None and int(code) != 200:
        msg = _BOCHA_ERR.get(int(code), body.get("msg") or f"博查返回业务码 {code}")
        raise SearchProviderError("bocha", int(code), msg)

    data = body.get("data") or {}
    web_pages = (data.get("webPages") or {}).get("value") or []

    results: list[dict] = []
    for item in web_pages:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if not valid_url(url):
            continue
        if site and not any(matches_domain(host_of(url), d.strip()) for d in re.split(r"[|,]", site) if d.strip()):
            continue  # Never silently accept results outside the requested domain policy.
        # summary（完整摘要，已开启）优先，缺失时退回 snippet
        snippet = (item.get("summary") or item.get("snippet") or "").strip()
        title = (item.get("name") or "").strip()
        # 相关性过滤：剔除明显不相关的结果（题不对版）
        if not _is_relevant(query, title, snippet):
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": item.get("siteName") or item.get("displayUrl", ""),
                # 标准发布时间用 datePublished（dateLastCrawled 有 UTC+8 坑，不用）
                "captured_at": item.get("datePublished") or "",
                "search_provider": "bocha",
            }
        )
        if len(results) >= count:
            break
    return results


def search_bocha(query: str, *, num: int = 10, site: Optional[str] = None,
                 freshness: str = "noLimit", source_kind: str = "mixed") -> list[dict]:
    settings = get_settings()
    limiter = outbound_limit.scope("bocha", settings.bocha_base_url, settings.bocha_api_key)

    def produce():
        for attempt in range(3):
            try:
                return _search_bocha_uncached(query, num=num, site=site, freshness=freshness)
            except SearchProviderError as exc:
                if exc.status != 429 or attempt == 2:
                    raise
                outbound_limit.cool_down(limiter, min(20.0, 2 ** attempt + 1))
                performance.record("search_retry", "bocha", 0, retry=True)
        return []

    return cached("bocha", settings.bocha_api_key, query, num, site,
                  freshness, source_kind, produce)


def search(query: str, *, num: int = 10, site: Optional[str] = None,
           freshness: str = "noLimit", source_kind: str = "mixed",
           use_grok: bool = False) -> list[dict]:
    """Search multiple providers and deduplicate by original URL."""
    settings = get_settings()
    enabled = {p.strip().lower() for p in settings.search_providers.split(",") if p.strip()}
    providers = []
    if "bocha" in enabled and settings.bocha_api_key:
        providers.append(("bocha", search_bocha))
    if "anysearch" in enabled:
        providers.append(("anysearch", search_anysearch))
    if "grok" in enabled and settings.grok_search_api_key:
        if source_kind == "x" and settings.grok_search_mode == "native":
            providers.append(("grok-x", search_grok_x))
        else:
            grok_provider = (search_grok_planned if settings.grok_search_mode == "planner"
                             else search_grok)
            providers.append(("grok", grok_provider))
    if source_kind in ("community", "x"):
        if source_kind == "x" and settings.grok_search_mode == "native":
            providers.sort(key=lambda item: {"grok-x": 0, "anysearch": 1, "bocha": 2}.get(item[0], 3))
        else:
            providers.sort(key=lambda item: {"anysearch": 0, "bocha": 1, "grok": 2}.get(item[0], 3))
    if not providers:
        raise SearchProviderError("search", 503, "未配置可用搜索服务")
    out, seen, errors = [], set(), []
    for name, provider in providers:
        if name != "grok" and len(out) >= num:
            continue
        # Only the caller's explicitly selected query can spend Grok tokens.
        if name in ("grok", "grok-x") and not use_grok:
            continue
        try:
            rows = provider(query, num=num, site=site, freshness=freshness,
                            source_kind=source_kind)
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
            performance.record("search_error", name, 0)
            trace.record_span(model=name, messages=[{"role": "user", "content": query}],
                              response=type(exc).__name__, decision="搜索源失败，改用其他来源")
            continue
        for row in rows:
            identity = canonical_url(row.get("url", ""))
            if identity and identity not in seen and (source_kind != "x" or x_post_url(row.get("url", ""))):
                seen.add(identity)
                out.append(row)
    if not out and errors:
        raise SearchProviderError("search", 503, ",".join(errors))
    return out


def multi_search(
    queries: List[str],
    *,
    num: int = 10,
    site: Optional[str] = None,
    freshness: str = "noLimit",
    source_kind: str = "mixed",
    use_grok: bool = False,
) -> list[dict]:
    """跑多条查询，按 URL 去重聚合。单条失败跳过（尽力而为）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for index, q in enumerate(queries):
        try:
            for r in search(q, num=num, site=site, freshness=freshness,
                            source_kind=source_kind,
                            use_grok=use_grok and index == 0):
                url = r.get("url", "")
                key = canonical_url(url) or r.get("title", "")
                if not key or key in seen:
                    continue
                seen.add(key)
                r["query"] = q
                out.append(r)
        except Exception as exc:
            trace.record_span(model="search", messages=[{"role": "user", "content": q}],
                              response=type(exc).__name__, decision="搜索请求失败，未当作无结果；" + type(exc).__name__)
            continue
    return out
