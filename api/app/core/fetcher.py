"""真实网页抓取（第 5 章 通用采集）。

- httpx 拉取 HTML → trafilatura 抽正文 → BeautifulSoup 抽图片。
- 失败降级：返回 snippet 占位，trace 标 degraded，绝不抛断流程。
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx
from app.core.source_policy import REGISTRY, host_of, matches_domain, publisher_key

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(connect=8, read=20, write=5, pool=5)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def is_public_http_url(url: str) -> bool:
    """Reject local hosts and private IP literals before fetching provider URLs."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def fetch_page(url: str, *, fallback_snippet: str = "") -> Dict[str, Any]:
    """抓取单页正文 + 图片。返回 {text, images, ok, degraded}。"""
    result: Dict[str, Any] = {
        "url": url,
        "text": fallback_snippet,
        "images": [],
        "og_image": "",
        "ok": False,
        "degraded": True,
        "fetch_kind": "snippet",
        "origin_url": "",
        "product_links": [],
        "published_at": "",
    }
    if not is_public_http_url(url):
        result["captured_at"] = _now()
        return result
    try:
        def validate_redirect(request: httpx.Request):
            if not is_public_http_url(str(request.url)):
                raise ValueError("unsafe page URL")

        with httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            event_hooks={"request": [validate_redirect]},
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            result["url"] = str(r.url)
            # 编码兜底：httpx 按响应头 charset 解码，遇错误声明会乱码。
            # 若检测到乱码，用 apparent_encoding（chardet/charset_normalizer）重解码。
            html = r.text
            try:
                from app.core.textquality import is_garbled
                if is_garbled(html[:2000]):
                    enc = r.encoding or ""
                    for cand in ("utf-8", "gbk", "gb18030"):
                        if cand.lower() == enc.lower():
                            continue
                        try:
                            redecoded = r.content.decode(cand, errors="strict")
                            if not is_garbled(redecoded[:2000]):
                                html = redecoded
                                break
                        except Exception:
                            continue
            except Exception:
                pass

        text = _extract_text(html)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        published = soup.find("meta", attrs={"property": "article:published_time"}) or soup.find("meta", attrs={"itemprop": "datePublished"})
        result["published_at"] = str(published.get("content", "")) if published else ""
        origin_url = _original_link(html, url)
        product_links = []
        fetch_kind = "body"
        # Reviewed first-party sites commonly render pricing with JavaScript.
        official = any(matches_domain(host_of(url), d) for p in REGISTRY.values() for d in p["domains"])
        if official and (len(text) < 250 or "design" in url):
            rendered = _render_official(url)
            if len(rendered.get("text", "")) > len(text):
                text, fetch_kind = rendered["text"], "rendered"
                product_links = rendered.get("product_links", [])
        extracted = bool(text.strip())
        text = text or fallback_snippet
        # 乱码正文丢弃，退回 snippet
        try:
            from app.core.textquality import is_garbled
            if text and is_garbled(text):
                text = fallback_snippet
                extracted = False
        except Exception:
            pass
        images = _extract_images(html, url)
        og = _extract_og_image(html, url)
        result.update(
            {"text": text[:48000], "images": images[:6], "og_image": og,
             "ok": extracted, "degraded": not extracted,
             "fetch_kind": fetch_kind if extracted else "snippet", "origin_url": origin_url,
             "product_links": product_links}
        )
    except Exception:
        # 降级保留 snippet
        pass
    result["captured_at"] = _now()
    return result


def cached_fetch_page(url: str, *, fallback_snippet: str = "",
                      ttl_seconds: int = 86400) -> Dict[str, Any]:
    """Cache only successfully fetched public bodies; snippets are request-specific."""
    from app.core import db, performance, research_cache
    from app.core.source_policy import canonical_url

    key = research_cache.key_for("page", db.current_visitor() or "server",
                                 canonical_url(url) or url)
    hit = research_cache.get("page", key, max_age_seconds=ttl_seconds)
    if hit is not None:
        performance.record("fetch", host_of(url), 0, cache_hit=True)
        return hit
    started = time.perf_counter()
    result = fetch_page(url, fallback_snippet=fallback_snippet)
    performance.record("fetch", host_of(url), (time.perf_counter() - started) * 1000)
    if result.get("ok") and result.get("fetch_kind") in ("body", "rendered"):
        research_cache.put("page", key, result, ttl_seconds)
    return result


def _original_link(html: str, base_url: str) -> str:
    from bs4 import BeautifulSoup
    import re
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if re.search(r"原文链接|来源原文|转载自|original article|original source", a.get_text(" ", strip=True), re.I):
            target = urljoin(base_url, a["href"])
            if urlparse(target).scheme in ("http", "https"):
                return target
    return ""


def _render_official(url: str) -> dict:
    """Optional JS extraction, only called for reviewed first-party hosts."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="zh-CN")
                page.route("**/*", lambda route: route.abort() if route.request.resource_type in
                           ("image", "media", "font") else route.continue_())
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                if publisher_key(page.url) != publisher_key(url):
                    return {}
                page.wait_for_timeout(3000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
                import re
                hrefs = page.locator("a[href]").evaluate_all("els => els.map(a=>a.href)")
                links = list(dict.fromkeys(h for h in hrefs if publisher_key(h) == publisher_key(url)
                             and re.search(r"/models/|/(?:l[6789]|i[689]|mega)(?:/|$|\?)", h, re.I)))
                return {"text": (page.title() + "\n" + page.locator("body").inner_text(timeout=5000))[:48000],
                        "product_links": links[:6]}
            finally:
                browser.close()
    except Exception:
        return {}


def _extract_text(html: str) -> str:
    try:
        import trafilatura

        out = trafilatura.extract(html, include_comments=False, include_tables=True)
        if out:
            return out.strip()
    except Exception:
        pass
    # 退而求其次：BeautifulSoup 抓段落
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        return "\n".join(p for p in ps if len(p) > 20)
    except Exception:
        return ""


def _extract_images(html: str, base_url: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or src.startswith("data:"):
                continue
            if _looks_like_icon(src):
                continue
            full = urljoin(base_url, src)
            alt = (img.get("alt") or "").strip()
            out.append({"src": full, "alt": alt, "source_url": base_url})
            if len(out) >= 8:
                break
    except Exception:
        pass
    return out


def _extract_og_image(html: str, base_url: str) -> str:
    """优先抓取社媒/媒体分享卡片用的 OG/Twitter 预览大图（最具代表性、可溯源）。"""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for prop in (
            ("property", "og:image"),
            ("property", "og:image:url"),
            ("name", "twitter:image"),
            ("name", "twitter:image:src"),
            ("itemprop", "image"),
        ):
            tag = soup.find("meta", attrs={prop[0]: prop[1]})
            if tag and tag.get("content"):
                src = tag["content"].strip()
                if src and not src.startswith("data:"):
                    return urljoin(base_url, src)
        # link rel image_src 兜底
        link = soup.find("link", attrs={"rel": "image_src"})
        if link and link.get("href"):
            return urljoin(base_url, link["href"].strip())
    except Exception:
        pass
    return ""


_ICON_HINTS = ("logo", "icon", "sprite", "avatar", "favicon", "blank", "spacer", "pixel", "1x1")


def _looks_like_icon(src: str) -> bool:
    s = src.lower()
    return any(h in s for h in _ICON_HINTS)
