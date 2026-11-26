"""SSRF-safe web fetcher and HTML-to-Clean-Markdown extractor."""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

import html2text
import httpx
from bs4 import BeautifulSoup

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_REDIRECTS = 5


class ExtractionError(Exception):
    """Raised when URL validation or content extraction fails."""


@dataclass(frozen=True)
class ExtractedDocument:
    url: str
    title: str
    description: str
    markdown: str
    word_count: int
    char_count: int
    links: tuple[dict[str, str], ...] = ()


HtmlFetcher = Callable[[str], Awaitable[tuple[str, str]]]


def validate_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ExtractionError("Only http and https URLs are supported")
    if not parsed.hostname:
        raise ExtractionError("URL must include a valid hostname")

    host = parsed.hostname
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise ExtractionError("Localhost URLs are not allowed")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ExtractionError(f"DNS resolution failed for {host}") from exc

    for info in infos:
        ip_str = info[4][0]
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ExtractionError(f"Blocked non-public target IP: {ip_str}")

    return url.strip()


async def default_fetch_html(url: str) -> tuple[str, str]:
    headers = {
        "User-Agent": "NeonExtractBot/0.1 (+https://github.com/austneo)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
    }
    current_url = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=15.0) as client:
        for _ in range(MAX_REDIRECTS + 1):
            resp = await client.get(current_url, headers=headers)
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ExtractionError("Redirect response missing Location header")
                current_url = validate_public_url(urljoin(str(resp.url), location))
                continue
            resp.raise_for_status()
            if len(resp.content) > MAX_RESPONSE_BYTES:
                raise ExtractionError("Response exceeds 2 MiB limit")
            return str(resp.url), resp.text
    raise ExtractionError("Too many redirects")


def html_to_clean_markdown(
    html: str,
    *,
    include_links: bool = True,
    base_url: str = "",
    extract_links: bool = False,
) -> tuple[str, str, str, tuple[dict[str, str], ...]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = desc_tag.get("content", "").strip() if desc_tag and desc_tag.get("content") else ""

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    main_node = soup.find("main") or soup.find("article") or soup.body or soup
    links_list: list[dict[str, str]] = []
    if extract_links:
        seen_hrefs: set[str] = set()
        for a_tag in main_node.find_all("a", href=True):
            href = urljoin(base_url, str(a_tag.get("href", "")).strip())
            parsed_href = urlparse(href)
            if parsed_href.scheme not in ("http", "https") or href in seen_hrefs:
                continue
            text = " ".join(a_tag.get_text(" ", strip=True).split())
            if not text:
                continue
            seen_hrefs.add(href)
            links_list.append({"text": text[:160], "url": href})
            if len(links_list) >= 100:
                break

    converter = html2text.HTML2Text()
    converter.ignore_links = not include_links
    converter.ignore_images = True
    converter.body_width = 0
    raw_md = converter.handle(str(main_node))
    clean_md = re.sub(r"\n{3,}", "\n\n", raw_md).strip()
    return title, description, clean_md, tuple(links_list)


async def extract_url(
    url: str,
    *,
    include_links: bool = True,
    extract_links: bool = False,
    max_chars: int | None = None,
    fetcher: HtmlFetcher | None = None,
) -> ExtractedDocument:
    validated = validate_public_url(url)
    fetch_fn = fetcher or default_fetch_html
    final_url, html = await fetch_fn(validated)
    title, description, md, links = html_to_clean_markdown(
        html,
        include_links=include_links,
        base_url=final_url,
        extract_links=extract_links,
    )
    if max_chars is not None and max_chars > 0 and len(md) > max_chars:
        md = md[:max_chars].rstrip()
    words = len(md.split())
    return ExtractedDocument(
        url=final_url,
        title=title,
        description=description,
        markdown=md,
        word_count=words,
        char_count=len(md),
        links=links,
    )
