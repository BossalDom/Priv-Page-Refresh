import os
import json
import re
import time
import random
import hashlib
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==========================
# Configuration
# ==========================

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
        print(f"[DEBUG] {msg}")


# URLs that need JavaScript rendering
DYNAMIC_URLS = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://mgnyconsulting.com/listings/",
    "https://city5.nyc/",
    "https://ibis.powerappsportals.com/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
]

# Files to store state
HASH_FILE = Path("dynamic_hashes.json")        # kept for compatibility
TEXT_FILE = Path("dynamic_texts.json")         # for debugging


# ==========================
# Utility helpers
# ==========================

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not load {path}: {e}")
        return {}


def save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# Optionally reuse static filters if you have them in monitor.py.
# For now we just pass text through.

def apply_content_filters(url: str, text: str) -> str:
    return text


# ==========================
# Dynamic fetching with Playwright
# ==========================

def fetch_rendered_text(url: str) -> str:
    """Render the page with Playwright and return visible text."""
    # Small random delay so we do not look like a bot
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
            # Wait for content to settle
            try:
                page.wait_for_selector("body", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(5000)

            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw HTML text length for {url}: {len(raw_text)}")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = apply_content_filters(url, text)

    debug_print(f"[dynamic] Filtered text length for {url}: {len(text)}")
    text = normalize_whitespace(text)
    return text


# ==========================
# Apartment id extraction
# ==========================

def extract_apartment_ids(text: str, url: str) -> set[str]:
    """
    Extract identifiers that represent individual apartments or listings.
    The goal is to stay stable if the page layout or order changes.
    """
    apartments: set[str] = set()

    # 1) Unit numbers like "Unit 408", "Apt 12F"
    for match in re.finditer(r"(Unit|Apt|Apartment)\s+\d+[A-Z]?", text, re.IGNORECASE):
        apartments.add(match.group(0))

    # 2) Address plus unit combos
    for match in re.finditer(
        r"(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+[Aa]partments?[-\s]*(?:Unit\s+)?(\d+[A-Z]?)?",
        text,
    ):
        apartments.add(match.group(0))

    # 3) Bedroom plus location plus rent
    for match in re.finditer(
        r"(\d+)[-\s]*Bedroom\s+([A-Za-z\s]+?)[:;]?\s*\$?([\d,]+)", text
    ):
        bedrooms, location, rent = match.groups()
        loc_clean = location.strip()[:20]
        rent_clean = rent.replace(",", "")
        apartments.add(f"{bedrooms}BR-{loc_clean}-${rent_clean}")

    # 4) Building with rent
    for match in re.finditer(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+Apartments.*?Rent:\s*\$([\d,]+)", text
    ):
        building, rent = match.groups()
        rent_clean = rent.replace(",", "")
        apartments.add(f"{building}-${rent_clean}")

    # 5) Address with rent
    for match in re.finditer(
        r"\b(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b.*?\$\s*([\d,]+)", text
    ):
        address, rent = match.groups()
        rent_clean = rent.replace(",", "")
        apartments.add(f"{address}-${rent_clean}")

    # Clean up generic or noisy ids that cause false positives
    blacklist_words = [
        "results",
        "neighborhood",
        "household & income",
        "household and income",
        "clear",
        "filters",
        "filter",
        "signup",
        "sign up",
        "view map",
        "sort by",
    ]

    must_have_keywords = [
        "unit",
        "apt",
        "apartment",
        "bedroom",
        "br",
        "avenue",
        "street",
        " st ",
        "road",
        "boulevard",
        "blvd",
    ]

    cleaned: set[str] = set()
    for apt in apartments:
        lower = apt.lower()

        # IAfford specific - anything with "results" is a header, not a listing
        if "results" in lower:
            continue

        if any(w in lower for w in blacklist_words):
            continue

        # Require at least one "real listing" keyword
        if not any(k in lower for k in must_have_keywords):
            continue

        cleaned.add(apt)

    debug_print(
        f"[dynamic] Raw extracted {len(apartments)} ids for {url}, kept {len(cleaned)}"
    )
    return cleaned


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ==========================
# Diffing and notifications
# ==========================

def format_apartment_changes(added: set[str], removed: set[str]) -> str | None:
    """Format apartment changes for notification."""
    if not added and not removed:
        return None

    parts: list[str] = []

    if added:
        parts.append("🔵 NEW LISTINGS:")
        for apt in sorted(added)[:10]:
            parts.append(f"  • {apt}")
        if len(added) > 10:
            parts.append(f"  … and {len(added) - 10} more")

    if removed:
        parts.append("\n❌ REMOVED:")
        for apt in sorted(removed)[:5]:
            parts.append(f"  • {apt}")
        if len(removed) > 5:
            parts.append(f"  … and {len(removed) - 5} more")

    return "\n".join(parts)


def send_ntfy_alert(url: str, diff_summary: str | None) -> None:
    if not diff_summary:
        print(f"[INFO] No meaningful changes on {url}")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set")
        print(f"[ALERT] Would notify for {url}:\n{diff_summary}")
        raise ValueError("NTFY_TOPIC_URL not configured")

    body = f"{url}\n\n{diff_summary}"
    title = "🏠 New housing listings"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "4",
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
    except Exception as e:
        print(f"[ERROR] Sending alert: {e}")
        raise


# ==========================
# Main dynamic monitor
# ==========================

def run_dynamic_once() -> None:
    # Old states (kept mainly for debugging)
    hash_state = load_json(HASH_FILE)
    text_state = load_json(TEXT_FILE)

    # New file for apartment ids
    apt_state_file = Path("dynamic_apartments.json")
    apt_state = load_json(apt_state_file)

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic {url}")

        try:
            new_text = fetch_rendered_text(url)
        except Exception as e:
            print(f"[ERROR] Failed to render {url}: {e}")
            if "forbidden" in str(e).lower() or "403" in str(e):
                print("[WARN] Possible blocking, will retry next run")
            continue

        if "forbidden" in new_text.lower() or "access denied" in new_text.lower():
            print(f"[WARN] {url} returned forbidden/blocked content, skipping")
            continue

        if len(new_text) < 50:
            print(f"[WARN] {url} returned very short content ({len(new_text)} chars)")
            continue

        new_apartments = extract_apartment_ids(new_text, url)
        old_apartments_list = apt_state.get(url, [])
        old_apartments = set(old_apartments_list)

        debug_print(f"[dynamic] Final apartment id count for {url}: {len(new_apartments)}")

        if not old_apartments:
            # First run - record baseline only
            print(f"[INIT] Recording {len(new_apartments)} apartments for {url}")
            apt_state[url] = sorted(new_apartments)
            text_state[url] = new_text
            hash_state[url] = hash_text(new_text)
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if added or removed:
            print(f"[CHANGE] {url}: +{len(added)} apartments, -{len(removed)} apartments")

            if added:
                print(f"  Added sample: {list(added)[:3]}")
            if removed:
                print(f"  Removed sample: {list(removed)[:3]}")

            diff_summary = format_apartment_changes(added, removed)

            # Only notify when new apartments appear
            if added and diff_summary:
                send_ntfy_alert(url, diff_summary)

            apt_state[url] = sorted(new_apartments)
            text_state[url] = new_text
            hash_state[url] = hash_text(new_text)
            changed_any = True
        else:
            print(f"[NOCHANGE] {url} - apartments unchanged")

    if changed_any:
        save_json(apt_state_file, apt_state)
        save_json(TEXT_FILE, text_state)
        save_json(HASH_FILE, hash_state)
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
