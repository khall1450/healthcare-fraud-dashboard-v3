#!/usr/bin/env python3
"""Fetch investigative healthcare-fraud journalism via Google News RSS queries.

## Why Google News instead of per-outlet RSS

Per-outlet RSS feeds are shallow (20-30 most recent items), frequently break
when outlets rename endpoints, and don't cover paywalled sources well. Google
News RSS search solves all three: it aggregates across thousands of outlets,
supports freshness filters (`when:7d`), and exposes a uniform query surface.

## Signal-to-noise strategy

The trade-off with Google News is volume — a broad query returns hundreds
of results, most of which are law firm client alerts, court-document
aggregators, local TV station syndications, wire-service duplicates, or
analyst / trade-press think pieces. The media tab is meant to be a curated
list of the best investigative journalism, not a comprehensive fraud feed.

To maximize signal, this scraper applies four stacked filters:

  1. Hard outlet whitelist. Only items whose canonical URL resolves to
     one of a tight list of investigative-journalism outlets are kept.
     No law firms, no local TV, no trade publications, no aggregators.

  2. URL pattern blacklist. Some whitelisted outlets host category pages,
     author bios, and podcast indexes alongside articles. We drop URLs
     matching ``/topics/``, ``/authors/``, ``/category/``, ``/podcasts/``,
     ``/staff/``, etc.

  3. Cross-outlet title dedup. Wire-service stories that hit multiple
     outlets are normalized by title and only the top-tier version is
     kept.

  4. No regex auto-promote. Every surviving candidate goes straight into
     ``needs_review_media.json`` for Claude Haiku relevance classification
     (``audit_new_items.py ai-review-media``). The AI is the editorial
     judge; the scraper's job is just to surface high-quality candidates.

## Backfill vs daily

The same code path works for both. A daily run uses ``when:3d`` in each
query URL; a backfill uses ``when:365d`` or similar. Google News caps
results at ~100 per query regardless of window, so very large backfills
would need grid-search by month — currently not implemented because
Phase 3 (backfill) uses LLM-seeded curation instead.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse, quote, parse_qs

import feedparser
import requests
from bs4 import BeautifulSoup

from tag_allowlist import auto_tags as _auto_tags, filter_tags as _filter_tags

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_FILE = os.path.join(SCRIPT_DIR, "data", "media.json")
REVIEW_FILE = os.path.join(SCRIPT_DIR, "data", "needs_review_media.json")

# ---------------------------------------------------------------------------
# Google News query list
# ---------------------------------------------------------------------------
# Queries are intentionally narrow to prioritize signal. Broad queries like
# ``"medicare fraud"`` return hundreds of routine court filings and press
# release reposts; the extra word ``investigation`` or ``exposé`` is what
# separates actual journalism from aggregator noise.
#
# The ``when:N`` placeholder is templated at runtime based on daily vs
# backfill mode.

GOOGLE_NEWS_QUERIES = [
    # Program-specific fraud investigations
    '"medicare fraud" investigation',
    '"medicaid fraud" investigation',
    '"healthcare fraud" investigation',
    # Specific fraud categories that are historically well-covered
    'hospice fraud investigation',
    '"nursing home" fraud investigation',
    '"medicare advantage" overbilling investigation',
    '"genetic testing" fraud investigation',
    # Legal frameworks journalists use as story pegs
    '"false claims act" healthcare investigation',
    '"qui tam" healthcare investigation',
    '"whistleblower" healthcare fraud',
    # Site-restricted (investigative outlets that frequently break HC fraud stories)
    'site:propublica.org healthcare fraud',
    'site:kffhealthnews.org fraud investigation',
    'site:statnews.com fraud investigation',
]


def build_google_news_url(query: str, days: int) -> str:
    """Return a Google News RSS search URL for the given query, constrained
    to the last `days` days of freshness."""
    q = f"{query} when:{days}d"
    return f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en"


# ---------------------------------------------------------------------------
# Outlet whitelist
# ---------------------------------------------------------------------------
# Only items whose canonical URL's host ends in one of these suffixes are
# accepted. Local TV stations, law firm sites, trade-press aggregators, and
# foreign outlets are intentionally excluded — they generate too much noise
# for a curated investigative-journalism tab.
#
# To add an outlet: append its primary domain (no protocol, no www.). Domain
# matching is right-anchored so subdomains like ``news.cbsnews.com`` still
# match ``cbsnews.com``.

OUTLET_WHITELIST = {
    # Tier 1 — investigative powerhouses
    "propublica.org",
    "kffhealthnews.org",
    "statnews.com",
    # Tier 2 — national outlets with strong investigative desks
    "nytimes.com",
    "wsj.com",
    "washingtonpost.com",
    "cbsnews.com",
    "npr.org",
    "bloomberg.com",
    "reuters.com",
    "theatlantic.com",
    "newyorker.com",
    # Tier 3 — major regional outlets known for investigative work
    "latimes.com",
    "chicagotribune.com",
    "bostonglobe.com",
    "miamiherald.com",
    "tampabay.com",
    "sfchronicle.com",
}


def is_whitelisted_outlet(url: str) -> bool:
    """True if the URL's host matches an entry in OUTLET_WHITELIST."""
    if not url:
        return False
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    host = host.removeprefix("www.")
    for allowed in OUTLET_WHITELIST:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


