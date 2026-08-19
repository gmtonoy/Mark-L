"""Article extraction and news briefing helpers for JARVIS.

This module turns search-result URLs into actual readable article content.
It intentionally keeps retrieval separate from web_search.py so future tools
(email digests, monitored-topic briefings, etc.) can reuse the same reader.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 8
MAX_ARTICLE_CHARS = 6500
MAX_SOURCE_ARTICLES = 4


@dataclass
class Article:
    title: str
    url: str
    source: str
    text: str


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_api_key() -> str:
    cfg_path = _base_dir() / "config" / "api_keys.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        return host or "unknown source"
    except Exception:
        return "unknown source"


def _looks_like_article_paragraph(text: str) -> bool:
    """Reject navigation, cookie notices and other obvious page chrome."""
    if len(text) < 55:
        return False
    low = text.lower()
    blocked = (
        "accept cookies",
        "cookie policy",
        "privacy policy",
        "terms of service",
        "sign up for",
        "subscribe to",
        "all rights reserved",
        "advertisement",
        "enable javascript",
    )
    return not any(term in low for term in blocked)


def extract_article(url: str, title_hint: str = "", source_hint: str = "") -> Article:
    """Fetch a URL and extract the most article-like body text available."""
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "xml" not in content_type:
        raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "form", "nav", "footer", "aside"]):
        tag.decompose()

    title = _clean_text(title_hint)
    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = _clean_text(og_title.get("content", ""))
        elif soup.title:
            title = _clean_text(soup.title.get_text(" ", strip=True))
    title = title or "Untitled article"

    # Prefer semantic article containers, then fall back to all paragraphs.
    root = soup.find("article") or soup.find("main") or soup
    paragraphs: list[str] = []
    seen: set[str] = set()

    for p in root.find_all("p"):
        text = _clean_text(p.get_text(" ", strip=True))
        if not _looks_like_article_paragraph(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        paragraphs.append(text)

    # Some news sites do not use <p> inside their article container.
    if len(" ".join(paragraphs)) < 500 and root is not soup:
        for p in soup.find_all("p"):
            text = _clean_text(p.get_text(" ", strip=True))
            if not _looks_like_article_paragraph(text):
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            paragraphs.append(text)

    article_text = "\n\n".join(paragraphs).strip()
    if len(article_text) < 250:
        raise ValueError("Could not extract enough article text")

    return Article(
        title=title,
        url=response.url or url,
        source=_clean_text(source_hint) or _domain(response.url or url),
        text=article_text[:MAX_ARTICLE_CHARS],
    )


def fetch_articles(results: Iterable[dict], limit: int = MAX_SOURCE_ARTICLES) -> list[Article]:
    """Fetch several search results concurrently while preserving result order."""
    candidates = []
    for item in results:
        url = (item.get("url") or item.get("href") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        candidates.append(
            (
                url,
                (item.get("title") or "").strip(),
                (item.get("source") or "").strip(),
            )
        )
        if len(candidates) >= limit:
            break

    if not candidates:
        return []

    fetched: dict[int, Article] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        future_map = {
            pool.submit(extract_article, url, title, source): index
            for index, (url, title, source) in enumerate(candidates)
        }
        for future in concurrent.futures.as_completed(future_map):
            index = future_map[future]
            try:
                fetched[index] = future.result()
            except Exception as exc:
                print(f"[NewsReader] Could not read article #{index + 1}: {exc}")

    return [fetched[i] for i in sorted(fetched)]


def _fallback_digest(articles: list[Article]) -> str:
    lines = ["I read the available article pages. Here is the extracted reporting:\n"]
    for i, article in enumerate(articles, 1):
        excerpt = _clean_text(article.text[:900])
        lines.extend(
            [
                f"{i}. {article.title}",
                f"   Source: {article.source}",
                f"   {excerpt}",
                f"   URL: {article.url}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def summarize_articles(articles: list[Article], topic: str = "") -> str:
    """Use one Gemini call to build a concise multi-source briefing from full article text."""
    if not articles:
        return ""

    source_blocks = []
    for i, article in enumerate(articles, 1):
        source_blocks.append(
            f"ARTICLE {i}\n"
            f"TITLE: {article.title}\n"
            f"SOURCE: {article.source}\n"
            f"URL: {article.url}\n"
            f"TEXT:\n{article.text}\n"
        )

    prompt = (
        "You are preparing a factual news briefing from article text that has already been fetched. "
        "Do not rely on outside knowledge. Do not invent details. "
        "For each article, give 2-4 sentences explaining what actually happened and why it matters. "
        "If multiple articles cover the same event, combine them and note meaningful differences. "
        "Keep names, dates, numbers, and uncertainty accurate. Include the source name and URL after each item. "
        "Do not merely repeat headlines and do not tell the reader to visit the link.\n\n"
        f"TOPIC: {topic or 'current news'}\n\n"
        + "\n\n".join(source_blocks)
    )

    try:
        from google import genai

        client = genai.Client(api_key=_get_api_key())
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = (response.text or "").strip()
        if text:
            return text
    except Exception as exc:
        print(f"[NewsReader] Gemini summarization failed: {exc}")

    return _fallback_digest(articles)


def build_news_brief(results: list[dict], topic: str = "") -> str:
    articles = fetch_articles(results)
    if not articles:
        return ""
    return summarize_articles(articles, topic=topic)


def read_url(url: str) -> str:
    """Read and summarize one direct article URL."""
    try:
        article = extract_article(url)
    except Exception as exc:
        return f"Could not read that article: {exc}"
    return summarize_articles([article], topic=article.title)
