#!/usr/bin/env python3
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, Optional, Set

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ============================================================
# URL list
# ============================================================

# KEEP YOUR EXISTING LIST HERE, just make sure the variable
# is called DYNAMIC_URLS.
DYNAMIC_URLS = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    # keep all your other dynamic URLs here
]

# ============================================================
# Files for state
# ============================================================

APT_STATE_FILE = Path("dynamic_apartments.json")
TEXT_STATE_FILE = Path("dynamic_texts.json")

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

DEBUG = os.environ.get("DEBUG", "").lower() == "true"


def debug_print(msg: str) -> None:
    if DEBUG:
        print("[DEBUG]", msg)


# ============================================================
# State helpers
# ============================================================


def load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not load {path}: {e}")
        return {}


def save_json(path: Path, data: Dict[str, object]) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Could not save {path}: {e}")


def normalize_whitespace(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def apply_content_filters(url: str, text: str) -> str:
    return normalize_whitespace(text)


# ============================================================
# Playwright fetch
# ============================================================


def fetch_rendered_text(url: str) -> Optional[str]:
    delay = random.uniform(2, 5)
    print(f"[INFO] Waiting {delay:.1f}s before fetching {url}")
    time.sleep(delay)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(5000)
            html = page.content()
        finally:
            browser.close()

    if "forbidden" in html.lower() or "access denied" in html.lower():
        print(f"[WARN] Appears blocked when fetching {url}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw text length for {url}: {len(raw_text)}")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = apply_content_filters(url, text)
    debug_print(f"[dynamic] Filtered text length for {url}: {len(text)}")
    return text


# ============================================================
# Apartment ID extraction
# ============================================================


def extract_apartment_ids(text: str, url: str) -> Set[str]:
    apartments: Set[str] = set()

    # Unit numbers like "Unit 408", "Apt 12F"
    for match in re.finditer(r"(Unit|Apt|Apartment)\s+\d+[A-Z]?", text, re.IGNORECASE):
        apartments.add(match.group(0))

    # Address plus unit combos
    for match in re.finditer(
        r"(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+[Aa]partments?[-\s]*(?:Unit\s+)?(\d+[A-Z]?)?",
        text,
    ):
        apartments.add(match.group(0))

    # Bedroom plus location plus rent
    for match in re.finditer(
        r"(\d+)[-\s]*Bedroom\s+([A-Za-z\s]+)[:;]?\s*\$?([\d,]+)", text
    ):
        bedrooms, location, rent = match.groups()
        loc_clean = location.strip()[:20]
        rent_clean = rent.replace(",", "")
        apartments.add(f"{bedrooms}BR-{loc_clean}-${rent_clean}")

    # Building with rent
    for match in re.finditer(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+Apartments.*?Rent:\s*\$([\d,]+)", text
    ):
        building, rent = match.groups()
        rent_clean = rent.replace(",", "")
        apartments.add(f"{building}-${rent_clean}")

    # Address with rent
    for match in re.finditer(
        r"\b(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b.*?\$\s*([\d,]+)", text
    ):
        address, rent = match.groups()
        rent_clean = rent.replace(",", "")
        apartments.add(f"{address}-${rent_clean}")

    debug_print(f"[dynamic] Raw extracted {len(apartments)} ids for {url}")
    return apartments


# ============================================================
# Notification
# ============================================================


def format_apartment_changes(
    added: Set[str], removed: Set[str]
) -> Optional[str]:
    if not added and not removed:
        return None

    parts = []

    if added:
        parts.append("NEW LISTINGS:")
        for apt in sorted(added)[:10]:
            parts.append(f"  • {apt}")
        if len(added) > 10:
            parts.append(f"  • and {len(added) - 10} more")

    if removed:
        parts.append("")
        parts.append("REMOVED:")
        for apt in sorted(removed)[:5]:
            parts.append(f"  • {apt}")
        if len(removed) > 5:
            parts.append(f"  • and {len(removed) - 5} more")

    return "\n".join(parts)


def send_ntfy_alert(url: str, diff_summary: Optional[str], priority: str = "4") -> None:
    if not diff_summary:
        print(f"[INFO] No meaningful changes on {url}")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set. Would have sent:")
        print(diff_summary)
        return

    body = f"{url}\n\n{diff_summary}"
    title = "New housing listings"
    tags = "housing,info"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
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
        print(f"[ERROR] Sending alert for {url}: {e}")


# ============================================================
# Main
# ============================================================


def run_dynamic_once() -> None:
    apt_state = load_json(APT_STATE_FILE)
    text_state = load_json(TEXT_STATE_FILE)

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic {url}")
        text = fetch_rendered_text(url)
        if not text:
            continue

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
            summary = format_apartment_changes(added, removed)

            if added:
                send_ntfy_alert(url, summary, priority="5")
            elif len(removed) > 5:
                send_ntfy_alert(url, summary, priority="2")

            apt_state[url] = sorted(new_apts)
            text_state[url] = text
            changed_any = True
        else:
            print(f"[NOCHANGE] {url}")

    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_STATE_FILE, text_state)
        print("[INFO] Dynamic state saved.")
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
