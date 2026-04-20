"""Add an item to data/actions.json (or data/media.json) from a URL.

Rule (per memory/feedback_manual_add_pipeline.md): manual additions
must go through the SAME extraction pipeline as auto-scraped items —
no guessing on dates, titles, tags, states, or labels.

Usage:
    python add_item.py URL [--agency X] [--type X] [--media]

Without --media, writes to data/actions.json.
With --media, writes to data/media.json (stories).

The script fetches the source page, extracts:
  - title (verbatim from h1 / og:title)
  - date (via _extract_canonical_date chain; falls back to YYYY-MM if
    no day found)
  - body text
  - tags (via generate_tags)
  - state (via get_state)
  - link_label (via derive_link_label)
  - type (via get_action_type)

Then dedups by normalized link and appends.

Dry-run by default; pass --apply to write.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests

# Reuse the scraper's helpers — these are the SAME functions that
# auto-scraped items go through.
from update import (
    fetch_detail_page,
    generate_tags,
    get_state,
    derive_link_label,
    get_action_type,
    normalize_link,
    scrape_page_with_browser,
    HAS_PLAYWRIGHT,
)
from tag_allowlist import filter_tags

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_FILE = os.path.join(SCRIPT_DIR, "data", "actions.json")
MEDIA_FILE = os.path.join(SCRIPT_DIR, "data", "media.json")


def _guess_agency(host: str) -> str:
    """Guess the primary agency from the URL host."""
    h = (host or "").lower().replace("www.", "")
    if "justice.gov" in h: return "DOJ"
    if "oig.hhs.gov" in h: return "HHS-OIG"
    if "hhs.gov" in h: return "HHS"
    if "cms.gov" in h: return "CMS"
    if "whitehouse.gov" in h: return "White House"
    if "gao.gov" in h: return "GAO"
    if "treasury.gov" in h or "fincen.gov" in h: return "Treasury"
    if "macpac.gov" in h: return "MACPAC"
    if "medpac.gov" in h: return "MedPAC"
    if "dea.gov" in h: return "DEA"
    if "fda.gov" in h: return "FDA"
    if any(s in h for s in ["senate.gov", "house.gov", "congress.gov"]):
        return "Congress"
    return ""


def build_item_from_url(url: str, agency_override: str = "",
                         type_override: str = "",
                         is_media: bool = False) -> dict:
    """Fetch URL and build a fully-populated item dict.

    All fields come from the source page via the same helpers the
    scraper uses. Returns the dict — caller decides whether to insert.
    """
    host = urlparse(url).netloc
    agency = agency_override or _guess_agency(host)
    if not agency:
        raise ValueError(
            f"Cannot guess agency for host {host}. Pass --agency explicitly."
        )

    # Use session + fetch_detail_page for extraction
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    })

    print(f"Fetching {url}", file=sys.stderr)
    try:
        body_text, doj_link, canonical_title, canonical_date = \
            fetch_detail_page(session, url)
    except Exception as e:
        print(f"  requests fetch failed: {e}", file=sys.stderr)
        body_text, doj_link, canonical_title, canonical_date = "", None, "", None

    # Fallback to Playwright if requests got empty body (Akamai-blocked sites)
    if not body_text and HAS_PLAYWRIGHT:
        print("  empty body, falling back to Playwright...", file=sys.stderr)
        try:
            soup = scrape_page_with_browser(url)
            if soup:
                main = (soup.find("main") or soup.find("article")
                        or soup.find("div", class_="field-item")
                        or soup.find("div", class_="entry-content")
                        or soup.body)
                if main:
                    for t in main.find_all(["nav", "footer", "aside",
                                            "script", "style"]):
                        t.decompose()
                    related_re = re.compile(
                        r"(?:^|\s)(related-content|related-press|related-stor|"
                        r"views-blockrelated|more-news|more-press|"
                        r"you-may-also-like|further-reading|recommend)", re.I)
                    for t in main.find_all(class_=related_re):
                        t.decompose()
                    body_text = re.sub(r"\s+", " ",
                                        main.get_text(" ", strip=True))[:12000]
                # og:title / h1 fallback
                if not canonical_title:
                    og = soup.find("meta", attrs={"property": "og:title"})
                    if og and og.get("content"):
                        canonical_title = og["content"].strip()
                    elif soup.find("h1"):
                        canonical_title = soup.find("h1").get_text(strip=True)
        except Exception as e:
            print(f"  Playwright fetch failed: {e}", file=sys.stderr)

    if not canonical_title:
        raise ValueError(
            "Could not extract title from source. Manual intervention needed."
        )

    # Date: use canonical if available, else YYYY-MM fallback
    if canonical_date:
        date_str = canonical_date
        # If canonical_date doesn't have a day, it's already YYYY-MM
    else:
        # Try to extract month/year from body as last resort
        m = re.search(
            r"\b(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+(\d{4})\b",
            body_text)
        if m:
            month_name, year = m.group(1), m.group(2)
            month_num = {"January": "01", "February": "02", "March": "03",
                         "April": "04", "May": "05", "June": "06",
                         "July": "07", "August": "08", "September": "09",
                         "October": "10", "November": "11",
                         "December": "12"}[month_name]
            date_str = f"{year}-{month_num}"
            print(f"  Using YYYY-MM format (no day in source): {date_str}",
                  file=sys.stderr)
        else:
            raise ValueError(
                "No date found in source. Manual date entry required — "
                "inspect the source and pass --date explicitly."
            )

    # Tags: use the strict pipeline
    tags = filter_tags(generate_tags(canonical_title, body_text))

    # State
    state = get_state(body_text, title=canonical_title, link=url)

    # Link label
    link_label = derive_link_label(agency, url, is_media=is_media)

    # Type
    if type_override:
        action_type = type_override
    else:
        action_type = get_action_type(canonical_title, body_text[:500],
                                       agency=agency, link=url)

    # Build deterministic id from date + normalized URL slug
    slug = re.sub(r"[^a-z0-9-]+", "-",
                   urlparse(url).path.rstrip("/").split("/")[-1].lower())[:60]
    prefix = "media" if is_media else agency.lower().replace(" ", "-")
    item_id = f"{prefix}-{date_str}-{slug}"

    return {
        "id": item_id,
        "date": date_str,
        "agency": agency,
        "type": action_type,
        "title": canonical_title,
        "amount": "",
        "amount_numeric": 0,
        "officials": [],
        "link": url,
        "link_label": link_label,
        "social_posts": [],
        "tags": tags,
        "entities": [],
        "state": state,
        "source_type": "official",
        "auto_fetched": False,
        "related_agencies": [],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="URL of the source page")
    ap.add_argument("--agency", default="", help="Override agency guess")
    ap.add_argument("--type", default="", dest="type_override",
                    help="Override type classification")
    ap.add_argument("--date", default="",
                    help="Override date (YYYY-MM-DD or YYYY-MM). Use only "
                         "when source truly lacks a date.")
    ap.add_argument("--media", action="store_true",
                    help="Add to data/media.json instead of actions.json")
    ap.add_argument("--apply", action="store_true",
                    help="Write change. Default is dry-run (preview only).")
    args = ap.parse_args()

    try:
        item = build_item_from_url(
            args.url,
            agency_override=args.agency,
            type_override=args.type_override,
            is_media=args.media,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.date:
        item["date"] = args.date

    print(json.dumps(item, indent=2, ensure_ascii=False))

    if not args.apply:
        print("\n[DRY-RUN — rerun with --apply to write]", file=sys.stderr)
        return

    # Apply
    path = MEDIA_FILE if args.media else ACTIONS_FILE
    d = json.load(open(path, encoding="utf-8"))
    items_key = "stories" if args.media else "actions"
    existing_links = {normalize_link(x.get("link", ""))
                       for x in d.get(items_key, [])}
    if normalize_link(item["link"]) in existing_links:
        print("\nERROR: URL already exists in data file — skipping.",
              file=sys.stderr)
        sys.exit(2)

    d[items_key].append(item)
    d["metadata"]["last_updated"] = datetime.now().isoformat()
    json.dump(d, open(path, "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    print(f"\nAdded to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
