import os
import json
import time
import random
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==========================
# Config
# ==========================

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()

DATA_DIR = Path(".")

APT_STATE_FILE = DATA_DIR / "dynamic_apartments.json"
TEXT_STATE_FILE = DATA_DIR / "dynamic_page_texts.json"  # debug only, not used for hashes

# Dynamic pages that need JS rendering
DYNAMIC_URLS = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://mgnyconsulting.com/listings/",
    "https://ibis.powerappsportals.com/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
]

DEBUG = os.environ.get("DEBUG", "").lower() == "true"


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


# ==========================
# Utilities
# ==========================

def normalize_whitespace(text: str) -> str:
    # Collapse multiple spaces and blank lines
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


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
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Could not save {path}: {e}")


# ==========================
# Fetch with Playwright
# ==========================

def fetch_rendered_text(url: str) -> str:
    """
    Render a dynamic page using Playwright and return visible text.
    Includes a bit of jitter and a realistic user agent to reduce blocking.
    """

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
            page.wait_for_timeout(5000)

            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw HTML text length for {url}: {len(raw_text)}")

    text = "\n".join(ln.strip() for ln in raw_text.splitlines() if ln.strip())
    text = normalize_whitespace(text)
    debug_print(f"[dynamic] Normalized text length for {url}: {len(text)}")

    return text


# ==========================
# Apartment ID extraction
# ==========================

def is_noise_name(name: str) -> bool:
    """Filter out obvious non-listing lines like '27 Results' or filter headers."""
    lower = name.lower()
    if "result" in lower:
        return True
    noise_keywords = [
        "neighborhood", "price range", "beds", "amenities",
        "subway", "household & income", "household and income",
        "clear", "filters", "sort by",
    ]
    return any(k in lower for k in noise_keywords)


