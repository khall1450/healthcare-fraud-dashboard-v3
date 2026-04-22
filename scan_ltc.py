"""Targeted scan: find items mentioning LTCH/LTAC/long-term care hospital
in body text that don't currently have the Long-Term Care tag.

Dry-run by default. --apply writes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import fetch_detail_page, scrape_page_with_browser, HAS_PLAYWRIGHT

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_FILE = os.path.join(SCRIPT_DIR, "data", "actions.json")

LTC_RE = re.compile(
    r"\blong[-\s]term\s+care\s+hospitals?\b|\bltch\b|\bltac\b|"
    r"\blong[-\s]term\s+acute\s+care\b",
    re.IGNORECASE,
)
# Generic LTC threshold — 2+ mentions of "long-term care" triggers
LTC_GENERIC_RE = re.compile(r"\blong[-\s]term\s+care\b", re.IGNORECASE)


def fetch_body(url, session):
    url_l = url.lower()
    if (url_l.endswith('.pdf') or url_l.endswith('/dl') or
            '/media/' in url_l or '/download' in url_l):
        return ""
    try:
        body, _, _, _ = fetch_detail_page(session, url)
        if body and len(body) > 200:
            return body
    except Exception:
        pass
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
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    d = json.load(open(ACTIONS_FILE, encoding="utf-8"))
    # Only items that don't already have the tag
    items = [x for x in d["actions"]
             if "Long-Term Care" not in (x.get("tags") or [])]
    print(f"Scanning {len(items)} items without Long-Term Care tag")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    })

    matches = []
    fetch_failures = 0
    for i, x in enumerate(items, 1):
        if i % 25 == 0 or i == 1:
            print(f"... [{i}/{len(items)}] scanning", flush=True)
        url = x.get("link", "") or ""
        if not url:
            continue
        # Quick title-first check
        title = x.get("title", "") or ""
        if LTC_RE.search(title):
            matches.append((x, "title"))
            print(f"[{i}] MATCH(title): {title[:70]}")
            continue
        # Body fetch
        body = fetch_body(url, session)
        if not body:
            fetch_failures += 1
            continue
        if LTC_RE.search(body):
            matches.append((x, "body:ltch"))
            print(f"[{i}] MATCH(LTCH): {title[:70]}")
        else:
            # Generic LTC threshold: 2+ "long-term care" mentions
            ltc_count = len(LTC_GENERIC_RE.findall(body))
            if ltc_count >= 2:
                matches.append((x, f"body:generic(x{ltc_count})"))
                print(f"[{i}] MATCH(generic x{ltc_count}): {title[:70]}")
        if i % 10 == 0:
            time.sleep(0.3)

    print(f"\n=== Summary ===")
    print(f"Scanned:          {len(items)}")
    print(f"Matches:          {len(matches)}")
    print(f"Fetch failures:   {fetch_failures}")

    if args.apply:
        for x, src in matches:
            t = list(x.get("tags") or [])
            if "Long-Term Care" not in t:
                t.append("Long-Term Care")
                x["tags"] = t
        from datetime import datetime
        d["metadata"]["last_updated"] = datetime.now().isoformat()
        with open(ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        print(f"\nWrote {ACTIONS_FILE}: tagged {len(matches)} items")
    else:
        print("\n[DRY-RUN — rerun with --apply to write]")


if __name__ == "__main__":
    main()
