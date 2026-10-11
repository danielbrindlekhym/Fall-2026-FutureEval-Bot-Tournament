from __future__ import annotations

import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from lxml import html

from .core import Config, Contract, utcnow


def public_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return False
        addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        return bool(addresses) and all(ipaddress.ip_address(address[4][0]).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def fetch_page(url: str, timeout: float = 10) -> dict:
    for redirect in range(4):
        if not public_url(url):
            raise ValueError("Research URL must resolve to a public address")
        with requests.get(url, timeout=timeout, stream=True, allow_redirects=False,
            headers={"User-Agent": "FutureEvalResearchBot/0.1 (public forecasting research)"}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if not any(t in content_type for t in ("html", "text/plain", "application/json")):
                raise ValueError("Unsupported source content type")
            chunks, size = [], 0
            for chunk in response.iter_content(16384):
                chunks.append(chunk)
                size += len(chunk)
                if size >= 600_000:
                    break
            raw = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            published = None
            title = ""
            if "html" in content_type:
                tree = html.fromstring(raw)
                published_values = tree.xpath('//meta[@property="article:published_time"]/@content | //meta[@property="article:modified_time"]/@content | //time/@datetime')
                published = published_values[0] if published_values else None
                title = tree.xpath("string(//title)")
                for item in tree.xpath("//script|//style|//nav|//footer|//header|//aside"):
                    item.drop_tree()
                raw = tree.text_content()
            text = re.sub(r"\s+", " ", raw).strip()
            # Keep bounded extracts, with provenance rather than untraceable summaries.
            return {"url": clean_url(url), "title": title[:200], "published_or_updated": published,
                    "retrieved_at": utcnow().isoformat(), "excerpt": text[:7500], "source_kind": "page_extract"}
    raise ValueError("Too many research redirects")


def research(contract: Contract, config: Config, stop_at: float) -> dict:
    from ddgs import DDGS
    sources, errors, seen = [], [], set()
    linked = re.findall(r'https?://[^\s<>"\)\]]+', "\n".join([contract.resolution, contract.fine_print, contract.background]))
    candidates = []
    for url in linked:
        if urlsplit(url).hostname not in {"www.metaculus.com", "metaculus.com"}:
            candidates.append({"url": clean_url(url), "priority": 0})
    queries = [contract.title, re.sub(r"[?]", "", contract.title)[:170] + " historical data statistics"]
    for query in queries:
        if time.monotonic() + 15 >= stop_at:
            errors.append("Search truncated by deadline budget")
            break
        try:
            results = DDGS(timeout=7).text(query, max_results=config.search_results_per_query)
            for result in results:
                url = clean_url(result.get("href", ""))
                if not url.startswith(("https://", "http://")) or url in seen:
                    continue
                seen.add(url)
                sources.append({"url": url, "title": result.get("title", "")[:200],
                    "excerpt": result.get("body", "")[:1500], "retrieved_at": utcnow().isoformat(),
                    "published_or_updated": None, "source_kind": "search_snippet", "query": query})
                candidates.append({"url": url, "priority": 1})
        except Exception as error:
            errors.append("Search unavailable: " + type(error).__name__)
    fetched = set()
    for candidate in sorted(candidates, key=lambda c: c["priority"]):
        url = candidate["url"]
        if url in fetched:
            continue
        if len(fetched) >= config.max_pages_to_fetch or time.monotonic() + 12 >= stop_at:
            break
        fetched.add(url)
        try:
            page = fetch_page(url, timeout=min(8, max(1, stop_at - time.monotonic())))
            sources = [s for s in sources if s["url"] != page["url"]]
            sources.insert(0, page)
        except Exception as error:
            errors.append("Source unavailable: " + type(error).__name__)
    return {"sources": sources[:12], "errors": errors, "retrieved_at": utcnow().isoformat(),
            "limitations": "Extracts may omit important context. Unknown publication dates are not evidence of recency. Search snippets are weaker than original pages."}