# ---------------------------------------------------------------------------
# URL pattern blacklist
# ---------------------------------------------------------------------------
# Even within whitelisted outlets, these URL patterns are category index
# pages, author bios, podcast episode listings, newsletter archives, etc.
# Drop them before any further processing.

URL_BLACKLIST_PATTERNS = [
    r"/topics?/",
    r"/tag/",
    r"/category/",
    r"/author(s)?/",
    r"/staff/",
    r"/people/",
    r"/contributor(s)?/",
    r"/podcasts?/",
    r"/newsletters?/",
    r"/feeds?/",
    r"/rss/",
    r"/search\?",
    r"/about/",
    r"/jobs?/",
    r"/events?/",
    r"/subscribe/",
    r"/donate/",
    r"/corrections/",
]
_URL_BLACKLIST_RE = re.compile("|".join(URL_BLACKLIST_PATTERNS), re.IGNORECASE)


def is_blacklisted_url(url: str) -> bool:
    return bool(_URL_BLACKLIST_RE.search(url or ""))


# ---------------------------------------------------------------------------
# State map (for geography tagging)
# ---------------------------------------------------------------------------
STATE_MAP = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR',
    'California': 'CA', 'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE',
    'Florida': 'FL', 'Georgia': 'GA', 'Hawaii': 'HI', 'Idaho': 'ID',
    'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS',
    'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS',
    'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV',
    'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY',
    'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK',
    'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT',
    'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV',
    'Wisconsin': 'WI', 'Wyoming': 'WY',
}


def get_state(text):
    for name, abbr in STATE_MAP.items():
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return abbr
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
silent = False


def log(msg):
    if not silent:
        print(f"  {msg}", file=sys.stderr)


def create_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def clean_html(text):
    if not text:
        return ""
    soup = BeautifulSoup(text, "lxml")
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def parse_date(date_str):
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
    ]:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    try:
        from dateutil import parser as du_parser
        return du_parser.parse(date_str).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def make_id(date_str, link):
    h = abs(int(hashlib.md5(link.encode()).hexdigest()[:8], 16))
    return f"media-{date_str}-{h}"


def normalize_title(title: str) -> str:
    """Normalize a title for cross-outlet dedup: lowercase, drop punctuation,
    collapse whitespace, strip any trailing ` - Outlet Name` rubrics Google
    News sometimes appends."""
    if not title:
        return ""
    # Drop trailing " - Publication" that Google News appends
    title = re.sub(r"\s+[-–—]\s+[^-–—]+$", "", title)
    # Lowercase + strip non-alphanumeric
    norm = re.sub(r"[^a-z0-9 ]", "", title.lower())
    return re.sub(r"\s+", " ", norm).strip()


