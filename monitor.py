#!/usr/bin/env python3
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import random   # 🟢 FIX: Added for rate limiting
import time     # 🟢 FIX: Added for rate limiting and cooldown
from pathlib import Path
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup

# ============================================================
# URL list - 20 Static Sites
# ============================================================

# Note: Keeping your original list structure.
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
# Files for state (UPDATED)
# ============================================================

HASH_FILE = Path("hashes.json")
TEXT_FILE = Path("page_texts.json")
FAILURE_FILE = Path("failures.json")             # 🟢 FIX: New file for failure count
ALERT_COOLDOWN_FILE = Path("last_alert.json")    # 🟢 FIX: New file for alert cooldown

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

def load_json(path: Path) -> Dict[str, str | int | float]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            # Safely load JSON data, converting to expected types
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[WARN] Error loading {path}: {e}")
        return {}

def save_json(path: Path, data: Dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================
# Core Functions
# ============================================================

def fetch_page_text(url: str) -> Optional[str]:
    # ... (fetch_page_text remains the same)
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


def send_ntfy_alert(url: str, message: str, priority: str = "default") -> None:
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set. Alert skipped.")
        return

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": f"Static Site Change: {url}",
                "Tags": "page_facing_up" if priority == "default" else "warning",
                "Priority": priority,
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
# Main (UPDATED with Rate Limit, Failure Tracking, and Cooldown)
# ============================================================

def run_once() -> None:
    hashes = load_json(HASH_FILE)
    texts = load_json(TEXT_FILE)
    
    # 🟢 FIX: Load new state for failure and cooldown
    failure_counts = load_json(FAILURE_FILE)
    alert_cooldowns = load_json(ALERT_COOLDOWN_FILE)

    changed_any = False
    current_time = time.time()
    
    # Use a separate dict to stage updates to failure counts
    next_failure_counts = {} 

    for url in STATIC_URLS:
        # 🟢 FIX: Add random delay for rate limiting
        delay = random.uniform(1, 3)
        print(f"[INFO] Waiting {delay:.1f}s before fetching {url}")
        time.sleep(delay)
        
        print(f"[INFO] Checking static {url}")
        text = fetch_page_text(url)
        
        # 🟢 FIX: Implement Failure Tracking
        if text is None:
            count = int(failure_counts.get(url, 0)) + 1
            next_failure_counts[url] = count
            print(f"[FAIL] {url} failed to fetch ({count} consecutive times)")
            
            # Alert after 3 consecutive failures
            if count >= 3:
                # 🟢 FIX: Implement Notification Cooldown (2 hours for site down alerts)
                last_alert = float(alert_cooldowns.get(url, 0))
                if current_time - last_alert > 3600 * 2: 
                    send_ntfy_alert(
                        url, 
                        f"🚨 Site down/unreachable for {count} consecutive checks (15 minutes of downtime).", 
                        priority="4"
                    )
                    alert_cooldowns[url] = current_time # Update alert time
                    changed_any = True # State change to save alert time
            
            continue # Skip normal processing for this URL

        # Site fetched successfully, reset failure count
        next_failure_counts[url] = 0
        
        new_hash = hash_text(text)
        old_hash = hashes.get(url)
        # Use .get with empty string fallback to handle new/missing entries cleanly
        old_text = str(texts.get(url, "")) 

        if old_hash is None or old_text == "":
            print(f"[INIT] Recording baseline for {url}")
            hashes[url] = new_hash
            texts[url] = text
            changed_any = True
            continue

        if new_hash == old_hash:
            print(f"[NOCHANGE] {url}")
            continue

        # 🟢 FIX: Implement Cooldown for Content Change Alert (1 hour)
        last_alert = float(alert_cooldowns.get(url, 0))
        if current_time - last_alert < 3600:
            print(f"[COOLDOWN] Change detected for {url}, but skipping alert (last alerted < 1hr ago)")
        else:
            print(f"[CHANGE] {url} content hash changed. Sending alert.")
            diff_summary = summarize_diff(old_text, text)
            send_ntfy_alert(url, diff_summary, priority="default")
            alert_cooldowns[url] = current_time # Update alert time
            changed_any = True

        hashes[url] = new_hash
        texts[url] = text
        changed_any = True

    # 🟢 FIX: Save all updated state files
    save_json(FAILURE_FILE, next_failure_counts)
    save_json(ALERT_COOLDOWN_FILE, alert_cooldowns)
    
    if changed_any:
        save_json(HASH_FILE, hashes)
        save_json(TEXT_FILE, texts)

if __name__ == "__main__":
    run_once()
