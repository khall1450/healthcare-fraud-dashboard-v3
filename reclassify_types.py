"""Dry-run type reclassification: re-run get_action_type() against all
items in actions.json and report items whose CURRENT stored type differs
from what the (post-refactor) classifier would now return.

Uses title + description only (no body fetch needed — title-first
classifier is already position-based).

Usage:
    python reclassify_types.py              # dry-run diff
    python reclassify_types.py --apply      # write changes (no cutoff by default)
    python reclassify_types.py --apply --date-cutoff 2026-01-13
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import get_action_type

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_FILE = os.path.join(SCRIPT_DIR, "data", "actions.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--date-cutoff", default="")
    args = ap.parse_args()

    d = json.load(open(ACTIONS_FILE, encoding="utf-8"))
    diffs = []
    unchanged = 0
    skipped_cutoff = 0

    for x in d["actions"]:
        title = x.get("title", "") or ""
        desc = ""  # no description stored anymore; title-only
        agency = x.get("agency", "")
        link = x.get("link", "")
        stored_type = x.get("type", "") or ""
        if not stored_type:
            continue

        fresh_type = get_action_type(title, desc, agency=agency, link=link)
        if fresh_type != stored_type:
            if args.date_cutoff and (x.get("date", "") or "") >= args.date_cutoff:
                skipped_cutoff += 1
                continue
            diffs.append((x, stored_type, fresh_type))
        else:
            unchanged += 1

    print(f"Processed:     {len(d['actions'])}")
    print(f"Unchanged:     {unchanged}")
    print(f"Would change:  {len(diffs)}")
    print(f"Skipped cutoff:{skipped_cutoff}")
    print()

    # Break down by transition
    from collections import Counter
    transitions = Counter()
    for _, old, new in diffs:
        transitions[f"{old} -> {new}"] += 1
    print("Transitions:")
    for t, c in transitions.most_common():
        print(f"  {c:4d}  {t}")
    print()

    print("First 30 changes:")
    for x, old, new in diffs[:30]:
        print(f"  [{x.get('date','')[:10]}] {old} -> {new}  |  {x.get('title','')[:75]}")

    if args.apply:
        for x, _, new in diffs:
            x["type"] = new
        d["metadata"]["last_updated"] = datetime.now().isoformat()
        with open(ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        print(f"\nWrote {ACTIONS_FILE}: {len(diffs)} type corrections")
    else:
        print("\n[DRY-RUN — rerun with --apply to write]")


if __name__ == "__main__":
    main()