def outlet_tier(url: str) -> int:
    """Return an outlet tier (lower = better) for dedup preference.

    When the same story appears from multiple outlets (via wire service or
    parallel reporting), we keep the one from the highest-tier outlet.
    """
    if not url:
        return 99
    host = urlparse(url).netloc.lower().removeprefix("www.")
    TIER_1 = {"propublica.org", "kffhealthnews.org", "statnews.com"}
    TIER_2 = {"nytimes.com", "wsj.com", "washingtonpost.com", "newyorker.com",
              "theatlantic.com"}
    TIER_3 = {"cbsnews.com", "npr.org", "bloomberg.com", "reuters.com"}
    for t, outlets in enumerate([TIER_1, TIER_2, TIER_3], start=1):
        for outlet in outlets:
            if host == outlet or host.endswith("." + outlet):
                return t
    return 4


# ---------------------------------------------------------------------------
# Google News URL resolution
# ---------------------------------------------------------------------------
# Google News RSS returns items with ``link`` values like
#   https://news.google.com/rss/articles/CAIiE...?oc=5
# We need to resolve these to the canonical outlet URL for:
#   - whitelist check
#   - dedup
#   - storing the real link in the dashboard
#
# Two resolution strategies:
#   (a) requests with follow-redirects — works ~90% of the time, ~200ms each
#   (b) Playwright goto — handles JS-based redirects, ~2s each
# We try (a) first and fall back to (b).

def resolve_google_news_url_requests(session, gnews_url: str) -> str | None:
    """Try to resolve a Google News redirect URL using plain requests."""
    if not gnews_url or "news.google.com" not in gnews_url:
        return gnews_url
    try:
        resp = session.get(gnews_url, timeout=10, allow_redirects=True)
        final = resp.url
        if final and "news.google.com" not in final:
            return final
        # Sometimes Google returns HTML with a meta refresh or JS redirect
        if "<meta http-equiv=\"refresh\"" in resp.text[:2000]:
            m = re.search(r'url=([^"\'>\s]+)', resp.text[:2000], re.IGNORECASE)
            if m:
                return m.group(1)
        return None
    except Exception:
        return None


