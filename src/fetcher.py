"""Collect recent articles from RSS feeds (and best-effort HTML scrapes).

Design notes:
- One failing publisher must never take the digest down, so every source is
  wrapped and reported individually.
- We fetch with `requests` and hand bytes to `feedparser` rather than letting
  feedparser fetch: several AI blogs reject the default urllib User-Agent.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import ICT, FetchDefaults, Source

LOGGER = logging.getLogger(__name__)

#: Query parameters stripped before URL comparison.
_TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "at_")
_TRACKING_PARAMS = {"ref", "source", "fbclid", "gclid", "igshid", "spm", "cmpid"}

#: Two titles above this ratio are treated as the same story.
TITLE_SIMILARITY_THRESHOLD = 0.88

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")


class FeedError(RuntimeError):
    """Raised when a source cannot be turned into articles."""


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
@dataclass
class Article:
    title: str
    url: str
    source_id: str
    source_name: str
    category: str
    published: datetime
    summary: str = ""
    author: str = ""
    #: True when the feed gave us no date and we assumed "now".
    date_estimated: bool = False

    @property
    def domain(self) -> str:
        netloc = urlparse(self.url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc

    @property
    def published_ict(self) -> datetime:
        return self.published.astimezone(ICT)

    @property
    def published_label(self) -> str:
        return self.published_ict.strftime("%d/%m %H:%M") + " ICT"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "category": self.category,
            "published": self.published.isoformat(),
            "summary": self.summary,
            "author": self.author,
        }


@dataclass
class FetchResult:
    """Per-source outcome, surfaced in the digest footer and the logs."""

    source_id: str
    source_name: str
    status: str  # "ok" | "empty" | "error"
    count: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class FetchReport:
    articles: List[Article] = field(default_factory=list)
    results: List[FetchResult] = field(default_factory=list)

    @property
    def failed(self) -> List[FetchResult]:
        return [r for r in self.results if r.status == "error"]

    @property
    def succeeded(self) -> List[FetchResult]:
        return [r for r in self.results if r.status == "ok"]


# --------------------------------------------------------------------------
# text / url helpers
# --------------------------------------------------------------------------
def clean_html(raw: str, max_chars: int = 1500) -> str:
    """Strip markup from feed summaries and collapse whitespace."""
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    text = _WHITESPACE.sub(" ", soup.get_text(separator=" ", strip=True)).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def normalize_url(url: str) -> str:
    """Canonical form used for dedup: no scheme case, no www, no tracking."""
    candidate = (url or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return candidate.lower()

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES) and key.lower() not in _TRACKING_PARAMS
    ]

    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower() or "https", netloc, path, "", urlencode(kept), ""))


def normalize_title(title: str) -> str:
    lowered = (title or "").strip().lower()
    return _WHITESPACE.sub(" ", _NON_ALNUM.sub(" ", lowered)).strip()


#: Human-readable formats seen on scraped index pages ("Jul 24, 2026").
#: These carry no time, so they land on midnight UTC — the 24h window is
#: therefore coarse for scraped sources. Widen it with --hours if that bites.
_HUMAN_DATE_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
)


def _parse_datetime_string(raw: str) -> Optional[datetime]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _HUMAN_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _entry_datetime(entry: Any) -> Optional[datetime]:
    """Best available publication time, always tz-aware UTC."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                # feedparser's *_parsed is already UTC, so timegm — not mktime,
                # which would misread it as local time.
                return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    for key in ("published", "updated", "created", "date", "dc_date"):
        parsed = _parse_datetime_string(str(entry.get(key) or ""))
        if parsed:
            return parsed
    return None


def _entry_link(entry: Any, base_url: str) -> str:
    link = (entry.get("link") or "").strip()
    if not link:
        for candidate in entry.get("links") or []:
            href = (candidate.get("href") or "").strip()
            if href:
                link = href
                break
    if not link:
        link = (entry.get("id") or "").strip()
    if link and not link.lower().startswith(("http://", "https://")):
        link = urljoin(base_url, link)
    return link


