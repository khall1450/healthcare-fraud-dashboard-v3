"""Scan: flag items whose `amount` field may reflect a negotiated resolution
(NPA/DPA/ability-to-pay criminal penalty) rather than fraud size.

Surfaces items with non-null amount whose body or title contains signals
that the amount is NOT a fraud-size measure.

Dry-run only — prints candidates for manual review.
"""
from __future__ import annotations

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

# Signals that an amount is a negotiated resolution, not fraud size
NEGOTIATED_RE = re.compile(
    r"ability\s+to\s+pay|"
    r"adjusted\s+based\s+on\s+[^.]{0,60}\s+ability|"
    r"non[-\s]prosecution\s+agreement|"
    r"deferred\s+prosecution\s+agreement|"
    r"\bnpa\b|\bdpa\b",
    re.IGNORECASE,
)

# Fraud-size signals. If body also has these, amount might still be legit.
FRAUD_SIZE_RE = re.compile(
    r"restitution|forfeiture|forfeit\s+\$|loss\s+(amount|to|of\s+\$)|"
    r"false\s+claims\s+act|civil\s+settlement|civil\s+damages|"
    r"billed\s+(medicare|medicaid|tricare)\s+[^.]{0,50}\$|"
    r"fraudulently\s+billed",
    re.IGNORECASE,
)


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
    d = json.load(open(ACTIONS_FILE, encoding="utf-8"))
    items = [x for x in d["actions"]
             if x.get("amount") and x.get("amount_numeric", 0) > 0]
    print(f"Scanning {len(items)} items with a populated amount")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    })

    flagged = []
    fetch_failures = 0
    for i, x in enumerate(items, 1):
        if i % 25 == 0 or i == 1:
            print(f"... [{i}/{len(items)}] scanning", flush=True)
        url = x.get("link", "") or ""
        title = x.get("title", "") or ""
        # Title-based quick hit
        title_hit = NEGOTIATED_RE.search(title)
        if title_hit:
            flagged.append((x, f"title:{title_hit.group(0)}", ""))
            print(f"[{i}] FLAG(title:{title_hit.group(0)}): {title[:80]}")
            continue
        if not url:
            continue
        body = fetch_body(url, session)
        if not body:
            fetch_failures += 1
            continue
        neg = NEGOTIATED_RE.search(body)
        if neg:
            # Check if fraud-size signal ALSO present
            has_fraud_size = bool(FRAUD_SIZE_RE.search(body))
            flagged.append((x, f"body:{neg.group(0)}",
                            "has fraud-size signal too" if has_fraud_size else "NO fraud-size signal"))
            print(f"[{i}] FLAG(body:{neg.group(0)}) "
                  f"[{'w/fraud-size' if has_fraud_size else 'NO fraud-size'}]: "
                  f"{title[:70]}")
        if i % 10 == 0:
            time.sleep(0.3)

    print(f"\n=== Summary ===")
    print(f"Scanned:          {len(items)}")
    print(f"Flagged:          {len(flagged)}")
    print(f"Fetch failures:   {fetch_failures}")
    print()
    print("Flagged items (review manually):")
    for x, signal, note in flagged:
        amt = x.get("amount", "")
        print(f"  [{amt:>18}]  {signal:<40}  {note}")
        print(f"    {x.get('title','')[:100]}")
        print(f"    {x.get('link','')}")
        print()


if __name__ == "__main__":
    main()
