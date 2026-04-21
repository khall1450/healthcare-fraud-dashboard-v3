"""Re-check the items where we removed HHS-OIG during the investigator
backfill. The regex has since been expanded (added 'praised the work',
'work of the', and 'Health and Human Services Office of Inspector
General' variants). Any item that now matches should have HHS-OIG
restored.

Strategy:
  1. Parse backfill_dryrun.log for ra changes that REMOVED HHS-OIG
  2. Re-fetch body for each matched item
  3. Apply the (new) extract_investigator_agencies
  4. If HHS-OIG matches now, restore it to related_agencies

Dry-run by default; --apply writes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import (
    extract_investigator_agencies,
    scrape_page_with_browser,
    HAS_PLAYWRIGHT,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_FILE = os.path.join(SCRIPT_DIR, "data", "actions.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "backfill_dryrun.log")


def parse_removed_hhs_oig():
    """Return list of title prefixes where the backfill removed HHS-OIG."""
    changes = []
    current_title = None
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"\[(\d+)/\d+\]\s+CHANGE:\s+(.*)$", line)
            if m:
                current_title = m.group(2).strip()
                continue
            if current_title is None:
                continue
            ra_m = re.match(r"\s+ra:\s+(\[.*?\])\s+->\s+(\[.*?\])\s*$", line)
            if ra_m:
                try:
                    old = eval(ra_m.group(1))
                    new = eval(ra_m.group(2))
                except Exception:
                    continue
                if "HHS-OIG" in old and "HHS-OIG" not in new:
                    changes.append(current_title)
    return changes


def fetch_body(url):
    url_l = url.lower()
    if (url_l.endswith('.pdf') or url_l.endswith('/dl') or
            '/media/' in url_l or '/download' in url_l):
        return ""
    try:
        soup = scrape_page_with_browser(url)
        if not soup: return ""
        main = (soup.find("main") or soup.find("article") or soup.body)
        if not main: return ""
        for t in main.find_all(["nav", "footer", "aside", "script", "style"]):
            t.decompose()
        return re.sub(r"\s+", " ", main.get_text(" ", strip=True))[:15000]
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Write restores. Default: dry-run.")
    args = ap.parse_args()
    if not HAS_PLAYWRIGHT:
        print("Playwright required", file=sys.stderr)
        sys.exit(1)

    removed_titles = parse_removed_hhs_oig()
    print(f"Checking {len(removed_titles)} items where HHS-OIG was removed...")

    d = json.load(open(ACTIONS_FILE, encoding="utf-8"))
    by_title_prefix = {}
    for x in d["actions"]:
        key = (x.get("title", "") or "")[:60]
        by_title_prefix.setdefault(key, []).append(x)

    def find_item(title_frag):
        key = title_frag[:60]
        if key in by_title_prefix and len(by_title_prefix[key]) == 1:
            return by_title_prefix[key][0]
        frag_trimmed = title_frag.rstrip().rstrip("\ufffd").rstrip("?")[:50]
        matches = [x for x in d["actions"]
                   if x.get("title", "").startswith(frag_trimmed)]
        return matches[0] if len(matches) == 1 else None

    restored = []
    unchanged = 0
    unmatched = 0
    for i, title_frag in enumerate(removed_titles, 1):
        if i % 20 == 0 or i == 1:
            print(f"... [{i}/{len(removed_titles)}] processing", flush=True)
        item = find_item(title_frag)
        if not item:
            unmatched += 1
            continue
        if "HHS-OIG" in (item.get("related_agencies") or []):
            # already present — likely user manually restored
            unchanged += 1
            continue
        body = fetch_body(item.get("link", ""))
        if not body:
            unmatched += 1
            continue
        agencies = extract_investigator_agencies(body)
        if "HHS-OIG" in agencies:
            ra = list(item.get("related_agencies") or [])
            if "HHS-OIG" not in ra:
                ra.append("HHS-OIG")
            if not args.apply:
                print(f"[{i}] RESTORE: {item.get('title','')[:75]}")
            restored.append((item, ra))
            if args.apply:
                item["related_agencies"] = ra
        else:
            unchanged += 1
        if i % 10 == 0:
            time.sleep(0.5)

    print(f"\n=== Summary ===")
    print(f"Processed:     {len(removed_titles)}")
    print(f"Would restore: {len(restored)}")
    print(f"Unchanged:     {unchanged}")
    print(f"Unmatched:     {unmatched}")

    if args.apply:
        d["metadata"]["last_updated"] = datetime.now().isoformat()
        with open(ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        print(f"\nWrote {ACTIONS_FILE} with {len(restored)} restores.")
    else:
        print("\n[DRY-RUN — rerun with --apply]")


if __name__ == "__main__":
    main()
