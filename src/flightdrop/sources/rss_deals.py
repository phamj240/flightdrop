"""Human-curated deal feeds (TheFlightDeal, r/flightdeals, ...).

Only titles and links are used, never post bodies; every alert links back to the post.
These feeds rarely mention a given home airport, so expect a trickle rather than a stream.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from ..airports import find_by_city, lookup
from ..config import Config
from ..models import Cabin, Deal
from .common import USER_AGENT, SourceError

log = logging.getLogger("flightdrop.rss")

_PRICE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
_SKIP = re.compile(r"\b(expired|first[- ]class|premium)\b", re.I)
_BUSINESS = re.compile(r"\b(business|biz)\b", re.I)
# Separator between origin and destination: " to ", or a dash with spaces around it.
_SEP = re.compile(r"\s(?:to|[–—-])\s", re.I)
_CITY = r"([A-Z][^,.$\[\]()\d€£]*?)"
_DEST_END = r"(?=\s*(?:,|\.|\$|\(|\d|€|£|\bfor\b|\bfrom\b|\bround\b|\bnon-?stop\b|$))"
_DEST = re.compile(_CITY + _DEST_END)
_TRAILING = re.compile(r"\s+(round ?trip|rt|non-?stop)$", re.I)


@dataclass(frozen=True)
class ParsedTitle:
    origin: str
    destination: str | None
    price_usd: float | None
    cabin: Cabin


def _keyword_origins(cfg: Config) -> list[tuple[re.Pattern[str], str]]:
    """Map each configured keyword to the home airport it stands for."""
    out = []
    for kw in cfg.rss.keywords:
        origin = None
        for iata in cfg.home_airports:
            ap = lookup(iata)
            city = (ap.city if ap else "").lower()
            if kw.upper() == iata or (len(kw) > 3 and kw.lower() in city):
                origin = iata
                break
        if origin is None:
            log.warning("rss keyword %r matches none of the home airports; ignoring it", kw)
            continue
        flags = 0 if (len(kw) == 3 and kw.isupper()) else re.I
        out.append((re.compile(rf"\b{re.escape(kw)}\b", flags), origin))
    return out


def _guess_destination(right: str) -> str | None:
    """Airport code for the city named at the start of ``right``, or None if unsure.

    If a city has several airports they must all be in one country, so the guess is at least
    country-accurate (enough for destination filters). Ambiguous names return None.
    """
    m = _DEST.match(right.strip())
    if not m:
        return None
    city = _TRAILING.sub("", m.group(1)).strip()
    candidates = find_by_city(city)
    if not candidates or len({a.country for a in candidates}) != 1:
        return None
    return candidates[0].iata


def parse_title(title: str, keyword_origins: list[tuple[re.Pattern[str], str]]) -> ParsedTitle | None:
    """Extract origin/destination/price/cabin, or None if this isn't a deal from a home airport."""
    title = html.unescape(title).strip()
    if _SKIP.search(title):
        return None
    sep = _SEP.search(title)
    left, right = (title[: sep.start()], title[sep.end():]) if sep else (title, "")
    origin = next((o for pat, o in keyword_origins if pat.search(left)), None)
    if origin is None:  # no keyword, or the keyword is only on the destination side
        return None
    price = _PRICE.search(title)
    return ParsedTitle(
        origin=origin,
        destination=_guess_destination(right) if right else None,
        price_usd=float(price.group(1).replace(",", "")) if price else None,
        cabin="business" if _BUSINESS.search(title) else "economy",
    )


def _published(entry: feedparser.FeedParserDict, default: datetime) -> datetime:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else default


def parse_feed(
    name: str, content: bytes | str, cfg: Config, now: datetime
) -> list[Deal]:
    feed = feedparser.parse(content)
    keyword_origins = _keyword_origins(cfg)
    oldest = now - timedelta(hours=cfg.rss.max_age_hours)
    deals = []
    for entry in feed.entries:
        title, link = entry.get("title", ""), entry.get("link", "")
        if not title or not link:
            continue
        if not link.lower().startswith(("http://", "https://")):   # feed content is untrusted
            continue
        found = _published(entry, now)
        if found < oldest:
            continue
        parsed = parse_title(title, keyword_origins)
        if parsed is None:
            continue
        deals.append(
            Deal(
                origin=parsed.origin,
                destination=parsed.destination,
                cabin=parsed.cabin,
                price_usd=parsed.price_usd,
                source=name,
                found_at=found,
                source_url=link,
                title=html.unescape(title).strip(),
                curated=True,
            )
        )
    return deals


def fetch(cfg: Config, now: datetime, session: requests.Session | None = None) -> list[Deal]:
    """Fetch every configured feed. A feed that fails is logged and skipped."""
    if not cfg.rss.enabled or not cfg.rss.feeds:
        return []
    session = session or requests.Session()
    deals: list[Deal] = []
    failed = 0
    for name, url in cfg.rss.feeds.items():
        try:
            resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            if not resp.ok:
                raise SourceError(f"HTTP {resp.status_code}")
            found = parse_feed(name, resp.content, cfg, now)
        except (requests.RequestException, SourceError) as e:
            failed += 1
            log.warning("feed %s failed: %s", name, e)
            continue
        log.info("feed %s: %d matching post(s)", name, len(found))
        deals.extend(found)
    if failed == len(cfg.rss.feeds):
        raise SourceError("every deal feed failed")
    return deals