def extract_cards_by_rent(text: str) -> set[str]:
    """
    Generic helper for card-style listing pages (iafford, afny, mgny etc).

    Strategy:
      - split into lines
      - whenever we see a line containing both 'Rent' and a dollar amount,
        look 1–3 lines above for a plausible building name
      - use only that building name as the stable ID
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ids: set[str] = set()

    for idx, line in enumerate(lines):
        if "rent" not in line.lower():
            continue
        if not re.search(r"\$\s*\d", line):
            continue

        # Walk backwards a few lines to find a name
        for back in range(1, 4):
            j = idx - back
            if j < 0:
                break
            candidate = lines[j].strip()
            if len(candidate) < 6:
                continue
            if is_noise_name(candidate):
                continue
            ids.add(candidate)
            break

    debug_print(f"[extract_cards_by_rent] Found {len(ids)} ids")
    return ids


def extract_iafford(text: str, url: str) -> set[str]:
    # iAfford structure works very well with the rent-card approach
    ids = extract_cards_by_rent(text)
    debug_print(f"[iafford] {len(ids)} apartment ids for {url}")
    return ids


def extract_afny(text: str, url: str) -> set[str]:
    # AFNY re-rentals page layout is very similar to iAfford
    ids = extract_cards_by_rent(text)
    debug_print(f"[afny] {len(ids)} apartment ids for {url}")
    return ids


def extract_mgny(text: str, url: str) -> set[str]:
    """
    MGNY listings: treat each building name as an ID.

    Example lines:
      '2010 Walton Avenue Apartments'
      '680 East 21st Street Apartments'
    """
    ids: set[str] = set()
    for m in re.finditer(r"[A-Z0-9][A-Za-z0-9 .,'-]+Apartments", text):
        name = m.group(0).strip()
        ids.add(name)

    # Fallback to card-by-rent if pattern misses everything
    if not ids:
        ids = extract_cards_by_rent(text)

    debug_print(f"[mgny] {len(ids)} apartment ids for {url}")
    return ids


def extract_generic_apartment_ids(text: str, url: str) -> set[str]:
    """
    Generic extractor for other dynamic sites.

    This is intentionally conservative. Site-specific extractors are preferred.
    """
    apartments: set[str] = set()

    # Unit labels like "Unit 6D", "Apt 12F", "Apartment 3B"
    for match in re.finditer(r"(Unit|Apt|Apartment)\s+\d+[A-Z]?", text, re.IGNORECASE):
        apartments.add(match.group(0))

    # Address + unit combos
    for match in re.finditer(
        r"(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+[Aa]partments?[-\s]*(?:Unit\s+)?(\d+[A-Z]?)?",
        text,
    ):
        apartments.add(match.group(0))

    # Bedroom + location + rent combos
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

    debug_print(f"[generic] Raw extracted {len(apartments)} ids for {url}")
    return apartments


SITE_EXTRACTORS = {
    "https://iaffordny.com/re-rentals": extract_iafford,
    "https://afny.org/re-rentals": extract_afny,
    "https://mgnyconsulting.com/listings/": extract_mgny,
    # others can be added here later if needed
}


# ==========================
# Diff formatting and alerts
# ==========================

def format_apartment_changes(added: set[str], removed: set[str]) -> str | None:
    if not added and not removed:
        return None

    parts: list[str] = []

    if added:
        parts.append("🆕 NEW LISTINGS:")
        for apt in sorted(added)[:10]:
            parts.append(f"• {apt}")
        if len(added) > 10:
            parts.append(f"… and {len(added) - 10} more")

    if removed:
        parts.append("\n❌ REMOVED:")
        for apt in sorted(removed)[:5]:
            parts.append(f"• {apt}")
        if len(removed) > 5:
            parts.append(f"… and {len(removed) - 5} more")

    return "\n".join(parts)


def send_ntfy_alert(url: str, diff_summary: str, priority: str = "high") -> None:
    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set")
        print(f"[ALERT] Would notify for {url}:\n{diff_summary}")
        return

    body = f"{url}\n\n{diff_summary}"
    title = "🏠 New housing listings"

    # Map our simple priority labels to ntfy numeric values
    if priority == "low":
        prio_header = "3"
    else:
        prio_header = "4"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": prio_header,
                "Tags": "house,tada",
                "Click": url,
            },
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert: {e}")


# ==========================
# Main dynamic monitor
# ==========================

def run_dynamic_once() -> None:
    apt_state: dict = load_json(APT_STATE_FILE)
    text_state: dict = load_json(TEXT_STATE_FILE)

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic {url}")

        try:
            page_text = fetch_rendered_text(url)
        except Exception as e:
            print(f"[ERROR] Failed to render {url}: {e}")
            continue

        if len(page_text) < 50:
            print(f"[WARN] Very short text for {url} ({len(page_text)} chars), skipping")
            continue

        extractor = SITE_EXTRACTORS.get(url, extract_generic_apartment_ids)
        new_apartments = extractor(page_text, url)

        if not new_apartments:
            print(f"[WARN] No apartments extracted for {url}")
            continue

        old_apartments = set(apt_state.get(url, []))

        if not old_apartments:
            print(f"[INIT] Recording baseline of {len(new_apartments)} apartments for {url}")
            apt_state[url] = sorted(new_apartments)
            text_state[url] = page_text
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        debug_print(
            f"[DIFF] {url}: {len(new_apartments)} current, "
            f"{len(added)} added, {len(removed)} removed"
        )

        if added or removed:
            summary = format_apartment_changes(added, removed)

            # High priority alerts when there are actual new listings
            if added and summary:
                send_ntfy_alert(url, summary, priority="high")

            # Optional: low priority alert if many removals
            elif not added and len(removed) > 5 and summary:
                send_ntfy_alert(url, summary, priority="low")

            apt_state[url] = sorted(new_apartments)
            text_state[url] = page_text
            changed_any = True
        else:
            print(f"[NOCHANGE] {url}")

    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_STATE_FILE, text_state)
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
