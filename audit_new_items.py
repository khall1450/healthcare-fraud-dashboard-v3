#!/usr/bin/env python3
"""Pre-publish audit for newly-scraped enforcement actions.

Diffs ``data/actions.json`` against the version in ``git HEAD`` to find items
added since the last commit, runs each new title through a strict healthcare-
context check, and moves items that don't obviously match into
``data/needs_review.json``. Approved items stay in actions.json and ship on
the next dashboard publish; flagged items wait for human (or AI) review.

Subcommands:

    python audit_new_items.py            # default: run the diff + flag pass
    python audit_new_items.py audit      # same as above
    python audit_new_items.py list       # show pending review items
    python audit_new_items.py promote ID # move an item from needs_review back to actions
    python audit_new_items.py reject ID  # confirm rejection (link is permanently blocked)

The companion file ``data/needs_review.json`` has two sections:

    {
      "items":           [ ... full action objects awaiting review ... ],
      "rejected_links":  [ "https://www.justice.gov/...", ... ]
    }

``rejected_links`` is read by ``update.py`` during scraping so confirmed
rejections never get re-pulled.

This script is the foundation for an AI-assisted review layer (commit 2),
which will process items in needs_review.json automatically and either
auto-promote, auto-reject, or escalate based on Claude's confidence.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "data", "actions.json")
REVIEW_FILE = os.path.join(SCRIPT_DIR, "data", "needs_review.json")
SUMMARY_FILE = os.path.join(SCRIPT_DIR, "data", "_audit_summary.md")

# ---------------------------------------------------------------------------
# Strict healthcare-context patterns. Anything matching these is auto-approved.
# Anything that doesn't match is flagged for review (NOT auto-rejected).
# ---------------------------------------------------------------------------
HC_KEYWORDS = re.compile(
    r"\b("
    # Programs
    r"medicare|medicaid|tricare|medi-?cal|chip\s+program|"
    r"affordable\s+care|\baca\b|obamacare|"
    # Generic healthcare
    r"health\s*care|healthcare|"
    r"hospital|clinic|physician|doctor|nurse|patient|"
    r"prescription|pharmac|hospice|home\s+health|"
    r"nursing\s+(home|facility)|skilled\s+nursing|long.term\s+care|"
    r"assisted\s+living|adult\s+day\s+care|"
    # Service types / specialties
    r"dental|dentist|behavioral\s+health|substance\s+abuse|addiction|"
    r"opioid|fentanyl|oxycodone|hydrocodone|controlled\s+substance|"
    r"telemedic|telehealth|"
    r"medical\s+(device|equipment|practice|center|group|provider|laborator|necessity)|"
    r"\bdme\b|dmepos|durable\s+medical|wound\s+care|skin\s+substitute|"
    r"genetic\s+test|genomic|"
    r"laborator|\blab\b|diagnostic|implant|prosthet|orthotic|"
    r"cardiac|cardio|oncolog|radiolog|podiatr|dermatolog|psychiatr|"
    r"pediatr|gyneco|ophthalmo|urolog|neurolog|rheumat|chiropract|"
    r"physiatr|physical\s+therapy|occupational\s+therapy|speech\s+therapy|"
    r"recovery\s+center|rehabilitation|ambulance|ambulatory|"
    r"health\s+(system|services|group|plan|insurance|net)|"
    r"pharma|drug\s+(company|manufacturer|distrib)|biotech|biologic|"
    r"vaccine|botox|insulin|infusion|"
    # Agencies
    r"\bcms\b|\bhhs\b|\boig\b|\bfda\b|\bdea\b|"
    # Healthcare-specific legal terms
    r"false\s+claims\s+act|anti.?kickback|stark\s+law|qui\s+tam|"
    r"whistleblower(?!.*tax)|"
    # Procedure / claim types
    r"upcod|unbundl|phantom\s+billing|prescription\s+(drug|fraud)|"
    r"compound\s+(drug|pharmacy)|drug\s+diversion|pill\s+mill|"
    r"\bnpi\b|provider\s+(enroll|number)"
    r")\b",
    re.IGNORECASE,
)

# Healthcare entity names that frequently appear without an HC keyword
HC_ENTITIES = re.compile(
    r"\b("
    r"kaiser|aetna|centene|humana|cigna|unitedhealth|elevance|molina|"
    r"anthem|blue\s+cross|blue\s+shield|"
    r"cvs|walgreens|rite\s+aid|express\s+scripts|optum|"
    r"amerisourcebergen|mckesson|cardinal\s+health|"
    r"pfizer|merck|abbvie|gilead|amgen|bristol[\s-]?myers|johnson\s*&\s*johnson|"
    r"novartis|sanofi|astrazeneca|eli\s+lilly|bayer|roche|"
    r"exactech|omnicare|dana[\s-]?farber|bioreference|opko|"
    r"atlantic\s+biologicals|semler|aesculap|magellan|"
    r"catholic\s+health|multicare|trinity\s+health|"
    r"hca|tenet\s+healthcare|community\s+health\s+systems|"
    r"davita|fresenius|encompass|brookdale|sunrise\s+senior"
    r")\b",
    re.IGNORECASE,
)


def is_obviously_healthcare(item: dict) -> bool:
    """True if the item title or link slug clearly references healthcare.

    Used as a regex pre-filter — items that match here skip the review queue.
    Items that don't match are flagged for human / AI review (NOT rejected).
    """
    title = item.get("title", "") or ""
    link = item.get("link", "") or ""
    text = f"{title} {link}"
    return bool(HC_KEYWORDS.search(text) or HC_ENTITIES.search(text))


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------
def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def load_review() -> dict:
    review = load_json(REVIEW_FILE, {"items": [], "rejected_links": []})
    review.setdefault("items", [])
    review.setdefault("rejected_links", [])
    return review


def get_committed_ids() -> set:
    """Return the set of action IDs in data/actions.json at git HEAD.

    Returns an empty set if HEAD has no actions.json (e.g. fresh repo).
    """
    try:
        out = subprocess.check_output(
            ["git", "show", "HEAD:data/actions.json"],
            cwd=SCRIPT_DIR,
            stderr=subprocess.DEVNULL,
        )
        committed = json.loads(out.decode("utf-8", errors="replace"))
        return {a["id"] for a in committed.get("actions", []) if "id" in a}
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return set()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_audit() -> int:
    """Diff actions.json vs HEAD, move flagged items to needs_review.json."""
    committed_ids = get_committed_ids()
    data = load_json(DATA_FILE, {"actions": []})
    actions = data.get("actions", [])

    new_items = [a for a in actions if a.get("id") not in committed_ids]
    if not new_items:
        print("audit: no new items since last commit, nothing to do")
        # Clear summary file so a stale one doesn't confuse the workflow
        if os.path.exists(SUMMARY_FILE):
            os.remove(SUMMARY_FILE)
        return 0

    print(f"audit: {len(new_items)} new items since last commit")

    review = load_review()
    approved_new = []
    flagged = []
    for item in new_items:
        if is_obviously_healthcare(item):
            approved_new.append(item)
        else:
            flagged.append(item)

    if not flagged:
        print(f"audit: all {len(new_items)} new items passed the healthcare check")
        _write_summary(approved_new, [])
        return 0

    # Strip flagged items from actions.json, append to needs_review.json
    flagged_ids = {a["id"] for a in flagged}
    data["actions"] = [a for a in actions if a.get("id") not in flagged_ids]

    now = datetime.now().isoformat()
    for item in flagged:
        item["flagged_at"] = now
        item["flag_reason"] = "title lacks healthcare keyword"
        review["items"].append(item)

    save_json(DATA_FILE, data)
    save_json(REVIEW_FILE, review)

    print(f"audit: {len(approved_new)} auto-approved, {len(flagged)} flagged for review:")
    for item in flagged:
        print(f"  - {item['id']}: {item.get('title', '')[:80]}")
    print()
    print(f"  approved items kept in {os.path.basename(DATA_FILE)}")
    print(f"  flagged items moved to {os.path.basename(REVIEW_FILE)}")
    print(f"  promote a flagged item: python audit_new_items.py promote <id>")
    print(f"  permanently reject:     python audit_new_items.py reject <id>")

    _write_summary(approved_new, flagged)
    return 0


def cmd_list() -> int:
    """Show pending items in needs_review.json."""
    review = load_review()
    items = review.get("items", [])
    if not items:
        print("no items pending review")
        return 0
    print(f"{len(items)} item(s) pending review:")
    print()
    for item in items:
        print(f"  {item.get('id', '?')}")
        print(f"    title:    {item.get('title', '')[:90]}")
        print(f"    link:     {item.get('link', '')[:90]}")
        print(f"    type:     {item.get('type', '?')}")
        print(f"    flagged:  {item.get('flagged_at', '?')}")
        print(f"    reason:   {item.get('flag_reason', '?')}")
        print()
    print(f"To promote: python audit_new_items.py promote <id>")
    print(f"To reject:  python audit_new_items.py reject <id>")
    return 0


def cmd_promote(item_id: str) -> int:
    """Move an item from needs_review.json back to actions.json."""
    review = load_review()
    item = next((a for a in review["items"] if a.get("id") == item_id), None)
    if not item:
        print(f"promote: no item with id {item_id!r} in {REVIEW_FILE}", file=sys.stderr)
        return 1

    # Strip review-only metadata before re-adding
    item.pop("flagged_at", None)
    item.pop("flag_reason", None)
    item.pop("ai_decision", None)
    item.pop("ai_confidence", None)
    item.pop("ai_reason", None)

    data = load_json(DATA_FILE, {"actions": []})
    data.setdefault("actions", []).append(item)
    save_json(DATA_FILE, data)

    review["items"] = [a for a in review["items"] if a.get("id") != item_id]
    save_json(REVIEW_FILE, review)

    print(f"promoted {item_id} -> {os.path.basename(DATA_FILE)}")
    print(f"  title: {item.get('title', '')[:80]}")
    return 0


def cmd_reject(item_id: str) -> int:
    """Permanently reject an item; its link is added to rejected_links."""
    review = load_review()
    item = next((a for a in review["items"] if a.get("id") == item_id), None)
    if not item:
        print(f"reject: no item with id {item_id!r} in {REVIEW_FILE}", file=sys.stderr)
        return 1

    link = item.get("link", "")
    if link and link not in review["rejected_links"]:
        review["rejected_links"].append(link)

    review["items"] = [a for a in review["items"] if a.get("id") != item_id]
    save_json(REVIEW_FILE, review)

    print(f"rejected {item_id}")
    print(f"  title: {item.get('title', '')[:80]}")
    if link:
        print(f"  link added to rejected_links — scraper will skip this URL going forward")
    return 0


# ---------------------------------------------------------------------------
# Workflow integration
# ---------------------------------------------------------------------------
def _write_summary(approved: list, flagged: list) -> None:
    """Write a markdown summary the GHA workflow can paste into the PR body."""
    lines = []
    if approved:
        lines.append(f"### Auto-approved ({len(approved)} new items)")
        for item in approved:
            lines.append(f"- {item.get('title', '')[:120]}")
        lines.append("")
    if flagged:
        lines.append(f"### Needs review ({len(flagged)} item(s) — moved to needs_review.json)")
        lines.append("")
        lines.append("These items were scraped but did not match the healthcare keyword filter. ")
        lines.append("They are NOT live on the dashboard. Review and either promote or reject:")
        lines.append("")
        for item in flagged:
            lines.append(f"- **{item.get('title', '')}**")
            link = item.get("link", "")
            if link:
                lines.append(f"  [{link}]({link})")
            lines.append(f"  `python audit_new_items.py promote {item['id']}`  or  `reject {item['id']}`")
            lines.append("")
    save_json(SUMMARY_FILE.replace(".md", ".json"), {
        "approved": [{"id": a["id"], "title": a.get("title", "")} for a in approved],
        "flagged":  [{"id": a["id"], "title": a.get("title", ""), "link": a.get("link", "")} for a in flagged],
    })
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Audit newly-scraped items before publish.")
    parser.add_argument(
        "cmd",
        nargs="?",
        default="audit",
        choices=["audit", "list", "promote", "reject"],
    )
    parser.add_argument("item_id", nargs="?")
    args = parser.parse_args()

    if args.cmd == "audit":
        return cmd_audit()
    if args.cmd == "list":
        return cmd_list()
    if args.cmd in ("promote", "reject"):
        if not args.item_id:
            print(f"{args.cmd} requires an item ID", file=sys.stderr)
            return 2
        return cmd_promote(args.item_id) if args.cmd == "promote" else cmd_reject(args.item_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
