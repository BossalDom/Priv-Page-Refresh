#!/usr/bin/env python3
"""
Dynamic website monitor for apartment listings.

This script:
- Renders pages with Playwright when needed.
- Extracts a stable set of "apartment ids" for each site.
- Sends a notification only when that set changes
  (a unit or building is added or removed).
"""

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
# URL list - ALL apartment listing sites go here
# ============================================================

DYNAMIC_URLS = [
# Primary Dynamic Sites
"https://iaffordny.com/re-rentals",
"https://afny.org/re-rentals",
"https://mgnyconsulting.com/listings/",
"https://city5.nyc/",
"https://ibis.powerappsportals.com/",
"https://www.prontohousingrentals.com/",
"https://riseboro.org/housing/woodlawn-senior-living/",
"https://www.rivertonsquare.com/available-rentals",
"https://east-village-homes-owner-llc.rentcafewebsite.com/",

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
    # For now we just normalize. Site specific trimming can be added here.
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


def extract_ids_iafford_afny(text: str) -> Set[str]:
    """
    iafford and AFNY often show buildings as:
    'The Urban 144-74 Northern Boulevard Multiple Units Rent: $...'

    We only use the portion before 'Rent:' as the stable id,
    so rent adjustments or text tweaks do not cause fake alerts.
    """
    apartments: Set[str] = set()

    pattern = re.compile(
        r"([A-Z][A-Za-z0-9 .,'\-]+?)\s+Rent:",
        re.IGNORECASE,
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        apartments.add(name)

    debug_print(f"[dynamic] iafford/afny buildings: {len(apartments)}")
    return apartments


def extract_ids_reside(text: str) -> Set[str]:
    """
    Reside New York open market listings usually have
    'Street Name Apartments - Unit 3A' style strings.
    We capture 'Building name - Unit X' where possible.
    """
    apartments: Set[str] = set()

    # Capture "Something Apartments - Unit 3A"
    pattern = re.compile(
        r"([A-Z][A-Za-z0-9 '&\-]+Apartments?)\s*[-–]\s*Unit\s+([0-9A-Z]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        building, unit = match.groups()
        apartments.add(f"{building.strip()} - Unit {unit.strip()}")

    # Fallback: building name plus rent
    if not apartments:
        pattern2 = re.compile(
            r"([A-Z][A-Za-z0-9 '&\-]+Apartments?).*?\$([\d,]+)",
            re.IGNORECASE,
        )
        for match in pattern2.finditer(text):
            building, rent = match.groups()
            rent_clean = rent.replace(",", "")
            apartments.add(f"{building.strip()} - ${rent_clean}")

    debug_print(f"[dynamic] Reside units/buildings: {len(apartments)}")
    return apartments


def extract_ids_mgny(text: str) -> Set[str]:
    """
    MGNY listings are often '2010 Walton Avenue Apartments - 1BR'
    with a unit id or simple description.
    """
    apartments: Set[str] = set()

    # Building with unit or bedroom info
    pattern = re.compile(
        r"(\d{3,5}\s+[A-Z][A-Za-z0-9 ]+?)(?:\s+Apartments?)?\s*[-–]\s*([A-Za-z0-9 ]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        addr, desc = match.groups()
        apartments.add(f"{addr.strip()} - {desc.strip()}")

    debug_print(f"[dynamic] MGNY ids: {len(apartments)}")
    return apartments


def extract_ids_generic(text: str) -> Set[str]:
    """
    Generic fallback for other listing sites.
    """
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

    debug_print(f"[dynamic] Generic ids: {len(apartments)}")
    return apartments


def extract_apartment_ids(text: str, url: str) -> Set[str]:
    url_lower = url.lower()
    if "iaffordny.com" in url_lower or "afny.org" in url_lower:
        return extract_ids_iafford_afny(text)
    if "residenewyork.com" in url_lower:
        return extract_ids_reside(text)
    if "mgnyconsulting.com" in url_lower:
        return extract_ids_mgny(text)
    return extract_ids_generic(text)


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


def send_ntfy_alert(url: str, diff_summary: Optional[str], priority: str = "5") -> None:
    if not diff_summary:
        print(f"[INFO] No meaningful apartment changes on {url}")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set. Would have sent:")
        print(diff_summary)
        return

    body = f"{url}\n\n{diff_summary}"

    # ASCII only, to avoid the latin-1 header issue
    title = "Apartment Change"
    tags = "housing,dynamic"

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
            print(f"[OK] Dynamic alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending dynamic ntfy alert for {url}: {e}")


# ============================================================
# Main
# ============================================================


def run_dynamic_once() -> None:
    if not DYNAMIC_URLS:
        print("[INFO] No DYNAMIC_URLS configured, nothing to do.")
        return

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
                # Large removals might mean a site issue.
                send_ntfy_alert(url, summary, priority="3")

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
