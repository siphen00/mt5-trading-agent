"""
News agent — aggregates macro / geopolitical headlines and the economic calendar,
scores them for market impact, and writes data/news.json for the dashboard.

Design notes
------------
* RSS over HTML scraping. RSS/Atom feeds are published for syndication: they're
  fast, stable, and don't break when a site restyles. HTML scraping of news sites
  breaks constantly and is usually against ToS.

* Feeds are CONFIGURABLE (FEEDS below) and failure-isolated: one dead feed is
  skipped and reported in the output's `errors` list, never fatal. Feed URLs do
  change over time, so this degrades instead of dying.

* ForexFactory calendar ("red folder" events) comes from their public weekly
  JSON export. IMPORTANT: FF rate-limits this to roughly 2 requests per 5
  minutes across ALL formats, and returns an HTML "Request Denied" page instead
  of JSON when exceeded. So it's cached on disk (CALENDAR_CACHE_MINUTES, default
  60) and the response is validated as real JSON before use.

* Stdlib only (urllib + xml.etree) — no new pip dependencies, keeping the
  zero-cost/zero-friction property of the project.

Scoring is deliberately simple and transparent: keyword categories with weights.
It's a relevance filter, NOT a prediction of direction. A high score means "this
is the kind of headline that moves markets", nothing more.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

USER_AGENT = "Mozilla/5.0 (compatible; APEX-Terminal-NewsAgent/1.0)"
REQUEST_TIMEOUT = 15

# --- Feeds ------------------------------------------------------------------
# (name, url, default_category). Edit freely; unreachable feeds are skipped.
FEEDS: List[Tuple[str, str, str]] = [
    ("Al Jazeera",      "https://www.aljazeera.com/xml/rss/all.xml",                   "geopolitical"),
    ("CNBC World",      "https://www.cnbc.com/id/100727362/device/rss/rss.html",       "markets"),
    ("CNBC Economy",    "https://www.cnbc.com/id/20910258/device/rss/rss.html",        "macro"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",          "central_bank"),
    ("BBC World",       "https://feeds.bbci.co.uk/news/world/rss.xml",                 "geopolitical"),
    ("CoinDesk",        "https://www.coindesk.com/arc/outboundfeeds/rss/",             "crypto"),
]

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_CACHE_MINUTES = 60          # FF rate-limits hard; do not lower much

# --- Impact scoring ---------------------------------------------------------
# category -> (weight, [keywords]).  Matched on lowercased title + summary.
KEYWORDS: Dict[str, Tuple[int, List[str]]] = {
    "war": (5, [
        "war", "invasion", "invade", "missile", "airstrike", "air strike", "strike on",
        "attack", "military", "troops", "nuclear", "ceasefire", "escalation", "escalate",
        "conflict", "drone strike", "retaliation", "offensive", "bombing",
    ]),
    "sanctions": (4, ["sanction", "embargo", "export ban", "tariff", "trade war", "blockade"]),
    "central_bank": (5, [
        "federal reserve", "fed ", "fomc", "powell", "interest rate", "rate cut", "rate hike",
        "monetary policy", "ecb", "bank of japan", "boj", "bank of england", "quantitative",
        "basis points", "dot plot", "hawkish", "dovish",
    ]),
    "macro_data": (4, [
        "inflation", "cpi", "ppi", "pce", "gdp", "unemployment", "nonfarm", "non-farm",
        "payrolls", "jobless", "retail sales", "pmi", "consumer confidence", "recession",
    ]),
    "crypto": (4, [
        "bitcoin", "btc", "ethereum", "crypto", "stablecoin", "spot etf", "sec approves",
        "halving", "exchange hack", "binance", "coinbase",
    ]),
    "risk": (3, [
        "crash", "plunge", "selloff", "sell-off", "surge", "soar", "tumble", "default",
        "bankrupt", "bailout", "contagion", "liquidity crisis", "circuit breaker",
    ]),
    "energy": (2, ["oil price", "opec", "crude", "natural gas", "energy crisis", "pipeline"]),
}

HIGH_SCORE = 8
MEDIUM_SCORE = 4


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: Optional[str]      # ISO8601 UTC
    published_ts: Optional[int]   # unix seconds, for sorting
    summary: str
    categories: List[str]
    score: int
    impact: str                   # high | medium | low

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """RSS summaries frequently contain HTML; the dashboard wants plain text."""
    if not text:
        return ""
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()


def score_text(title: str, summary: str = "") -> Tuple[int, List[str]]:
    """
    Score a headline for market relevance. Returns (score, matched_categories).

    Each category contributes its weight AT MOST ONCE, so a headline repeating
    "war" five times doesn't outrank a genuinely multi-factor story.
    """
    blob = f" {title.lower()} {summary.lower()} "
    score = 0
    cats: List[str] = []
    for cat, (weight, words) in KEYWORDS.items():
        if any(w in blob for w in words):
            score += weight
            cats.append(cat)
    return score, cats


def impact_label(score: int) -> str:
    if score >= HIGH_SCORE:
        return "high"
    if score >= MEDIUM_SCORE:
        return "medium"
    return "low"


def _text_of(elem: Optional[ET.Element]) -> str:
    return (elem.text or "").strip() if elem is not None and elem.text else ""


def parse_feed(xml_text: str, source: str, default_category: str = "") -> List[NewsItem]:
    """
    Parse an RSS 2.0 or Atom feed into NewsItems. Handles both because feeds in
    the wild are a mix of the two.
    """
    items: List[NewsItem] = []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return items

    ATOM = "{http://www.w3.org/2005/Atom}"
    # RSS: channel/item ; Atom: feed/entry
    entries = root.findall(".//item") or root.findall(f".//{ATOM}entry")

    for e in entries:
        title = _text_of(e.find("title")) or _text_of(e.find(f"{ATOM}title"))
        if not title:
            continue

        link = _text_of(e.find("link")) or _text_of(e.find(f"{ATOM}link"))
        if not link:  # Atom often puts it in an attribute
            le = e.find(f"{ATOM}link")
            if le is not None:
                link = le.attrib.get("href", "")

        summary = (_text_of(e.find("description"))
                   or _text_of(e.find(f"{ATOM}summary"))
                   or _text_of(e.find(f"{ATOM}content")))
        summary = strip_html(summary)[:400]

        raw_date = (_text_of(e.find("pubDate"))
                    or _text_of(e.find(f"{ATOM}published"))
                    or _text_of(e.find(f"{ATOM}updated")))
        published_iso, published_ts = normalise_date(raw_date)

        score, cats = score_text(title, summary)
        if default_category and default_category not in cats:
            cats.append(default_category)

        items.append(NewsItem(
            title=strip_html(title), url=link, source=source,
            published=published_iso, published_ts=published_ts,
            summary=summary, categories=cats, score=score, impact=impact_label(score),
        ))
    return items


def normalise_date(raw: str) -> Tuple[Optional[str], Optional[int]]:
    """RFC-2822 (RSS) or ISO-8601 (Atom) -> (iso utc string, unix ts)."""
    if not raw:
        return None, None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, None
    if dt is None:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(), int(dt.timestamp())


def parse_calendar(events: List[Dict[str, Any]], now: Optional[datetime] = None,
                   window_hours: int = 48) -> List[Dict[str, Any]]:
    """
    Filter the ForexFactory weekly export down to high/medium impact events
    inside a forward/backward window. These are the "red folder" releases.
    """
    now = now or datetime.now(timezone.utc)
    lo = now - timedelta(hours=6)         # keep just-released prints visible
    hi = now + timedelta(hours=window_hours)
    out = []
    for ev in events:
        impact = str(ev.get("impact", "")).lower()
        if impact not in ("high", "medium"):
            continue
        iso, ts = normalise_date(str(ev.get("date", "")))
        if ts is None:
            continue
        when = datetime.fromtimestamp(ts, tz=timezone.utc)
        if not (lo <= when <= hi):
            continue
        out.append({
            "title": ev.get("title", ""),
            "country": ev.get("country", ""),
            "impact": impact,
            "date": iso,
            "date_ts": ts,
            "forecast": ev.get("forecast", ""),
            "previous": ev.get("previous", ""),
            "upcoming": ts > int(now.timestamp()),
        })
    out.sort(key=lambda x: x["date_ts"])
    return out


def dedupe(items: List[NewsItem]) -> List[NewsItem]:
    """Same story often appears across feeds; keep the highest-scored copy."""
    seen: Dict[str, NewsItem] = {}
    for it in items:
        key = re.sub(r"[^a-z0-9 ]", "", it.title.lower())[:70].strip()
        if not key:
            continue
        if key not in seen or it.score > seen[key].score:
            seen[key] = it
    return list(seen.values())


# ---------------------------------------------------------------------------
# Network / orchestration
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_calendar(cache_path: str, cache_minutes: int = CALENDAR_CACHE_MINUTES) -> Tuple[List[Dict], Optional[str]]:
    """
    Fetch the FF weekly calendar, honouring an on-disk cache.

    FF returns an HTML "Request Denied" page (not an error status) when the
    ~2-per-5-minutes limit is exceeded, so the body is validated as JSON and a
    stale cache is preferred over garbage.
    """
    now = time.time()
    cached: Optional[List[Dict]] = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                blob = json.load(f)
            cached = blob.get("events")
            if now - blob.get("fetched_at", 0) < cache_minutes * 60:
                return cached or [], None
        except (json.JSONDecodeError, OSError, AttributeError):
            cached = None

    try:
        body = http_get(FF_CALENDAR_URL)
        events = json.loads(body)            # HTML denial page fails here
        if not isinstance(events, list):
            raise ValueError("calendar payload was not a list")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"fetched_at": now, "events": events}, f)
        return events, None
    except Exception as e:                    # noqa: BLE001 - any failure -> stale cache
        if cached is not None:
            return cached, f"calendar refresh failed ({e}); using cached copy"
        return [], f"calendar unavailable ({e})"


def collect(max_items: int = 40, max_age_hours: int = 36,
            cache_path: str = "data/.ff_calendar_cache.json") -> Dict[str, Any]:
    """Fetch everything and build the payload the dashboard renders."""
    all_items: List[NewsItem] = []
    errors: List[str] = []

    for name, url, cat in FEEDS:
        try:
            all_items.extend(parse_feed(http_get(url), name, cat))
        except Exception as e:                # noqa: BLE001 - isolate per feed
            errors.append(f"{name}: {e}")

    cutoff = int(time.time()) - max_age_hours * 3600
    fresh = [i for i in all_items if i.published_ts is None or i.published_ts >= cutoff]
    ranked = dedupe(fresh)
    # Most impactful first, then most recent.
    ranked.sort(key=lambda i: (i.score, i.published_ts or 0), reverse=True)
    ranked = [i for i in ranked if i.score > 0][:max_items]

    events, cal_err = fetch_calendar(cache_path)
    if cal_err:
        errors.append(cal_err)

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": [i.to_dict() for i in ranked],
        "calendar": parse_calendar(events),
        "sources": [f[0] for f in FEEDS],
        "errors": errors,
    }


def run_once(output_path: str = "data/news.json") -> Dict[str, Any]:
    payload = collect(cache_path=os.path.join(os.path.dirname(output_path) or ".",
                                              ".ff_calendar_cache.json"))
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    high = sum(1 for i in payload["items"] if i["impact"] == "high")
    print(f"[news] {len(payload['items'])} items ({high} high impact), "
          f"{len(payload['calendar'])} calendar events, {len(payload['errors'])} errors")
    for e in payload["errors"]:
        print(f"[news]   ! {e}")
    return payload


if __name__ == "__main__":
    run_once()
