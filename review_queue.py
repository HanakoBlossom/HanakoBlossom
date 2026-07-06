#!/usr/bin/env python3
"""
Rebuild the Briarmont "Review Queue" document in Outline, automatically.

Status comes from each document's own status emoji, read either from the
document's icon or from a leading emoji in its title (whichever you use):

    👀  ->  Needs review
    🌱  ->  Being worked on
    ✅  ->  Recently shipped   (auto-culled after QUEUE_SHIPPED_DAYS days)
    ⏸️  ->  ignored (parked)
    🗄️  ->  ignored (archived)

Each tracked doc becomes a row under its section, sorted oldest to newest,
top down, with columns: Doc, Owner, Notes.

Owner defaults to the document's author. Override it, and add a note, with an
optional info callout in the doc header:

    :::info
    Owner: Hanako
    Notes: One sentence on where this stands.
    :::

Required environment variables:
  OUTLINE_URL      Base URL, no trailing slash, no /api.  e.g. https://wiki.example
  OUTLINE_API_KEY  A key from Settings > API Keys.

Optional environment variables:
  QUEUE_TITLE          Exact title of the queue document (default: Review Queue).
  QUEUE_SHIPPED_DAYS   How long a shipped doc stays listed (default: 14).
"""

import os
import sys
from datetime import datetime, timezone, timedelta

import requests


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


OUTLINE_URL = env("OUTLINE_URL", required=True).rstrip("/")
API_KEY = env("OUTLINE_API_KEY", required=True)
QUEUE_TITLE = env("QUEUE_TITLE", "Review Queue")
SHIPPED_DAYS = int(env("QUEUE_SHIPPED_DAYS", "14"))

API = f"{OUTLINE_URL}/api"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

MIDDOT = "\u00b7"

# Emoji -> status. Only these three appear in the queue.
TRACKED_EMOJI = {
    "\U0001F440": "review",  # 👀
    "\U0001F331": "draft",   # 🌱
    "\u2705": "live",        # ✅
}

# Section order, top to bottom: key, emoji, heading, text shown when empty.
SECTIONS = [
    ("review", "👀", "Needs review", "Nothing waiting on review."),
    ("draft", "🌱", "Being worked on", "Nothing in progress right now."),
    ("live", "✅", "Recently shipped", "Nothing shipped recently."),
]


def post(endpoint, payload):
    """Every Outline API endpoint is a POST. Returns the parsed JSON."""
    resp = requests.post(f"{API}/{endpoint}", json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_all_documents():
    """Page through every document, oldest first."""
    docs = []
    offset = 0
    limit = 100
    while True:
        data = post("documents.list", {
            "limit": limit, "offset": offset,
            "sort": "createdAt", "direction": "ASC",
        })
        batch = data.get("data", [])
        docs.extend(batch)
        if len(batch) < limit or offset >= 5000:
            break
        offset += limit
    return docs


def status_from_doc(doc):
    """Read the status emoji from the doc icon first, then the title."""
    for key in ("emoji", "icon"):
        value = doc.get(key)
        if isinstance(value, str):
            for emoji, status in TRACKED_EMOJI.items():
                if value.startswith(emoji):
                    return status
    title = (doc.get("title") or "").lstrip()
    for emoji, status in TRACKED_EMOJI.items():
        if title.startswith(emoji):
            return status
    return None


def strip_leading_emoji(title):
    """Remove a leading status emoji from a title for clean display."""
    t = (title or "").lstrip()
    for emoji in TRACKED_EMOJI:
        if t.startswith(emoji):
            return t[len(emoji):].strip()
    return t.strip()


def header_value(text, label, trim_middot=False):
    """Pull a value like 'Owner: Hanako' out of the doc header."""
    prefix = label.lower() + ":"
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith(prefix):
            value = s[len(prefix):].strip()
            if trim_middot and MIDDOT in value:
                value = value.split(MIDDOT)[0].strip()
            return value
    return ""


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cell(value):
    """Make a value safe for a markdown table cell."""
    return (value or "").replace("|", "/").replace("\n", " ").strip()


def find_queue_doc_id():
    hits = post("documents.search", {"query": QUEUE_TITLE, "limit": 25}).get("data", [])
    for item in hits:
        doc = item.get("document", item)
        if doc.get("title", "").strip().lower() == QUEUE_TITLE.strip().lower():
            return doc["id"]
    sys.exit(f'Could not find a document titled "{QUEUE_TITLE}". Create it first.')


def build_body(buckets):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {key: len(rows) for key, rows in buckets.items()}

    # Amber banner when reviews are waiting, green when caught up.
    banner = "warning" if counts["review"] else "tip"
    headline = ("You have items waiting on review" if counts["review"]
                else "All caught up on reviews")
    lines = [
        f":::{banner}",
        f"**{headline}**",
        f"👀 {counts['review']} to review  ·  "
        f"🌱 {counts['draft']} in progress  ·  "
        f"✅ {counts['live']} recently shipped",
        f"Rebuilt {now}",
        ":::",
        "",
    ]

    for key, emoji, heading, empty_text in SECTIONS:
        lines.append(f"## {emoji} {heading}")
        lines.append("")
        rows = buckets[key]
        if not rows:
            lines.append(f"_{empty_text}_")
            lines.append("")
            continue
        lines.append("| Doc | Owner | Notes |")
        lines.append("| :-- | :-- | :-- |")
        for d in rows:
            title = cell(strip_leading_emoji(d.get("title", "Untitled")))
            url = OUTLINE_URL + (d.get("url", "") or "")
            owner = cell(d.get("_owner", ""))
            notes = cell(d.get("_notes", ""))
            lines.append(f"| **[{title}]({url})** | {owner} | {notes} |")
        lines.append("")

    lines.append("---")
    lines.append("_Auto-generated. Edits here are overwritten on the next run._")
    return "\n".join(lines).rstrip() + "\n"


def main():
    now = datetime.now(timezone.utc)
    shipped_cutoff = now - timedelta(days=SHIPPED_DAYS)
    buckets = {"review": [], "draft": [], "live": []}

    for doc in list_all_documents():
        if doc.get("title", "").strip().lower() == QUEUE_TITLE.strip().lower():
            continue
        status = status_from_doc(doc)
        if status not in buckets:
            continue
        if status == "live":
            updated = parse_dt(doc.get("updatedAt", ""))
            if updated and updated < shipped_cutoff:
                continue

        info = post("documents.info", {"id": doc["id"]}).get("data", {})
        text = info.get("text", "")
        author = (info.get("createdBy") or {}).get("name", "")
        doc["_owner"] = header_value(text, "Owner", trim_middot=True) or author
        doc["_notes"] = header_value(text, "Notes")
        buckets[status].append(doc)

    for status in buckets:
        buckets[status].sort(key=lambda d: d.get("createdAt", ""))  # oldest first

    post("documents.update", {"id": find_queue_doc_id(), "text": build_body(buckets)})
    print(
        f"Updated '{QUEUE_TITLE}': "
        f"{len(buckets['review'])} to review, "
        f"{len(buckets['draft'])} in progress, "
        f"{len(buckets['live'])} recently shipped."
    )


if __name__ == "__main__":
    main()
