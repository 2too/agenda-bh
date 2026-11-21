"""Website fetcher: Playwright renders the page, BeautifulSoup cleans it."""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.robotparser
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..config import Settings, fill_url
from ..models import RawItem

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 30_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 AgendaBH/1.0"
)
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


class RobotsBlocked(Exception):
    pass


def robots_from_response(status: int, text: str) -> urllib.robotparser.RobotFileParser | None:
    """Only a robots.txt the site actually serves (HTTP 200) counts.

    Python's own reader treats a 401/403 as "everything forbidden", but those
    usually come from a firewall blocking the robots.txt request itself, not
    from the site's rules. Unreadable = no rules known = allowed.
    """
    if status != 200:
        return None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def allowed_by_robots(url: str, browser: "Browser | None" = None) -> bool:
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots_cache:
        try:
            status, text = browser.get(base + "/robots.txt") if browser else (0, "")
        except Exception:
            status, text = 0, ""
        _robots_cache[base] = robots_from_response(status, text)
        if status != 200:
            log.info("%s: robots.txt not readable (HTTP %s), proceeding politely", base, status)
    rp = _robots_cache[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


# ---------- HTML helpers (pure functions, unit-tested) ----------

def extract_jsonld_events(html: str) -> list[dict]:
    """Return every schema.org Event object found in JSON-LD blocks."""
    soup = BeautifulSoup(html, "lxml")
    found: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x.endswith("Event") for x in types):
                found.append(node)
            for key in ("@graph", "itemListElement", "item", "subEvent"):
                if key in node:
                    walk(node[key])

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_TEXT_CHARS]


def og_image(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("meta", property="og:image")
    return tag.get("content", "") if tag else ""


def matching_links(html: str, base_url: str, include: list[str], exclude: list[str], limit: int) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    inc = [re.compile(p) for p in include]
    exc = [re.compile(p) for p in exclude]
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0]
        if url in out or url.rstrip("/") == base_url.rstrip("/"):
            continue
        if inc and not any(p.search(url) for p in inc):
            continue
        if any(p.search(url) for p in exc):
            continue
        out.append(url)
        if len(out) >= limit:
            break
    return out


# ---------- Playwright ----------

class Browser:
    """One Chromium for the whole run."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(user_agent=USER_AGENT, locale="pt-BR",
                                              timezone_id="America/Sao_Paulo")
        return self

    def __exit__(self, *exc):
        self._ctx.close()
        self._browser.close()
        self._pw.stop()

    def get(self, url: str, timeout_ms: int = 30_000) -> tuple[int, str]:
        """Plain HTTP GET with the browser's identity (passes firewalls that block scripts)."""
        r = self._ctx.request.get(url, timeout=timeout_ms, headers={"Accept": "application/json,text/plain,*/*"})
        return r.status, r.text()

    def html(self, url: str, scroll: int = 0, timeout_ms: int = 45_000) -> str:
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # some sites never go idle; what we have is enough
            for _ in range(scroll):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
            return page.content()
        finally:
            page.close()


def fetch_web_source(src: dict, settings: Settings, browser: Browser, now: datetime) -> list[RawItem]:
    name = src["name"]
    delay = settings.request_delay_seconds
    urls = [fill_url(u, now, settings.window_days) for u in [src["url"], *src.get("pages", [])]]
    follow = src.get("follow_links")
    hint = src.get("default_venue_hint", "")
    extra = src.get("extract_hint", "")
    items: list[RawItem] = []
    article_urls: list[str] = []

    for i, url in enumerate(urls):
        if not allowed_by_robots(url, browser):
            if i == 0:
                raise RobotsBlocked("o robots.txt do site não permite leitura automática desta página")
            log.warning("%s: robots.txt disallows %s, skipping", name, url)
            continue
        try:
            html = browser.html(url, scroll=src.get("scroll", 0))
        except Exception as e:
            if i == 0:
                raise  # the main page failing = the source failed
            log.warning("%s: extra page failed %s (%s)", name, url, e)
            continue
        jsonld = extract_jsonld_events(html)
        if follow:
            article_urls += matching_links(html, url, follow.get("include", []),
                                           follow.get("exclude", []), follow.get("max", 10))
            if jsonld:
                items.append(RawItem(source=name, url=url, jsonld_events=jsonld, venue_hint=hint))
        else:
            items.append(RawItem(source=name, url=url, text=visible_text(html),
                                 jsonld_events=jsonld, venue_hint=hint, extract_hint=extra))
        time.sleep(delay)

    seen = set()
    for url in article_urls[: (follow or {}).get("max", 10)]:
        if url in seen or not allowed_by_robots(url, browser):
            continue
        seen.add(url)
        try:
            html = browser.html(url)
        except Exception as e:
            log.warning("%s: article failed %s (%s)", name, url, e)
            continue
        img = og_image(html)
        items.append(RawItem(source=name, url=url, text=visible_text(html),
                             image_urls=[img] if img else [],
                             jsonld_events=extract_jsonld_events(html), venue_hint=hint,
                             extract_hint=extra))
        time.sleep(delay)
    return items