def resolve_google_news_url_playwright(gnews_url: str) -> str | None:
    """Fall back to Playwright for JS-based Google News redirects."""
    browser = _get_browser()
    if not browser:
        return None
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    page = context.new_page()
    try:
        page.goto(gnews_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        final = page.url
        if final and "news.google.com" not in final:
            return final
        return None
    except Exception:
        return None
    finally:
        context.close()


def resolve_google_news_url(session, gnews_url: str) -> str | None:
    """Resolve a Google News redirect URL to its canonical outlet URL."""
    if not gnews_url or "news.google.com" not in gnews_url:
        return gnews_url
    resolved = resolve_google_news_url_requests(session, gnews_url)
    if resolved and "news.google.com" not in resolved:
        return resolved
    if HAS_PLAYWRIGHT:
        resolved = resolve_google_news_url_playwright(gnews_url)
        if resolved and "news.google.com" not in resolved:
            return resolved
    return None


# ---------------------------------------------------------------------------
# Playwright (lazy-init, shared)
# ---------------------------------------------------------------------------
_pw_instance = None
_browser = None


def _get_browser():
    global _pw_instance, _browser
    if not HAS_PLAYWRIGHT:
        return None
    if _browser is None:
        _pw_instance = sync_playwright().start()
        _browser = _pw_instance.chromium.launch(headless=True)
        log("Started headless browser for Playwright fallback")
    return _browser


def _close_browser():
    global _pw_instance, _browser
    if _browser:
        _browser.close()
        _browser = None
    if _pw_instance:
        _pw_instance.stop()
        _pw_instance = None


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global silent, HAS_PLAYWRIGHT

    parser = argparse.ArgumentParser(description="Fetch healthcare-fraud media stories via Google News")
    parser.add_argument("-s", "--silent", action="store_true")
    parser.add_argument("--no-browser", action="store_true",
                        help="Disable Playwright (URL resolution falls back to requests-only).")
    parser.add_argument("--backfill-from", metavar="YYYY-MM-DD",
                        help="Backfill mode. Widens the Google News freshness "
                             "window to cover the span from this date to today.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the full pipeline but do NOT write to "
                             "needs_review_media.json. Print what would be added.")
    args = parser.parse_args()

    silent = args.silent
    if args.no_browser:
        HAS_PLAYWRIGHT = False

    log("=== Media Investigations Scraper (Google News) ===")

    media_data = load_json(MEDIA_FILE, {"metadata": {"last_updated": "", "version": "1.0"}, "stories": []})
    review_data = load_json(REVIEW_FILE, {"items": [], "rejected_links": []})

    # Compute the `when:N` window
    today = datetime.now().date()
    if args.backfill_from:
        try:
            start = datetime.strptime(args.backfill_from, "%Y-%m-%d").date()
            days = max(1, (today - start).days)
        except ValueError:
            log(f"invalid --backfill-from value {args.backfill_from!r}, defaulting to 7")
            days = 7
        last_run_date = args.backfill_from
        log(f"BACKFILL MODE: window = last {days} days (from {last_run_date})")
    else:
        # Daily run — look back 3 days to catch items that may have been
        # scraped after the previous run cutoff
        days = 3
        last_run_raw = media_data.get("metadata", {}).get("last_updated", "")
        last_run_date = last_run_raw[:10] if last_run_raw else (today.isoformat())
        log(f"DAILY MODE: window = last {days} days (last_updated {last_run_date})")

    # Build dedup set from media.json + needs_review_media.json (items and
    # rejected_links). The same canonical URL or normalized title should
    # never pass through twice.
    existing_links = set()
    existing_titles = set()
    for s in media_data.get("stories", []):
        if s.get("link"):
            existing_links.add(s["link"])
        t = normalize_title(s.get("title", ""))
        if t:
            existing_titles.add(t)
    for pending in review_data.get("items", []):
        if pending.get("link"):
            existing_links.add(pending["link"])
        t = normalize_title(pending.get("title", ""))
        if t:
            existing_titles.add(t)
    for rejected in review_data.get("rejected_links", []) or []:
        if rejected:
            existing_links.add(rejected)

    session = create_session()

    # Candidate accumulator keyed by normalized title; used for cross-outlet
    # dedup so we can keep the highest-tier outlet for each unique story.
    candidates_by_title: dict[str, dict] = {}
    dropped_no_whitelist = 0
    dropped_blacklist = 0
    dropped_resolve_fail = 0
    dropped_existing = 0

    for query in GOOGLE_NEWS_QUERIES:
        url = build_google_news_url(query, days)
        log(f"Query: {query} [{days}d]")
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            log(f"  ERROR: {e}")
            continue

        if not parsed.entries:
            log(f"  0 entries")
            continue

        log(f"  {len(parsed.entries)} raw entries")

        for entry in parsed.entries:
            title = entry.get("title", "").strip()
            if not title:
                continue

            # Clean any HTML the feed wraps
            if "<" in title:
                title = clean_html(title)
            title = re.sub(r"\s+", " ", title).strip()

            # Google News often appends " - Outlet Name"; normalize away
            # for display but keep for dedup lookup
            display_title = re.sub(r"\s+[-–—]\s+[^-–—]+$", "", title)
            norm = normalize_title(display_title)
            if not norm:
                continue

            # Dedup against existing data
            if norm in existing_titles:
                dropped_existing += 1
                continue

            gnews_link = entry.get("link", "")
            if not gnews_link:
                continue

            # Resolve the Google News redirect to the real canonical URL
            resolved = resolve_google_news_url(session, gnews_link)
            if not resolved or "news.google.com" in resolved:
                dropped_resolve_fail += 1
                continue

            # Dedup against existing URLs after resolution
            if resolved in existing_links:
                dropped_existing += 1
                continue

            # Outlet whitelist
            if not is_whitelisted_outlet(resolved):
                dropped_no_whitelist += 1
                continue

            # URL pattern blacklist
            if is_blacklisted_url(resolved):
                dropped_blacklist += 1
                continue

            # Date
            date_str = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    date_str = datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d")
                except Exception:
                    pass
            if not date_str:
                date_str = parse_date(entry.get("published", ""))
            if date_str < "2025-01-01":
                continue

            desc_raw = entry.get("summary", "") or entry.get("description", "")
            desc_clean = clean_html(desc_raw)
            search_text = f"{display_title} {desc_clean}"
            tags = _filter_tags(_auto_tags(search_text))
            state = get_state(search_text) or ""

            # Derive a friendly outlet label from the host
            host = urlparse(resolved).netloc.lower().removeprefix("www.")
            # Map common hosts to polished labels
            OUTLET_LABELS = {
                "propublica.org": "ProPublica",
                "kffhealthnews.org": "KFF Health News",
                "statnews.com": "STAT News",
                "nytimes.com": "The New York Times",
                "wsj.com": "Wall Street Journal",
                "washingtonpost.com": "Washington Post",
                "cbsnews.com": "CBS News",
                "npr.org": "NPR",
                "bloomberg.com": "Bloomberg",
                "reuters.com": "Reuters",
                "theatlantic.com": "The Atlantic",
                "newyorker.com": "The New Yorker",
                "latimes.com": "Los Angeles Times",
                "chicagotribune.com": "Chicago Tribune",
                "bostonglobe.com": "Boston Globe",
                "miamiherald.com": "Miami Herald",
                "tampabay.com": "Tampa Bay Times",
                "sfchronicle.com": "San Francisco Chronicle",
            }
            outlet_label = OUTLET_LABELS.get(host, host)

            story = {
                "id": make_id(date_str, resolved),
                "date": date_str,
                "agency": "Media",
                "type": "Investigative Report",
                "title": display_title,
                "amount": "",
                "amount_numeric": 0,
                "officials": [],
                "link": resolved,
                "link_label": f"{outlet_label} Report",
                "social_posts": [],
                "tags": tags,
                "state": state,
                "source_type": "news",
                "auto_fetched": True,
                "entities": [],
            }

            # Cross-outlet dedup: if we already have this normalized title
            # from another outlet, keep whichever has the lower tier
            prev = candidates_by_title.get(norm)
            if prev is None:
                candidates_by_title[norm] = story
            else:
                # Keep the better-tier outlet
                if outlet_tier(resolved) < outlet_tier(prev.get("link", "")):
                    candidates_by_title[norm] = story

    _close_browser()

    new_stories = list(candidates_by_title.values())
    new_stories.sort(key=lambda s: s.get("date", ""), reverse=True)

    log("")
    log(f"Candidate stats:")
    log(f"  dropped (not in whitelist):    {dropped_no_whitelist}")
    log(f"  dropped (URL pattern match):   {dropped_blacklist}")
    log(f"  dropped (redirect unresolved): {dropped_resolve_fail}")
    log(f"  dropped (already seen):        {dropped_existing}")
    log(f"  surviving candidates:          {len(new_stories)}")

    if args.dry_run:
        log(f"\n=== DRY RUN: would add {len(new_stories)} new stories ===")
        for s in new_stories:
            log(f"  [{s['date']}] [{urlparse(s['link']).netloc}] {s['title'][:80]}")
        return len(new_stories)

    if new_stories:
        now_iso = datetime.now().isoformat()
        for s in new_stories:
            s["flagged_at"] = now_iso
            s["flag_reason"] = "scraped from Google News, awaiting AI review"
        review_data["items"] = (review_data.get("items") or []) + new_stories
        save_json(REVIEW_FILE, review_data)

        media_data["metadata"]["last_updated"] = datetime.now().isoformat()
        save_json(MEDIA_FILE, media_data)

        log(f"\n=== Added {len(new_stories)} stories to needs_review_media.json ===")
        for s in new_stories:
            log(f"  [{s['date']}] {s['title'][:80]}")
    else:
        media_data["metadata"]["last_updated"] = datetime.now().isoformat()
        save_json(MEDIA_FILE, media_data)
        log("\n=== No new media stories found ===")

    return len(new_stories)


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
