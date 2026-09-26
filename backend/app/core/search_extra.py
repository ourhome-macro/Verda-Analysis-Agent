"""AnySearch and Grok web discovery adapters; only fetched pages become Evidence."""
from __future__ import annotations

import json
import random
import re
import threading
import time
from urllib.parse import urlparse

import httpx

from app.core import outbound_limit, performance, research_cache
from app.core.config import get_settings
from app.core.source_policy import canonical_url, host_of, matches_domain

_GROK_UNSUPPORTED: set[str] = set()
_GROK_LOCK = threading.Lock()


class SearchProviderError(RuntimeError):
    def __init__(self, provider: str, status: int, detail: str):
        super().__init__(f"{provider} HTTP {status}: {detail}")
        self.provider = provider
        self.status = status


def valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def x_post_url(url: str) -> bool:
    return (valid_url(url) and site_match(url, "x.com|twitter.com")
            and bool(re.search(r"/status/\d+", urlparse(url).path)))


def site_match(url: str, site: str | None) -> bool:
    return not site or any(matches_domain(host_of(url), part.strip())
                           for part in re.split(r"[|,]", site) if part.strip())


def cache_ttl(freshness: str, source_kind: str, query: str = "") -> int:
    if source_kind in ("community", "x"):
        return 3600
    if re.search(r"价格|定价|售价|月费|pricing|price|subscription", query, re.I):
        return 21600
    return {"oneDay": 3600, "oneWeek": 21600, "oneMonth": 43200,
            "oneYear": 86400, "noLimit": 7 * 86400}.get(freshness, 86400)


def cached(provider: str, credential: str, query: str, num: int, site: str | None,
           freshness: str, source_kind: str, producer):
    cache_key = research_cache.key_for("search", provider,
                                      outbound_limit.scope(provider, "", credential),
                                      query, num, site or "", freshness, source_kind)
    hit = research_cache.get("search", cache_key)
    if hit is not None:
        performance.record("search", provider, 0, cache_hit=True)
        return hit
    rows = producer()
    if rows:
        research_cache.put("search", cache_key, rows, cache_ttl(freshness, source_kind, query))
    return rows


def post_json(provider: str, base_url: str, credential: str, path: str,
              payload: dict, *, timeout: float, rate: float) -> dict:
    settings = get_settings()
    limiter = outbound_limit.scope(provider, base_url, credential)
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    last_error = None
    for attempt in range(3):
        started = time.perf_counter()
        try:
            with outbound_limit.settings_permit(provider, base_url, credential, settings,
                                       per_second=rate,
                                       max_in_flight=settings.search_max_in_flight,
                                       max_wait=settings.search_max_queue_wait):
                with httpx.Client(timeout=httpx.Timeout(connect=8, read=timeout, write=5, pool=5),
                                  follow_redirects=False) as client:
                    response = client.post(f"{base_url.rstrip('/')}/{path.lstrip('/')}",
                                           headers=headers, json=payload)
            performance.record("search", provider, (time.perf_counter() - started) * 1000,
                               retry=attempt > 0)
            if response.status_code == 429:
                hint = response.headers.get("Retry-After", "")
                try:
                    delay = min(30.0, max(1.0, float(hint)))
                except ValueError:
                    delay = min(20.0, 2 ** attempt + random.random())
                outbound_limit.cool_down(limiter, delay)
                last_error = SearchProviderError(provider, 429, "请求频率超限")
                continue
            if response.status_code != 200:
                raise SearchProviderError(provider, response.status_code, "请求失败")
            body = response.json()
            if not isinstance(body, dict):
                raise SearchProviderError(provider, 502, "响应格式错误")
            return body
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            performance.record("search", provider, (time.perf_counter() - started) * 1000,
                               retry=attempt > 0)
            last_error = exc
            if attempt < 2:
                time.sleep(min(3.0, 0.5 * (2 ** attempt) + random.random() / 2))
    raise last_error or SearchProviderError(provider, 503, "搜索不可用")


