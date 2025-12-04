#!/usr/bin/env python3
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup

# ============================================================
# URL list
# ============================================================

# KEEP YOUR EXISTING LIST HERE, just make sure the variable
# is called STATIC_URLS.
STATIC_URLS = [
    "https://residenewyork.com/property-status/open-market/",
    "https://mgnyconsulting.com/listings/",
    # keep all your other static URLs here
]

# ============================================================
# Files for state
# ============================================================

HASH_FILE = Path("hashes.json")
TEXT_FILE = Path("page_texts.json")

# Notification target
NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ============================================================
# Helpers for state storage
# ============================================================


def load_json(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not load {path}: {e}")
        return {}


def save_json(path: Path, data: Dict[str, str]) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Could not save {path}: {e}")


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_whitespace(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


# ============================================================
# Content filters and extraction
# ============================================================

LISTING_KEYWORDS = [
    r"\b(apartment|apt|unit|studio|bedroom|br)\b",
    r"\b(rent|rental|lease|available|vacancy)\b",
    r"\$\d{1,3}(?:,\d{3})*",
    r"\b\d+\s*(bed|br|bedroom)\b",
    r"\b(one|two|three|1|2|3)[\s-]*bedroom\b",
    r"\b(floor|bldg|building|address|location)\b",
    r"\b(apply|application|waitlist|lottery)\b",
]

LISTING_REGEXES = [re.compile(p, re.IGNORECASE) for p in LISTING_KEYWORDS]

IGNORE_PATTERNS = [
    r"^(skip to|menu|search|login|sign in|subscribe|newsletter)\b",
    r"^(facebook|twitter|instagram|linkedin|youtube)\b",
    r"^(privacy policy|terms|copyright|cookies)\b",
    r"^\s*[×✕✖]\s*$",
    r"^(home|about|contact|careers|media|events)\s*$",
]

IGNORE_REGEXES = [re.compile(p, re.IGNORECASE) for p in IGNORE_PATTERNS]


def filter_resideny_open_market(text: str) -> str:
    marker = "Open Market"
    idx = text.lower().find(marker.lower())
    if idx != -1:
        text = text[idx:]
    return text


def filter_mgny(text: str) -> str:
    for marker in ["Available Apartments", "Listings"]:
        idx = text.lower().find(marker.lower())
        if idx != -1:
            text = text[idx:]
            break
    return text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://mgnyconsulting.com/listings/": filter_mgny,
    # you can add more site specific filters here if needed
}


def extract_relevant_content(text: str) -> str:
    lines = text.splitlines()
    relevant_lines = []
    context_window = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        if any(rx.match(line) for rx in IGNORE_REGEXES):
            continue

        has_listing_content = any(rx.search(line) for rx in LISTING_REGEXES)

        if has_listing_content:
            relevant_lines.extend(context_window)
            relevant_lines.append(line)
            context_window = []
        else:
            context_window.append(line)
            if len(context_window) > 2:
                context_window.pop(0)

    result = "\n".join(relevant_lines)

    if len(result) < 100:
        return text

    return result


def apply_content_filters(url: str, text: str) -> str:
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter:
        text = site_filter(text)
    text = extract_relevant_content(text)
    return text


# ============================================================
# Fetch and diff
# ============================================================


def fetch_page_text(url: str) -> Optional[str]:
    print(f"[INFO] Fetching {url}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=45)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = apply_content_filters(url, text)
    text = normalize_whitespace(text)

    print(f"[INFO] Text length for {url}: {len(text)} characters")
    return text


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 5,
    context_chars: int = 120,
    max_chars: int = 1200,
) -> Optional[str]:
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    additions = []
    removals = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("insert", "replace"):
            segment = new_text[j1:j2].strip()
            if segment and len(segment) >= 10:
                start = max(0, j1 - context_chars)
                end = min(len(new_text), j2 + context_chars)
                snippet = new_text[start:end].strip()
                additions.append(f"+ {snippet}")

        if tag in ("delete", "replace"):
            segment = old_text[i1:i2].strip()
            if segment and len(segment) >= 10:
                removals.append(f"- {segment[:100]}")

    snippets = additions[:max_snippets]
    if len(snippets) < max_snippets:
        snippets.extend(removals[: max_snippets - len(snippets)])

    if not snippets:
        return None

    summary = "\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[...truncated]"
    return summary


# ============================================================
# Notification
# ============================================================


def send_ntfy_alert(url: str, diff_summary: Optional[str]) -> None:
    if not diff_summary:
        print(f"[INFO] Change at {url} was too minor. No alert.")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set. Would have sent:")
        print(diff_summary)
        return

    body = f"{url}\n\nChanges:\n{diff_summary}"
    # Header values must be ASCII to avoid the latin-1 error
    title = "New housing listings"
    tags = "housing,info"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "4",
                "Tags": tags,
                "Click": url,
            },
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert for {url}: {e}")


# ============================================================
# Main
# ============================================================


def run_once() -> None:
    hashes = load_json(HASH_FILE)
    texts = load_json(TEXT_FILE)

    changed_any = False

    for url in STATIC_URLS:
        text = fetch_page_text(url)
        if text is None:
            continue

        new_hash = hash_text(text)
        old_hash = hashes.get(url)
        old_text = texts.get(url)

        if old_hash is None or old_text is None:
            print(f"[INIT] Recording baseline for {url}")
            hashes[url] = new_hash
            texts[url] = text
            changed_any = True
            continue

        if new_hash == old_hash:
            print(f"[NOCHANGE] {url}")
            continue

        print(f"[CHANGE] {url} content hash changed")
        diff_summary = summarize_diff(old_text, text)
        send_ntfy_alert(url, diff_summary)

        hashes[url] = new_hash
        texts[url] = text
        changed_any = True

    if changed_any:
        save_json(HASH_FILE, hashes)
        save_json(TEXT_FILE, texts)
        print("[INFO] State saved.")
    else:
        print("[INFO] No changes to save.")


if __name__ == "__main__":
    run_once()
