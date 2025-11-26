#!/usr/bin/env python3
import os
import json
import re
import time
import random
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# --------------------------------------------------
# Configuration
# --------------------------------------------------

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "")

# Only the tricky or JS heavy sites go here
DYNAMIC_URLS = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://city5.nyc/",
    "https://ibis.powerappsportals.com/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    "https://www.nychdc.com/find-re-rentals",
]

STATE_DIR = Path(".")
APT_STATE_FILE = STATE_DIR / "dynamic_apartments.json"
TEXT_FILE = STATE_DIR / "dynamic_texts.json"

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
        print(f"[DEBUG] {msg}")


# --------------------------------------------------
# Helpers
# --------------------------------------------------


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[WARN] Could not read {path}: {exc}")
        return {}


def save_json(path: Path, data: dict) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"[ERROR] Could not write {path}: {exc}")


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Site specific filters are optional but help cut noise

def filter_iafford(text: str) -> str:
    marker = "RE-RENTALS"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


def filter_afny(text: str) -> str:
    marker = "RE-RENTALS"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


CONTENT_FILTERS = {
    "https://iaffordny.com/re-rentals": filter_iafford,
    "https://afny.org/re-rentals": filter_afny,
}


def apply_content_filters(url: str, text: str) -> str:
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter:
        text = site_filter(text)
    return text


# --------------------------------------------------
# Fetch with Playwright, with basic anti blocking
# --------------------------------------------------


def fetch_rendered_text(url: str) -> str:
    # Small random delay to avoid obvious patterns
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
            page.wait_for_timeout(5000)

            content = page.content()
            if "forbidden" in content.lower() or "access denied" in content.lower():
                raise Exception("Site blocking detected")

            html = content
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw text length for {url}: {len(raw_text)}")

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    text = "\n".join(lines)
    text = apply_content_filters(url, text)

    debug_print(f"[dynamic] Filtered text length for {url}: {len(text)}")
    debug_print(f"[dynamic] First 200 chars for {url}: {text[:200]}")

    text = normalize_whitespace(text)
    return text


# --------------------------------------------------
# Apartment extraction and change formatting
# --------------------------------------------------


def extract_apartment_ids(text: str, url: str) -> set[str]:
    """
    Extract identifiers that represent individual apartments or listings.
    The goal is to be stable if the page layout or order changes.
    """
    apartments: set[str] = set()

    # Unit numbers like "Unit 408", "Apt 12F" - use finditer + group(0)
    for match in re.finditer(
        r"(Unit|Apt|Apartment)\s+\d+[A-Z]?", text, re.IGNORECASE
    ):
        apartments.add(match.group(0))

    # Address plus unit combos
    for match in re.finditer(
        r"(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+[Aa]partments?[-\s]*(?:Unit\s+)?(\d+[A-Z]?)?",
        text,
    ):
        apartments.add(match.group(0))

    # Bedroom plus location plus rent
    for match in re.finditer(
        r"(\d+)[-\s]*Bedroom\s+([A-Za-z\s]+)[:;]?\s*\$?([\d,]+)",
        text,
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
        r"\b(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b.*?\$\s*([\d,]+)",
        text,
    ):
        address, rent = match.groups()
        rent_clean = rent.replace(",", "")
        apartments.add(f"{address}-${rent_clean}")

    debug_print(f"[dynamic] Raw extracted {len(apartments)} ids for {url}")
    return apartments


def format_apartment_changes(added: set[str], removed: set[str]) -> str | None:
    if not added and not removed:
        return None

    parts: list[str] = []

    if added:
        parts.append("🆕 NEW LISTINGS:")
        for apt in sorted(added)[:10]:
            parts.append(f"  • {apt}")
        if len(added) > 10:
            parts.append(f"  • ... and {len(added) - 10} more")

    if removed:
        parts.append("")
        parts.append("❌ REMOVED:")
        for apt in sorted(removed)[:5]:
            parts.append(f"  • {apt}")
        if len(removed) > 5:
            parts.append(f"  • ... and {len(removed) - 5} more")

    return "\n".join(parts)


def send_ntfy_alert(url: str, diff_summary: str | None) -> None:
    if not diff_summary:
        print(f"[INFO] No meaningful apartment changes on {url}")
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
                "Priority": "high",
                "Tags": "house,tada",
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


# --------------------------------------------------
# Main dynamic monitor
# --------------------------------------------------


def run_dynamic_once() -> None:
    apt_state = load_json(APT_STATE_FILE)
    text_state = load_json(TEXT_FILE)

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic site {url}")

        try:
            new_text = fetch_rendered_text(url)
        except Exception as exc:
            print(f"[ERROR] Failed to render {url}: {exc}")
            if "forbidden" in str(exc).lower() or "403" in str(exc):
                print("[WARN] Blocking detected, will retry next run")
            continue

        if (
            "forbidden" in new_text.lower()
            or "access denied" in new_text.lower()
            or "403" in new_text.lower()
        ):
            print(f"[WARN] {url} appears blocked or forbidden, skipping")
            continue

        if len(new_text) < 50:
            print(
                f"[WARN] {url} returned very short content "
                f"({len(new_text)} chars), skipping"
            )
            continue

        new_apartments = extract_apartment_ids(new_text, url)
        old_apartments = set(apt_state.get(url, []))

        debug_print(f"[dynamic] Found {len(new_apartments)} apartments on {url}")

        if not old_apartments:
            print(f"[INIT] Recording {len(new_apartments)} apartments for {url}")
            apt_state[url] = sorted(new_apartments)
            text_state[url] = new_text
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if added or removed:
            print(
                f"[CHANGE] {url}: +{len(added)} apartments, "
                f"-{len(removed)} apartments"
            )

            if added:
                print(f"  Added example: {list(added)[:3]}")
            if removed:
                print(f"  Removed example: {list(removed)[:3]}")

            diff_summary = format_apartment_changes(added, removed)

            # Only alert when there are new listings
            if added and diff_summary:
                send_ntfy_alert(url, diff_summary)

            apt_state[url] = sorted(new_apartments)
            text_state[url] = new_text
            changed_any = True
        else:
            print(f"[NOCHANGE] {url} apartment set unchanged")

    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_FILE, text_state)
    else:
        print("[INFO] No dynamic changes to save")


if __name__ == "__main__":
    run_dynamic_once()
