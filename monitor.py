#!/usr/bin/env python3
"""
Static website monitor.

Use this for pages where any text change is important.
Apartment listing pages should normally go in monitor_dynamic.py instead,
so that only unit or building changes trigger alerts.
"""

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
# URL list - only non listing pages should go here
# ============================================================

STATIC_URLS = [
    # Example:
    # "https://www.taxaceny.com/projects-8",
    # "https://www.thebridgeny.org/news-and-media",
    # If a page primarily lists apartments, move it to DYNAMIC_URLS
]

# ============================================================
# Files for state
# ============================================================

HASH_FILE = Path("hashes.json")
TEXT_FILE = Path("page_texts.json")

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# How big a change must be to bother you on static pages
MIN_DIFF_SNIPPETS = 1
MIN_DIFF_CHARS = 80  # ignore very tiny changes


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
    text = normalize_whitespace(text)

    print(f"[INFO] Text length for {url}: {len(text)} characters")
    return text


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 5,
    context_chars: int = 120,
    max_chars: int = 1500,
) -> Optional[str]:
    """
    Summarize the change. If it is too small or uninformative,
    return None so we do not send an alert.
    """
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
                removals.append(f"- {segment[:160]}")

    snippets = additions[:max_snippets]
    if len(snippets) < max_snippets:
        snippets.extend(removals[: max_snippets - len(snippets)])

    if not snippets:
        return None

    summary = "\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[...truncated]"

    # If the diff is extremely small, ignore it
    if len(summary) < MIN_DIFF_CHARS and len(snippets) < MIN_DIFF_SNIPPETS:
        return None

    return summary


# ============================================================
# Notification
# ============================================================


def send_ntfy_alert(url: str, diff_summary: Optional[str]) -> None:
    """
    For static pages, if diff_summary is None we do not send anything.
    That removes the useless "content changed but empty summary" spam.
    """
    if not diff_summary:
        print(f"[INFO] Change at {url} was too minor. No static alert.")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set. Would have sent:")
        print(diff_summary)
        return

    body = f"{url}\n\nStatic page change:\n\n{diff_summary}"

    # Headers must be pure ASCII
    title = "Static Site Change"
    tags = "housing,static"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "3",
                "Tags": tags,
                "Click": url,
            },
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Static alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending static ntfy alert for {url}: {e}")


# ============================================================
# Main
# ============================================================


def run_once() -> None:
    if not STATIC_URLS:
        print("[INFO] No STATIC_URLS configured, nothing to do.")
        return

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
            print(f"[INIT] Recording baseline for static {url}")
            hashes[url] = new_hash
            texts[url] = text
            changed_any = True
            continue

        if new_hash == old_hash:
            print(f"[NOCHANGE] {url}")
            continue

        print(f"[CHANGE] Static content hash changed for {url}")
        diff_summary = summarize_diff(old_text, text)
        send_ntfy_alert(url, diff_summary)

        hashes[url] = new_hash
        texts[url] = text
        changed_any = True

    if changed_any:
        save_json(HASH_FILE, hashes)
        save_json(TEXT_FILE, texts)
        print("[INFO] Static state saved.")
    else:
        print("[INFO] No static changes to save.")


if __name__ == "__main__":
    run_once()
