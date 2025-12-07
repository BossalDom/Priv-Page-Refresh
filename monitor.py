#!/usr/bin/env python3
"""
Static site monitor.

Checks a list of URLs, captures cleaned text, compares to the last
version, and sends an ntfy alert if there is a meaningful change.

Relies on env:
    NTFY_TOPIC_URL   – ntfy topic URL
    DEBUG            – "true" to print extra logs
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import shutil
import time
from pathlib import Path
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

ROOT = Path(__file__).parent

HASH_FILE = ROOT / "page_hashes.json"
TEXT_FILE = ROOT / "page_texts.json"

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

# How aggressively to filter small diffs
MIN_DIFF_CHARS = 120
MIN_DIFF_SNIPPETS = 1

WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# IMPORTANT: put only non-listing pages here.
# Apartment listing pages belong in monitor_dynamic.py.
STATIC_URLS = [
    "https://www.spjny.com/affordable-rentals",
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://www.thebridgeny.org/news-and-media",
    "https://www.taxaceny.com/projects-8",
    # Add or remove static (non-listing) pages as needed
]

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


def load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Could not read {path}: {e}")
        return {}


def save_json(path: Path, data: Dict[str, object]) -> None:
    """Atomic JSON write to avoid corrupting state on crashes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        shutil.move(str(tmp_path), str(path))
    except Exception as e:
        print(f"[ERROR] Could not save {path}: {e}")
        try:
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink()
        except Exception:
        # nothing else to do
            pass


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def hash_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Content filtering
# ---------------------------------------------------------------------


def filter_reside_google(text: str) -> str:
    """Example filter for HPD Google Site – skip nav if needed."""
    marker = "Affordable Housing"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


CONTENT_FILTERS = {
    "https://sites.google.com/affordablelivingnyc.com/hpd/home": filter_reside_google,
}


def apply_content_filters(url: str, text: str) -> str:
    func = CONTENT_FILTERS.get(url)
    if func:
        text = func(text)
    return text


# ---------------------------------------------------------------------
# Network / diff / ntfy
# ---------------------------------------------------------------------


def fetch_page_text(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=WEB_HEADERS, timeout=45)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")

    debug_print(f"Raw length for {url}: {len(raw_text)}")

    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    text = "\n".join(lines)
    text = apply_content_filters(url, text)
    text = normalize_whitespace(text)

    debug_print(f"Normalized length for {url}: {len(text)}")
    return text


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 5,
    context_chars: int = 120,
    max_chars: int = 1500,
) -> Optional[str]:
    """
    Summarize change between two versions.

    If the change is too small or uninformative, return None so
    no alert is sent.
    """
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    additions = []
    removals = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("insert", "replace"):
            seg = new_text[j1:j2].strip()
            if seg and len(seg) >= 10:
                start = max(0, j1 - context_chars)
                end = min(len(new_text), j2 + context_chars)
                snippet = new_text[start:end].strip()
                additions.append(f"+ {snippet}")

        if tag in ("delete", "replace"):
            seg = old_text[i1:i2].strip()
            if seg and len(seg) >= 10:
                removals.append(f"- {seg[:160]}")

    snippets = additions[:max_snippets]
    if len(snippets) < max_snippets:
        snippets.extend(removals[: max_snippets - len(snippets)])

    # Remove empty snippets
    snippets = [s.strip() for s in snippets if s.strip()]
    if not snippets:
        return None

    summary = "\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[...truncated]"

    # Ignore tiny diffs
    if len(summary) < MIN_DIFF_CHARS and len(snippets) < MIN_DIFF_SNIPPETS:
        return None

    return summary


def send_ntfy_alert(url: str, diff_summary: str) -> None:
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set – would have sent alert")
        print(diff_summary)
        return

    body = f"Static site change detected:\n{url}\n\n{diff_summary}"

    headers = {
        # ASCII only to avoid latin-1 header errors
        "Title": f"Static Site Change: {url}",
        "Priority": "3",
        "Tags": "static,monitor",
    }

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert for {url}: {e}")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def run_static_once() -> None:
    hash_state = load_json(HASH_FILE)
    text_state = load_json(TEXT_FILE)

    for url in STATIC_URLS:
        print(f"[INFO] Checking {url}")
        new_text = fetch_page_text(url)
        if new_text is None:
            continue

        old_text = text_state.get(url)

        if old_text is None:
            print(f"[INIT] Baseline stored for {url}")
            text_state[url] = new_text
            hash_state[url] = hash_text(new_text)
            continue

        if new_text == old_text:
            print(f"[NOCHANGE] {url}")
            continue

        summary = summarize_diff(old_text, new_text)
        if summary is None:
            print(
                f"[INFO] {url}: content changed but diff not significant; "
                "updating baseline without alert"
            )
        else:
            send_ntfy_alert(url, summary)

        text_state[url] = new_text
        hash_state[url] = hash_text(new_text)

    save_json(TEXT_FILE, text_state)
    save_json(HASH_FILE, hash_state)


if __name__ == "__main__":
    run_static_once()
