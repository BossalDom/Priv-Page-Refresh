#!/usr/bin/env python3
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup

# ============================================================
# URL list - 20 Static Sites
# ============================================================

STATIC_URLS: list[str] = [
    # HPD & Government-adjacent
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://cgmrcompliance.com/housing-opportunities-1",

    # Property/Management Sites
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://fifthave.org/re-rental-availabilities/",
    "https://ihrerentals.com/",
    "https://kgupright.com/",
    "https://www.langsampropertyservices.com/affordable-rental-opportunities",
    "https://www.mickigarciarealty.com/",
    "https://sbmgmt.sitemanager.rentmanager.com/RECLAIMHDFC.aspx",
    "https://ahgleasing.com/",
    "https://residenewyork.com/property-status/open-market/",
    "https://www.sjpny.com/affordable-rerentals",
    "https://soisrealestateconsulting.com/current-projects-1",
    "https://springmanagement.net/apartments-for-rent/",
    "https://www.taxaceny.com/projects-8",
    "https://tfc.com/about/affordable-re-rentals",
    "https://www.thebridgeny.org/news-and-media",
    "https://wavecrestrentals.com/section.php?id=1",
    "https://yourneighborhoodhousing.com/",
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

DEBUG = os.environ.get("DEBUG", "").lower() == "true"


# ============================================================
# Helpers for state storage and debug
# ============================================================

def debug_print(msg: str) -> None:
    if DEBUG:
        print("[DEBUG]", msg)

def load_json(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Error loading {path}: {e}")
        return {}

def save_json(path: Path, data: Dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================
# Core Functions
# ============================================================

def fetch_page_text(url: str) -> Optional[str]:
    try:
        # Use simple requests for static content
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()

    except requests.exceptions.Timeout:
        print(f"[ERROR] Timeout fetching {url}")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP Error {e.response.status_code} fetching {url}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return None

    html = response.text

    if "forbidden" in html.lower() or "access denied" in html.lower():
        print(f"[WARN] Appears blocked when fetching {url}")
        return None

    # Use BeautifulSoup to strip HTML tags and normalize text
    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")

    # Clean and normalize the text
    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    return text

def hash_text(text: str) -> str:
    # Use SHA256 hash of the page content to detect changes
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def summarize_diff(old_text: str, new_text: str) -> str:
    # Use difflib to generate a summary of changes
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
    summary = ""
    changes = 0

    for line in diff:
        if line.startswith(("+ ", "- ")):
            summary += line + "\n"
            changes += 1
            if changes >= 15:
                # Truncate long diffs
                summary += f"\n...and more lines changed. (Truncated for brevity)\n"
                break

    if not summary:
        return "Content changed, but diff summary is empty. Check page_texts.json."

    return summary


def send_ntfy_alert(url: str, message: str) -> None:
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set. Alert skipped.")
        return

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": f"Static Site Change: {url}",
                "Tags": "page_facing_up",
                "Priority": "default",
                "X-Link": url,
            },
            timeout=10,
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
        print(f"[INFO] Checking static {url}")
        text = fetch_page_text(url)
        if text is None:
            continue

        new_hash = hash_text(text)
        old_hash = hashes.get(url)
        old_text = texts.get(url, "")

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

if __name__ == "__main__":
    run_once()
