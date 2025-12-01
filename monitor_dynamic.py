# monitor_dynamic.py
import os
import json
import re
import time
import random
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =========================
# Configuration
# =========================

# JS-heavy / dynamic pages to watch
DYNAMIC_URLS = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://mgnyconsulting.com/listings/",
    "https://city5.nyc/",
    "https://ibis.powerappsportals.com/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    # Add or remove as needed
]

# Where we keep previous state
APT_STATE_FILE = Path("dynamic_apartments.json")
TEXT_FILE = Path("dynamic_page_texts.json")  # for debugging

# ntfy topic URL comes from Actions secret
NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()

DEBUG = os.environ.get("DEBUG", "").lower() == "true"


# =========================
# Helpers
# =========================

def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


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
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(path)


def normalize_whitespace(text: str) -> str:
    """Collapse whitespace to make regex matching more reliable."""
    return re.sub(r"\s+", " ", text).strip()


# =========================
# Fetch rendered page
# =========================

def fetch_rendered_text(url: str) -> str:
    """
    Render a JS-heavy page with Playwright and return its text content.
    Includes a bit of jitter and a realistic user agent to reduce blocking.
    """
    # Random delay so we do not look like a perfect cron pattern
    time.sleep(random.uniform(2, 5))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
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
        finally:
            browser.close()

    if "forbidden" in html.lower() or "access denied" in html.lower():
        raise Exception("Site blocking detected or 403 page")

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw text length for {url}: {len(raw_text)}")
    return raw_text


# =========================
# Apartment ID extraction
# =========================

# Things that look like “ids” but are really summary lines
IGNORE_APT_ID_PATTERNS = [
    re.compile(r"results\s+neighborhood", re.IGNORECASE),
    re.compile(r"\bresults\b", re.IGNORECASE),
    re.compile(r"\bhousehold\b", re.IGNORECASE),
    re.compile(r"\bperson\b", re.IGNORECASE),
]


def extract_apartment_ids(text: str, url: str) -> set[str]:
    """
    Extract identifiers that represent individual apartments or listings.
    The goal is to be stable if the page layout or order changes and to
    ignore summary counters like "26 Results Neighborhood:$2104".
    """
    apartments: set[str] = set()

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

    # Filter out obvious non-listing ids like "26 Results Neighborhood:$2104"
    cleaned: set[str] = set()
    for apt in apartments:
        if any(rx.search(apt) for rx in IGNORE_APT_ID_PATTERNS):
            continue
        cleaned.add(apt)

    debug_print(f"[dynamic] Extracted {len(cleaned)} apartment ids for {url}")
    return cleaned


# =========================
# Diff formatting and alerts
# =========================

def format_apartment_changes(added: set[str], removed: set[str]) -> str | None:
    """Format apartment changes for notification."""
    if not added and not removed:
        return None

    parts: list[str] = []

    if added:
        parts.append("🆕 NEW LISTINGS:")
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


# =========================
# Main loop
# =========================

def run_dynamic_once() -> None:
    apt_state = load_json(APT_STATE_FILE)
    text_state = load_json(TEXT_FILE)

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic site {url}")

        try:
            raw_text = fetch_rendered_text(url)
        except Exception as e:
            print(f"[ERROR] Failed to render {url}: {e}")
            continue

        if len(raw_text) < 50:
            print(f"[WARN] {url} returned very short content ({len(raw_text)} chars), skipping")
            continue

        norm_text = normalize_whitespace(raw_text)
        new_apartments = extract_apartment_ids(norm_text, url)

        old_apartments_list = apt_state.get(url, [])
        old_apartments = set(old_apartments_list)

        if not old_apartments:
            # First run: record baseline, no alert
            print(f"[INIT] Recording {len(new_apartments)} apartments for {url}")
            apt_state[url] = sorted(new_apartments)
            text_state[url] = raw_text
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if added or removed:
            print(f"[CHANGE] {url}: +{len(added)} apartments, -{len(removed)} apartments")
            if added:
                diff_summary = format_apartment_changes(added, removed)
                if diff_summary:
                    send_ntfy_alert(url, diff_summary)
            else:
                print("[INFO] Only removals detected; not alerting.")

            apt_state[url] = sorted(new_apartments)
            text_state[url] = raw_text
            changed_any = True
        else:
            print(f"[NOCHANGE] {url} – same apartments")

    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_FILE, text_state)
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