def _tool_urls(body: dict, tool_type: str, capability: str) -> list[str]:
    usage = body.get("usage") or {}
    details = usage.get("server_side_tool_usage_details") or {}
    tool_count = int(details.get(f"{tool_type}_calls") or usage.get("num_server_side_tools_used") or 0)
    tool_count += sum(item.get("type") == f"{tool_type}_call"
                      for item in body.get("output") or [] if isinstance(item, dict))
    performance.record("grok_tokens", "grok", 0,
                       prompt_tokens=usage.get("input_tokens") or 0,
                       completion_tokens=usage.get("output_tokens") or 0)
    urls = list(body.get("citations") or []) if isinstance(body.get("citations"), list) else []
    for output in body.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            for annotation in content.get("annotations") or []:
                if annotation.get("type") == "url_citation":
                    urls.append(annotation.get("url", ""))
    if tool_count <= 0:
        with _GROK_LOCK:
            _GROK_UNSUPPORTED.add(capability)
        raise SearchProviderError("grok", 501, f"网关未执行 {tool_type}")
    return urls


def search_anysearch(query: str, *, num: int = 10, site: str | None = None,
                     freshness: str = "noLimit", source_kind: str = "mixed") -> list[dict]:
    settings = get_settings()
    count = max(1, min(int(num), 50))

    def produce():
        body = post_json("anysearch", settings.anysearch_base_url, settings.anysearch_api_key,
                         "search", {"query": query, "max_results": min(50, count * 2)},
                         timeout=settings.search_timeout, rate=settings.anysearch_requests_per_second)
        if body.get("code") not in (0, "0", None):
            raise SearchProviderError("anysearch", 502, "搜索业务错误")
        rows = (body.get("data") or {}).get("results") or []
        results = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            if not valid_url(url) or not site_match(url, site) or (source_kind == "x" and not x_post_url(url)):
                continue
            results.append({"title": str(item.get("title") or ""), "url": url,
                            "snippet": str(item.get("snippet") or ""),
                            "source": host_of(url), "captured_at": item.get("date") or "",
                            "search_provider": "anysearch"})
            if len(results) >= count:
                break
        return results

    return cached("anysearch", settings.anysearch_api_key, query, count, site,
                  freshness, source_kind, produce)


def search_grok(query: str, *, num: int = 10, site: str | None = None,
                freshness: str = "noLimit", source_kind: str = "mixed") -> list[dict]:
    """Use structured web-search citations as leads, never the generated prose."""
    settings = get_settings()
    if not settings.grok_search_api_key:
        raise SearchProviderError("grok", 503, "未配置 Grok 搜索凭据")
    capability = outbound_limit.scope("grok-web", settings.grok_search_base_url,
                                      settings.grok_search_api_key + settings.grok_search_model)
    with _GROK_LOCK:
        if capability in _GROK_UNSUPPORTED:
            raise SearchProviderError("grok", 501, "网关未执行 web_search")
    count = max(1, min(int(num), 20))

    def produce():
        domains = [d.strip() for d in re.split(r"[|,]", site or "") if d.strip()][:5]
        tool = {"type": "web_search"}
        if domains:
            tool["filters"] = {"allowed_domains": domains}
        body = post_json("grok", settings.grok_search_base_url, settings.grok_search_api_key,
                         "responses", {"model": settings.grok_search_model,
                                       "input": [{"role": "user", "content":
                                                  f"Search the web for public sources about: {query}. Cite the pages you opened."}],
                                       "tools": [tool], "max_output_tokens": 600},
                         timeout=settings.llm_timeout, rate=settings.grok_requests_per_second)
        urls = _tool_urls(body, "web_search", capability)
        seen, results = set(), []
        for entry in urls:
            url = entry if isinstance(entry, str) else entry.get("url", "") if isinstance(entry, dict) else ""
            canonical = canonical_url(url)
            if not valid_url(url) or not canonical or canonical in seen or not site_match(url, site):
                continue
            seen.add(canonical)
            results.append({"title": f"网页来源 · {host_of(url)}", "url": url,
                            "snippet": "", "source": host_of(url), "captured_at": "",
                            "search_provider": "grok"})
            if len(results) >= count:
                break
        return results

    return cached("grok", settings.grok_search_api_key, query, count, site,
                  freshness, source_kind, produce)


