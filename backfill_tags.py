"""Tag backfill: re-extract tags for historical items with current
(improved) regex + boilerplate stripping + MFCU signal.

For each item in actions.json:
  1. Re-fetch body via Playwright (if DOJ) or requests (if OIG/CMS/etc.)
  2. Apply generate_tags() with current rules
  3. Diff against stored tags
  4. Report adds/removes per item

Dry-run by default; --apply writes with --date-cutoff protection.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import requests
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import (
    fetch_detail_page,
    scrape_page_with_browser,
    generate_tags,
    HAS_PLAYWRIGHT,
)
from tag_allowlist import filter_tags

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_FILE = os.path.join(SCRIPT_DIR, "data", "actions.json")

# MFCU → Medicaid rule DISABLED (2026-04-21). MFCUs frequently
# investigate Medicare-only cases as joint partners, so their
# presence is not a reliable Medicaid signal. See
# memory/project_mfcu_implies_medicaid.md for rationale.


def fetch_body(url, session):
    """Fetch body via requests first, Playwright fallback for DOJ."""
    url_l = url.lower()
    if (url_l.endswith('.pdf') or url_l.endswith('/dl') or
            '/media/' in url_l or '/download' in url_l):
        return ""
    # Try requests first
    try:
        body, _, _, _ = fetch_detail_page(session, url)
        if body and len(body) > 200:
            return body
    except Exception:
        pass
    # Playwright fallback for bot-blocked sites (DOJ, etc.)
    if HAS_PLAYWRIGHT:
        try:
            soup = scrape_page_with_browser(url)
            if not soup:
                return ""
            main = (soup.find("main") or soup.find("article") or soup.body)
            if not main:
                return ""
            for t in main.find_all(["nav", "footer", "aside", "script", "style"]):
                t.decompose()
            return re.sub(r"\s+", " ", main.get_text(" ", strip=True))[:15000]
        except Exception:
            pass
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Write changes. Default: dry-run.")
    ap.add_argument("--date-cutoff", default="",
                    help="Skip items with date >= this YYYY-MM-DD "
                         "(to protect recently-manually-fixed items)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process N items for testing")
    args = ap.parse_args()

    if not HAS_PLAYWRIGHT:
        print("Playwright required for DOJ items", file=sys.stderr)

    d = json.load(open(ACTIONS_FILE, encoding="utf-8"))
    items = d["actions"]
    if args.limit:
        items = items[:args.limit]
    print(f"Processing {len(items)} items "
          f"(apply={args.apply}, date_cutoff={args.date_cutoff or 'none'})")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    })

    diffs = []     # (item, old, new, added, removed)
    unchanged = 0
    fetch_failures = 0
    protected_by_cutoff = 0

    for i, x in enumerate(items, 1):
        if i % 25 == 0 or i == 1:
            print(f"... [{i}/{len(items)}] processing", flush=True)

        url = x.get("link", "")
        if not url:
            continue

        # Date cutoff: skip entirely if item is recent
        if args.date_cutoff and (x.get("date", "") or "") >= args.date_cutoff:
            protected_by_cutoff += 1
            continue

        body = fetch_body(url, session)
        if not body:
            fetch_failures += 1
            continue

        # Extract fresh tags
        fresh = filter_tags(generate_tags(x.get("title", "") or "", body))

        old = list(x.get("tags") or [])
        old_set = set(old)
        fresh_set = set(fresh)

        added = fresh_set - old_set
        removed = old_set - fresh_set

        if added or removed:
            diffs.append((x, old, fresh, added, removed))
            print(f"[{i}] CHANGE: {x.get('title','')[:60]}")
            if added:
                print(f"    +{sorted(added)}")
            if removed:
                print(f"    -{sorted(removed)}")
        else:
            unchanged += 1

        if i % 10 == 0:
            time.sleep(0.3)

    print(f"\n=== Summary ===")
    print(f"Processed:            {len(items)}")
    print(f"Unchanged:            {unchanged}")
    print(f"Would change:         {len(diffs)}")
    print(f"Fetch failures:       {fetch_failures}")
    print(f"Protected by cutoff:  {protected_by_cutoff}")

    # Cache diffs to JSON for apply step
    cache_path = os.path.join(SCRIPT_DIR, "backfill_tags_diff.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump([
            {"id": x["id"], "old": old, "new": new,
             "added": sorted(added), "removed": sorted(removed)}
            for x, old, new, added, removed in diffs
        ], f, indent=2, ensure_ascii=False)
    print(f"\nDiffs cached to {cache_path}")

    if args.apply:
        for x, _, new, _, _ in diffs:
            x["tags"] = new
        d["metadata"]["last_updated"] = datetime.now().isoformat()
        with open(ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        print(f"\nWrote {ACTIONS_FILE}: {len(diffs)} tag corrections")
    else:
        print("\n[DRY-RUN — rerun with --apply to write]")


if __name__ == "__main__":
    main()
