#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ============================================================
# URL list - 9 Dynamic Sites
# ============================================================

DYNAMIC_URLS: list[str] = [
    # Primary Dynamic Sites
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://mgnyconsulting.com/listings/", 
    
    # Other Dynamic/JS-reliant Sites
    "https://city5.nyc/",
    "https://ibis.powerappsportals.com/",
    "https://www.prontohousingrentals.com/",
    "https://riseboro.org/housing/woodlawn-senior-living/",
    "https://www.rivertonsquare.com/available-rentals",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
]

# ============================================================
# Files and Config
# ============================================================

APT_STATE_FILE = Path("dynamic_apartments.json")
TEXT_STATE_FILE = Path("dynamic_texts.json")
FAILURE_FILE = Path("dynamic_failures.json")
# Kept for Site-Down Spam Prevention
ALERT_COOLDOWN_FILE = Path("dynamic_last_alert.json") 

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
# Helpers
# ============================================================

def debug_print(msg: str) -> None:
    if DEBUG:
        print("[DEBUG]", msg)

def load_json(path: Path) -> Dict[str, List[str] | int | float]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[WARN] Error loading {path}: {e}")
        return {}

def save_json(path: Path, data: Dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================
# Playwright fetch
# ============================================================

def fetch_rendered_text(url: str) -> Optional[str]:
    delay = random.uniform(2, 5)
    print(f"[INFO] Waiting {delay:.1f}s before fetching {url}")
    time.sleep(delay)

    html = ""
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            page = context.new_page()

            try:
                # Playwright timeout is 30 seconds
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                # Wait 2 seconds for any immediate rendering
                page.wait_for_timeout(2000)
                
                html = page.content()
            
            except Exception as nav_err:
                print(f"[ERROR] Playwright Navigation Error for {url}: {nav_err}")
                return None
            finally:
                browser.close()

    except Exception as e:
        print(f"[ERROR] Playwright session crash on {url}: {e}")
        return None

    if "forbidden" in html.lower() or "access denied" in html.lower():
        print(f"[WARN] Appears blocked when fetching {url}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    return text

# ============================================================
# Extraction and Alerting
# ============================================================

def extract_apartment_ids(text: str, url: str) -> Set[str]:
    """
    Extracts unique identifiers for available apartments from the page text.
    """
    # Regex 1: Highly dynamic sites (iAfford, AFNY) looking for full listing strings
    if url in ["https://iaffordny.com/re-rentals", "https://afny.org/re-rentals"]:
        pattern = re.compile(r"(\d+ [A-Z].*?Rent:[\s\$]*[\d,]+)", re.MULTILINE | re.IGNORECASE)
    
    # Regex 2: RentCafe and sites that list unit/apt/address
    elif "rentcafewebsite.com" in url or "city5.nyc" in url or "mgnyconsulting.com" in url:
        # Looks for Unit/Apt followed by number/letter, or Address followed by 'Apartments'
        pattern = re.compile(r"(Unit|Apt)\s*[\d\-A-Z]+|(\d+ \w+ (Street|Avenue|Road) Apartments)", re.MULTILINE | re.IGNORECASE)
    
    else:
        # Default fallback (including RiseBoro, Pronto, Riverton, IBIS)
        # Looks for common unit formats or rent phrases
        pattern = re.compile(r"(Unit|Apt)\s*[\d\-A-Z]+|Rent:\s*[\$\d,]+|Available Units:\s*\d+", re.MULTILINE | re.IGNORECASE)
        
    
    apartments = {match.strip() for match in pattern.findall(text)}
    
    # Flatten tuples and clean extra whitespace
    cleaned_apts = set()
    for item in apartments:
        if isinstance(item, tuple):
            for sub_item in item:
                if sub_item:
                    cleaned_apts.add(re.sub(r'\s+', ' ', sub_item).strip())
        else:
            cleaned_apts.add(re.sub(r'\s+', ' ', item).strip())

    return cleaned_apts

def format_apartment_changes(added: Set[str], removed: Set[str]) -> str:
    summary = ""
    if added:
        summary += "🚨 **NEW LISTINGS ADDED:** 🚨\n- " + "\n- ".join(sorted(added)) + "\n\n"
    if removed:
        summary += "🗑️ **LISTINGS REMOVED:** 🗑️\n- " + "\n- ".join(sorted(removed)) + "\n\n"
    return summary.strip()

def send_ntfy_alert(url: str, message: str, priority: str = "3") -> None:
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set. Alert skipped.")
        return

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": f"Apartment Change: {url}",
                "Tags": "house_with_garden" if priority == "3" else "warning",
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
# Main 
# ============================================================

def run_dynamic_once() -> None:
    apt_state = load_json(APT_STATE_FILE)
    text_state = load_json(TEXT_STATE_FILE)
    
    # Load new state for failure and cooldown
    failure_counts = load_json(FAILURE_FILE)
    alert_cooldowns = load_json(ALERT_COOLDOWN_FILE) 

    changed_any = False
    current_time = time.time()
    next_failure_counts = {}

    for url in DYNAMIC_URLS:
        text = fetch_rendered_text(url)
        
        # Implement Failure Tracking
        if not text:
            count = int(failure_counts.get(url, 0)) + 1
            next_failure_counts[url] = count
            print(f"[FAIL] {url} failed to fetch ({count} consecutive times)")
            
            # Alert after 3 consecutive failures
            if count >= 3:
                # KEEPING SITE-DOWN ALERT COOLDOWN (2 hours) to prevent spam
                last_alert = float(alert_cooldowns.get(url, 0))
                if current_time - last_alert > 3600 * 2:
                    send_ntfy_alert(
                        url, 
                        f"🚨 Dynamic Site down/unreachable for {count} consecutive checks (15 minutes of downtime).", 
                        priority="4"
                    )
                    alert_cooldowns[url] = current_time
                    changed_any = True
            
            continue

        # Site fetched successfully, reset failure count
        next_failure_counts[url] = 0
        
        new_apts = extract_apartment_ids(text, url)
        old_apts = set(apt_state.get(url, []))

        if not old_apts:
            print(f"[INIT] Recording {len(new_apts)} apartments for {url}")
            apt_state[url] = sorted(new_apts)
            text_state[url] = text
            changed_any = True
            continue

        added = new_apts - old_apts
        removed = old_apts - new_apts

        if added or removed:
            print(
                f"[CHANGE] {url}: +{len(added)} apartments, "
                f"-{len(removed)} apartments"
            )
            
            # 🟢 ALERT LOGIC: Only send alert if new listings were ADDED
            if added:
                summary = format_apartment_changes(added, removed)
                send_ntfy_alert(url, summary, priority="5")
            
            # Update the cooldown file time on successful change to reset site-down timer
            alert_cooldowns[url] = current_time 

            apt_state[url] = sorted(new_apts)
            text_state[url] = text
            changed_any = True
        else:
            print(f"[NOCHANGE] {url}")

    # Save all updated state files
    save_json(FAILURE_FILE, next_failure_counts)
    save_json(ALERT_COOLDOWN_FILE, alert_cooldowns) 
    
    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_STATE_FILE, text_state)

if __name__ == "__main__":
    run_dynamic_once()
