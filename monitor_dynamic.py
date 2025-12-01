#!/usr/bin/env python3
"""
Dynamic website monitor.

Uses Playwright to render JS heavy sites, then extracts apartment level
identifiers and compares sets between runs. Only alerts when apartments
are added or removed, not when text order or counts change.

State is stored in:
  - dynamic_apartments.json   (list of apartment ids per url)
  - dynamic_texts.json        (raw normalized text per url, for debugging)
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ------------- config -------------

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Sites that benefit from JS rendering or that have complex listing layouts
DYNAMIC_URLS: List[str] = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://city5.nyc/",
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://fifthave.org/re-rental-availabilities/",
    "https://ihrerentals.com/",
    "https://ibis.powerappsportals.com/",
    "https://kgupright.com/",
    "https://www.langsampropertyservices.com/affordable-rental-opportunities",
    "https://mgnyconsulting.com/listings/",
    "https://www.mickigarciarealty.com/",
    "https://www.prontohousingrentals.com/",
    "https://sbmgmt.sitemanager.rentmanager.com/RECLAIMHDFC.aspx",
    "https://ahgleasing.com/",
    "https://residenewyork.com/property-status/open-market/",
    "https://riseboro.org/housing/woodlawn-senior-living/",
    "https://streeteasy.com/building/riverton-square",
    "https://www.sjpny.com/affordable-rerentals",
    "https://soisrealestateconsulting.com/current-projects-1",
    "https://springmanagement.net/apartments-for-rent/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://www.taxaceny.com/projects-8",
    "https://tfc.com/about/affordable-re-rentals",
    "https://www.thebridgeny.org/news-and-media",
    "https://wavecrestrentals.com/section.php?id=1",
]

HASH_FILE = Path("dynamic_hashes.json")  # kept for backward compatibility
TEXT_FILE = Path("dynamic_texts.json")
APT_FILE = Path("dynamic_apartments.json")

DEBUG = os.environ.get("DEBUG", "").lower() == "true"


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


# -------- helpers for state --------


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[WARN] Could not read {path}: {exc}")
        return {}


def save_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    tmp.replace(path)


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# reuse some static filters so output is similar


def filter_resideny_open_market(text: str) -> str:
    marker = "Open Market"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


def filter_ahg(text: str) -> str:
    marker = "Affordable Housing Group"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


def filter_google_sites(text: str) -> str:
    marker = "HPD"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://ahgleasing.com/": filter_ahg,
    "https://sites.google.com/affordablelivingnyc.com/hpd/home": filter_google_sites,
}


def apply_content_filters(url: str, text: str) -> str:
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter:
        text = site_filter(text)
    return text


# -------- Playwright fetch --------


def fetch_rendered_text(url: str) -> str:
    """
    Use Playwright to fetch and render a page that may use JS.

    Includes random delay and a desktop user agent to reduce blocking.
    """

    time.sleep(random.uniform(2, 5))

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
            try:
                page.wait_for_selector("body", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(5000)
            html = page.content()
            if "forbidden" in html.lower() or "access denied" in html.lower():
                raise RuntimeError("Site blocking detected")
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw text length for {url}: {len(raw_text)}")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = apply_content_filters(url, text)
    debug_print(f"[dynamic] Filtered text length for {url}: {len(text)}")

    text = normalize_whitespace(text)
    return text


# -------- apartment id extraction --------


def extract_apartment_ids(text: str, url: str) -> Set[str]:
    """
    Extract identifiers that represent individual apartments or listings.

    We keep things like:
      - "11 Hancock Street Apartments - Unit 5A"
      - "The Urban 144-74 Northern Boulevard - Multiple Units $2104"
      - "1BR-Queens-$2700"

    and try to drop things like:
      - "26 Results Neighborhood-$2104"
      - "1 Person-$2700"
    """
    apartments: Set[str] = set()

    # Unit numbers like "Unit 408", "Apt 12F", "Apartment 5A"
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
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+Apartments.*?Rent:\s*\$([\d,]+)",
        text,
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

    # Filter out obvious non listing fragments
    BAD_WORDS = [
        "Results",
        "Person",
        "People",
        "Household",
        "Income",
        "Neighborhood",
        "Beds",
        "Price",
        "Range",
    ]

    GOOD_HINTS = [
        "Apartment",
        "Apartments",
        "Unit",
        "Street",
        " St ",
        "Avenue",
        " Ave",
        "Boulevard",
        " Blvd",
        "Road",
        " Rd",
        "Place",
        " Pl ",
        "Court",
        " Ct ",
        "Drive",
        " Dr ",
        "Tower",
        "House",
    ]

    filtered: Set[str] = set()
    for apt in apartments:
        s = apt.strip()

        if any(word in s for word in BAD_WORDS):
            continue

        if not any(hint in s for hint in GOOD_HINTS):
            continue

        filtered.add(s)

    debug_print(
        f"[dynamic] Raw extracted {len(apartments)} ids, kept {len(filtered)} for {url}"
    )
    return filtered


# -------- diff and notifications --------


def format_apartment_changes(
    added: Set[str], removed: Set[str]
) -> Optional[str]:
    if not added and not removed:
        return None

    parts: List[str] = []

    if added:
        parts.append("NEW LISTINGS:")
        for apt in sorted(added)[:10]:
            parts.append(f"  • {apt}")
        if len(added) > 10:
            parts.append(f"  ... and {len(added) - 10} more")

    if removed:
        parts.append("")
        parts.append("REMOVED:")
        for apt in sorted(removed)[:5]:
            parts.append(f"  • {apt}")
        if len(removed) > 5:
            parts.append(f"  ... and {len(removed) - 5} more")

    return "\n".join(parts)


def send_ntfy_alert(url: str, diff_summary: Optional[str]) -> None:
    """
    Send an ntfy notification.

    Headers must be ASCII due to latin 1 encoding. Emojis can go in body.
    """
    if not diff_summary:
        print(f"[INFO] No meaningful changes on {url}")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set")
        print(f"[ALERT] Would notify for {url}:\n{diff_summary}")
        raise ValueError("NTFY_TOPIC_URL not configured")

    body = f"{url}\n\n{diff_summary}"
    title = "New housing listings"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "4",
                "Tags": "house,warning",
                "Click": url,
            },
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code}")
            raise RuntimeError(f"Notification failed: {resp.status_code}")
    except Exception as exc:
        print(f"[ERROR] Sending alert: {exc}")
        raise


# -------- main dynamic run --------


def run_dynamic_once() -> None:
    hash_state = load_json(HASH_FILE)
    text_state = load_json(TEXT_FILE)
    apt_state = load_json(APT_FILE)

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic {url}")

        try:
            new_text = fetch_rendered_text(url)
        except Exception as exc:
            print(f"[ERROR] Failed to render {url}: {exc}")
            if "forbidden" in str(exc).lower() or "403" in str(exc).lower():
                print("[WARN] Possible blocking, will retry on next run.")
            continue

        if "forbidden" in new_text.lower() or "403" in new_text.lower():
            print(f"[WARN] {url} looks forbidden or blocked, skipping.")
            continue

        if len(new_text) < 50:
            print(
                f"[WARN] {url} returned very short content ({len(new_text)} chars), skipping."
            )
            continue

        new_apartments = extract_apartment_ids(new_text, url)
        old_apartments = set(apt_state.get(url, []))

        debug_print(f"[dynamic] Found {len(new_apartments)} apartments on {url}")

        if not old_apartments:
            print(
                f"[INIT] Recording {len(new_apartments)} apartments as baseline for {url}"
            )
            apt_state[url] = sorted(new_apartments)
            text_state[url] = new_text
            hash_state[url] = "baseline"
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if not added and not removed:
            print(f"[NOCHANGE] {url} - same apartments")
            continue

        print(
            f"[CHANGE] {url}: +{len(added)} apartments, -{len(removed)} apartments"
        )
        if added:
            print(f"  Added sample: {list(added)[:3]}")
        if removed:
            print(f"  Removed sample: {list(removed)[:3]}")

        diff_summary = format_apartment_changes(added, removed)

        # Only alert when there are new apartments
        if added and diff_summary:
            send_ntfy_alert(url, diff_summary)

        apt_state[url] = sorted(new_apartments)
        text_state[url] = new_text
        hash_state[url] = "updated"
        changed_any = True

    if changed_any:
        save_json(APT_FILE, apt_state)
        save_json(TEXT_FILE, text_state)
        save_json(HASH_FILE, hash_state)
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