def _entry_summary(entry: Any) -> str:
    for key in ("summary", "description", "subtitle"):
        value = entry.get(key)
        if value:
            return clean_html(str(value))
    contents = entry.get("content") or []
    if contents:
        return clean_html(str(contents[0].get("value") or ""))
    return ""


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------
def _http_get(url: str, timeout: int, user_agent: str, session: Optional[requests.Session] = None):
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
        "Accept-Language": "en;q=0.9,vi;q=0.8",
    }
    getter = session.get if session is not None else requests.get
    response = getter(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response


# --------------------------------------------------------------------------
# per-source fetchers
# --------------------------------------------------------------------------
def fetch_rss(
    source: Source,
    defaults: FetchDefaults,
    session: Optional[requests.Session] = None,
) -> List[Article]:
    """Parse one RSS/Atom feed into Articles (no time filtering yet)."""
    response = _http_get(source.url, defaults.request_timeout, defaults.user_agent, session)
    parsed = feedparser.parse(response.content)

    if not parsed.entries:
        if getattr(parsed, "bozo", 0):
            raise FeedError("malformed feed: {}".format(getattr(parsed, "bozo_exception", "unknown")))
        return []

    now = datetime.now(timezone.utc)
    articles: List[Article] = []
    for entry in parsed.entries:
        title = _WHITESPACE.sub(" ", str(entry.get("title") or "").strip())
        link = _entry_link(entry, source.homepage or source.url)
        if not title or not link:
            continue

        published = _entry_datetime(entry)
        articles.append(
            Article(
                title=title,
                url=link,
                source_id=source.id,
                source_name=source.name,
                category=source.category,
                published=published or now,
                summary=_entry_summary(entry),
                author=str(entry.get("author") or "").strip(),
                date_estimated=published is None,
            )
        )
    return articles


def _select_text(node: Any, selector: str) -> str:
    if not selector:
        return ""
    found = node.select_one(selector)
    return _WHITESPACE.sub(" ", found.get_text(" ", strip=True)) if found else ""


def fetch_html(
    source: Source,
    defaults: FetchDefaults,
    config: Optional[Dict[str, Any]] = None,
    session: Optional[requests.Session] = None,
) -> List[Article]:
    """Best-effort scrape for publishers without a usable feed.

    Selectors come from `sources.yaml`. Markup changes silently, so this path
    is expected to occasionally return nothing — the caller treats that as
    "empty", not as a hard failure.
    """
    spec: Dict[str, Any] = dict(config or {})
    url = str(spec.get("url") or source.url)
    item_selector = str(spec.get("item_selector") or source.item_selector)
    if not item_selector:
        raise FeedError("html source is missing `item_selector`")

    title_selector = str(spec.get("title_selector") or source.title_selector)
    link_selector = str(spec.get("link_selector") or source.link_selector or "a")
    summary_selector = str(spec.get("summary_selector") or source.summary_selector)
    date_selector = str(spec.get("date_selector") or source.date_selector)

    response = _http_get(url, defaults.request_timeout, defaults.user_agent, session)
    soup = BeautifulSoup(response.text, "html.parser")

    now = datetime.now(timezone.utc)
    articles: List[Article] = []
    seen: set = set()

    for node in soup.select(item_selector):
        # The card is often the <a> itself (Anthropic), so fall back to the
        # node before searching its descendants.
        anchor = None
        if node.name == "a" and node.get("href"):
            anchor = node
        elif link_selector:
            anchor = node.select_one(link_selector)

        href = (anchor.get("href") or "").strip() if anchor else ""
        if not href:
            continue
        link = urljoin(url, href)
        if link in seen:
            continue

        title = _select_text(node, title_selector) or (
            _WHITESPACE.sub(" ", anchor.get_text(" ", strip=True)) if anchor else ""
        )
        if not title or len(title) < 12:
            continue

        published = None
        if date_selector:
            time_node = node.select_one(date_selector)
            if time_node is not None:
                published = _parse_datetime_string(
                    str(time_node.get("datetime") or time_node.get_text(" ", strip=True))
                )

        seen.add(link)
        articles.append(
            Article(
                title=title,
                url=link,
                source_id=source.id,
                source_name=source.name,
                category=source.category,
                published=published or now,
                summary=clean_html(_select_text(node, summary_selector)),
                date_estimated=published is None,
            )
        )
    return articles


def _fetch_one(
    source: Source,
    defaults: FetchDefaults,
    session: Optional[requests.Session],
) -> Tuple[List[Article], str]:
    """Run the primary strategy, then the configured fallback if it came up dry."""
    primary_error: Optional[BaseException] = None
    articles: List[Article] = []
    strategy = source.type

    try:
        if source.type == "html":
            articles = fetch_html(source, defaults, None, session)
        else:
            articles = fetch_rss(source, defaults, session)
    except Exception as exc:  # noqa: BLE001 - retried via the fallback below
        if not source.fallback:
            raise
        primary_error = exc

    if articles or not source.fallback:
        if primary_error is not None:
            raise primary_error
        return articles, strategy

    LOGGER.info(
        "%s came up empty (%s); trying fallback",
        source.name,
        primary_error or "no entries",
    )

    fallback_type = str(source.fallback.get("type") or "html").lower()
    try:
        if fallback_type == "rss":
            mirror = replace(source, url=str(source.fallback.get("url") or source.url))
            articles = fetch_rss(mirror, defaults, session)
        else:
            articles = fetch_html(source, defaults, source.fallback, session)
    except Exception as exc:  # noqa: BLE001 - report the primary cause, not the fallback's
        raise (primary_error or exc)

    return articles, "{}+{}-fallback".format(strategy, fallback_type)


def _within_window(article: Article, cutoff: datetime, keep_undated: bool) -> bool:
    if article.date_estimated:
        return keep_undated
    return article.published >= cutoff


def fetch_all(
    sources: Sequence[Source],
    defaults: FetchDefaults,
    lookback_hours: Optional[int] = None,
    now: Optional[datetime] = None,
    keep_undated: bool = True,
) -> FetchReport:
    """Fetch every enabled source, isolating failures.

    `keep_undated` controls articles whose feed carried no date (arXiv items,
    some scraped pages). They are kept by default and marked so downstream
    consumers know the timestamp is an estimate.
    """
    hours = lookback_hours if lookback_hours is not None else defaults.lookback_hours
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(hours=hours)

    report = FetchReport()
    with requests.Session() as session:
        for source in sources:
            try:
                raw_articles, strategy = _fetch_one(source, defaults, session)
            except requests.exceptions.RequestException as exc:
                LOGGER.warning("[%s] network error: %s", source.name, exc)
                report.results.append(
                    FetchResult(source.id, source.name, "error", detail=str(exc)[:200])
                )
                continue
            except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
                LOGGER.warning("[%s] fetch failed: %s", source.name, exc)
                report.results.append(
                    FetchResult(source.id, source.name, "error", detail=str(exc)[:200])
                )
                continue

            fresh = [a for a in raw_articles if _within_window(a, cutoff, keep_undated)]
            fresh.sort(key=lambda a: a.published, reverse=True)

            cap = source.max_articles or defaults.max_articles_per_source
            if cap and len(fresh) > cap:
                fresh = fresh[:cap]

            if fresh:
                report.articles.extend(fresh)
                report.results.append(FetchResult(source.id, source.name, "ok", len(fresh)))
                LOGGER.info("[%s] %d article(s) via %s", source.name, len(fresh), strategy)
            else:
                detail = "no articles in the last {}h".format(hours)
                report.results.append(FetchResult(source.id, source.name, "empty", 0, detail))
                LOGGER.info("[%s] %s (%d total in feed)", source.name, detail, len(raw_articles))

    return report


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------
def deduplicate(
    articles: Sequence[Article],
    threshold: float = TITLE_SIMILARITY_THRESHOLD,
) -> List[Article]:
    """Drop repeats by canonical URL, then by fuzzy title match.

    Newest wins: the same story syndicated by two publishers collapses to
    whichever copy was published most recently.
    """
    ordered = sorted(articles, key=lambda a: (a.published, a.title), reverse=True)

    kept: List[Article] = []
    seen_urls: set = set()
    seen_titles: List[str] = []

    for article in ordered:
        url_key = normalize_url(article.url)
        if url_key and url_key in seen_urls:
            LOGGER.debug("Duplicate URL dropped: %s", article.url)
            continue

        title_key = normalize_title(article.title)
        if title_key and any(
            SequenceMatcher(None, title_key, other).ratio() >= threshold for other in seen_titles
        ):
            LOGGER.debug("Duplicate title dropped: %s", article.title)
            continue

        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.append(title_key)
        kept.append(article)

    dropped = len(articles) - len(kept)
    if dropped:
        LOGGER.info("Deduplicated %d article(s)", dropped)
    return kept


def collect(
    sources: Sequence[Source],
    defaults: FetchDefaults,
    lookback_hours: Optional[int] = None,
    limit: Optional[int] = None,
) -> FetchReport:
    """fetch_all + deduplicate + optional global cap. The entry point main uses."""
    report = fetch_all(sources, defaults, lookback_hours)
    report.articles = deduplicate(report.articles)

    if limit and len(report.articles) > limit:
        LOGGER.info("Capping %d article(s) to %d", len(report.articles), limit)
        report.articles = report.articles[:limit]

    return report