def search_grok_x(query: str, *, num: int = 10, site: str | None = None,
                  freshness: str = "noLimit", source_kind: str = "x") -> list[dict]:
    """Native xAI X Search; requires actual server-side tool execution."""
    settings = get_settings()
    if not settings.grok_search_api_key:
        raise SearchProviderError("grok-x", 503, "未配置 Grok 搜索凭据")
    capability = outbound_limit.scope("grok-x", settings.grok_search_base_url,
                                      settings.grok_search_api_key + settings.grok_search_model)
    with _GROK_LOCK:
        if capability in _GROK_UNSUPPORTED:
            raise SearchProviderError("grok-x", 501, "网关未执行 x_search")
    count = max(1, min(int(num), 20))

    def produce():
        body = post_json(
            "grok", settings.grok_search_base_url, settings.grok_search_api_key,
            "responses", {"model": settings.grok_search_model,
                          "input": [{"role": "user", "content":
                                     f"Search X posts about {query}. Cite individual post URLs."}],
                          "tools": [{"type": "x_search"}], "max_output_tokens": 450},
            timeout=settings.llm_timeout, rate=settings.grok_requests_per_second)
        urls = _tool_urls(body, "x_search", capability)
        seen, rows = set(), []
        for entry in urls:
            url = entry if isinstance(entry, str) else entry.get("url", "") if isinstance(entry, dict) else ""
            canonical = canonical_url(url)
            if not x_post_url(url) or not canonical or canonical in seen:
                continue
            seen.add(canonical)
            rows.append({"title": f"X 帖子 · {host_of(url)}", "url": url, "snippet": "",
                         "source": host_of(url), "captured_at": "", "search_provider": "grok-x"})
            if len(rows) >= count:
                break
        return rows

    return cached("grok-x", settings.grok_search_api_key, query, count,
                  "x.com|twitter.com", freshness, source_kind, produce)


def search_grok_planned(query: str, *, num: int = 10, site: str | None = None,
                        freshness: str = "noLimit", source_kind: str = "mixed") -> list[dict]:
    """Let a Grok model plan one alternate query; AnySearch supplies source URLs."""
    settings = get_settings()
    if not settings.grok_search_api_key:
        raise SearchProviderError("grok", 503, "未配置 Grok 凭据")
    plan_key = research_cache.key_for(
        "grok_plan", outbound_limit.scope("grok", settings.grok_search_base_url,
                                          settings.grok_search_api_key),
        settings.grok_search_model, query, site or "", source_kind)
    variants = research_cache.get("grok_plan", plan_key)
    if variants is None:
        body = post_json(
            "grok", settings.grok_search_base_url, settings.grok_search_api_key,
            "chat/completions",
            {"model": settings.grok_search_model,
             "messages": [
                 {"role": "system", "content":
                  "You plan web searches. Return only JSON: {\"queries\":[\"one alternate query\"]}. "
                  "Keep the exact brand and product names. Do not answer the question or invent source URLs."},
                 {"role": "user", "content":
                  f"Original search: {query}\nSource category: {source_kind}\nDomain constraint: {site or 'none'}"}],
             "temperature": 0, "max_tokens": 180},
            timeout=settings.llm_timeout, rate=settings.grok_requests_per_second)
        usage = body.get("usage") or {}
        performance.record("grok_tokens", "grok", 0,
                           prompt_tokens=usage.get("prompt_tokens") or 0,
                           completion_tokens=usage.get("completion_tokens") or 0)
        try:
            content = body["choices"][0]["message"]["content"] or ""
            content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(content)
            raw = parsed.get("queries", []) if isinstance(parsed, dict) else []
        except (KeyError, IndexError, TypeError, ValueError):
            raw = []
        brand = query.split(maxsplit=1)[0] if query.split() else ""
        variants = []
        for candidate in raw if isinstance(raw, list) else []:
            if not isinstance(candidate, str):
                continue
            candidate = " ".join(candidate.split())[:160]
            if not candidate or candidate.casefold() == query.casefold():
                continue
            if brand and brand.casefold() not in candidate.casefold():
                candidate = f"{brand} {candidate}"[:160]
            if site and "x.com" in site and "site:x.com" not in candidate.lower():
                candidate = f"{candidate} site:x.com"[:160]
            variants.append(candidate)
            break  # One model call and one extra search per requested cell.
        if variants:
            research_cache.put("grok_plan", plan_key, variants,
                               min(3600, cache_ttl(freshness, source_kind, query)))
    if not variants:
        return []
    rows = search_anysearch(variants[0], num=num, site=site, freshness=freshness,
                            source_kind=source_kind)
    return [{**row, "search_provider": "grok+anysearch"} for row in rows]
