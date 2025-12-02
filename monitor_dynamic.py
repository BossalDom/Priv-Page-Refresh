import os
import json
import time
import random
import re
import hashlib
from pathlib import Path
from typing import Dict, Set, List

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =========================================
# Config
# =========================================

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

DATA_DIR = Path(".")
APT_STATE_FILE = DATA_DIR / "dynamic_apartments.json"
TEXT_STATE_FILE = DATA_DIR / "dynamic_texts.json"

# Dynamic sites you are monitoring.
# Add or remove URLs as needed.
DYNAMIC_URLS: List[str] = [
    "https://mgnyconsulting.com/listings/",
    "https://ibis.powerappsportals.com/",
    "https://afny.org/re-rentals",
    "https://iaffordny.com/re-rentals",
]

# =========================================
# Helpers
# =========================================


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


def load_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not load {path}: {e}")
        return {}


def save_json(path: Path, data: Dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def normalize_whitespace(text: str) -> str:
    # Collapse internal whitespace but preserve newlines that separate blocks.
    lines = []
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


# =========================================
# Fetching with Playwright
# =========================================


def fetch_rendered_text(url: str) -> str:
    """Render a dynamic page with Playwright and return cleaned text."""
    # Small random delay to avoid obvious polling patterns
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

        html = ""
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(5000)
            html = page.content()
        finally:
            browser.close()

    if not html:
        raise RuntimeError("Empty HTML from Playwright")

    if "forbidden" in html.lower() or "access denied" in html.lower():
        raise RuntimeError("Site appears to be blocking the request")

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw HTML text length for {url}: {len(raw_text)}")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = normalize_whitespace(text)
    debug_print(f"[dynamic] Normalized text length for {url}: {len(text)}")

    return text


# =========================================
# Site specific apartment extractors
# =========================================


def extract_mgny(text: str) -> Set[str]:
    """
    MGNY: mgnyconsulting.com/listings/
    Treat each building (Walton Avenue Apartments etc.) as one listing.
    Ignore per unit details so small changes do not churn IDs.
    """
    apartments: Set[str] = set()

    # Lines that look like "2010 Walton Avenue Apartments" etc.
    pattern = re.compile(
        r"\b\d{3,5}\s+[A-Z][A-Za-z0-9 .,'-]+?Apartments\b", re.IGNORECASE
    )
    for match in pattern.finditer(text):
        name = " ".join(match.group(0).split())
        apartments.add(name)

    # Fallback: any line ending with "Apartments"
    if not apartments:
        for line in text.splitlines():
            line = line.strip()
            if len(line) > 25 and "apartments" in line.lower():
                if not line.lower().startswith(("home", "about", "contact")):
                    apartments.add(line)

    debug_print(f"[dynamic-mgny] extracted {len(apartments)} ids")
    return apartments


def extract_iafford_or_afny(text: str) -> Set[str]:
    """
    iAffordNY and AFNY re-rentals.
    Use building name plus rent range as ID.
    """
    apartments: Set[str] = set()

    # Example pattern:
    # "The Urban 144-74 Northern Boulevard - Multiple Units Rent: $2,104.89 - $2,162.77"
    pattern = re.compile(
        r"([A-Z][A-Za-z0-9 .,'-]+?)\s+Rent:\s*\$([\d,]+(?:\s*-\s*\$[\d,]+)?)",
        re.IGNORECASE,
    )

    for match in pattern.finditer(text):
        name, rent = match.groups()
        name = " ".join(name.split())
        rent_clean = rent.replace(" ", "")
        apartments.add(f"{name} Rent:{rent_clean}")

    # If that finds nothing, fall back to card like chunks
    if not apartments:
        apartments |= extract_generic_cards(text)

    debug_print(f"[dynamic-iafford/afny] extracted {len(apartments)} ids")
    return apartments


def extract_ibis(text: str) -> Set[str]:
    """
    IBIS PowerApps portals.
    Cards usually contain short codes like '1BR-Apartment Bright-9-01'.
    """
    apartments: Set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"\d+BR[- ]Apartment", line, re.IGNORECASE):
            apartments.add(line)

    if not apartments:
        apartments |= extract_generic_cards(text)

    debug_print(f"[dynamic-ibis] extracted {len(apartments)} ids")
    return apartments


def extract_generic_cards(text: str) -> Set[str]:
    """
    Generic fallback: treat each card-sized block as a listing.
    A block is a pair of non-trivial lines that includes rent or household info.
    """
    apartments: Set[str] = set()

    blocks = text.split("\n\n")
    for block in blocks:
        cleaned = " ".join(block.split())
        if len(cleaned) < 40:
            continue
        if "$" not in cleaned:
            continue
        if not any(
            key in cleaned.lower()
            for key in ["rent", "household", "income", "apartment", "unit"]
        ):
            continue
        apartments.add(cleaned)

    return apartments


SITE_EXTRACTORS = {
    "https://mgnyconsulting.com/listings/": extract_mgny,
    "https://iaffordny.com/re-rentals": extract_iafford_or_afny,
    "https://afny.org/re-rentals": extract_iafford_or_afny,
    "https://ibis.powerappsportals.com/": extract_ibis,
}


def extract_apartment_ids(text: str, url: str) -> Set[str]:
    extractor = SITE_EXTRACTORS.get(url, extract_generic_cards)
    apartments = extractor(text)
    debug_print(f"[dynamic] {url} -> {len(apartments)} apartment ids")
    return apartments


# =========================================
# Diffing and notifications
# =========================================


def format_apartment_changes(
    added: Set[str], removed: Set[str]
) -> str | None:
    if not added and not removed:
        return None

    parts: List[str] = []

    if added:
        parts.append("🔵 NEW LISTINGS:")
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

    summary = "\n".join(parts)
    return summary if summary.strip() else None


def send_ntfy_alert(url: str, diff_summary: str | None) -> None:
    if not diff_summary:
        print(f"[INFO] No meaningful changes on {url}")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set")
        print(f"[ALERT] Would have notified for {url}:\n{diff_summary}")
        # Fail the job so it is visible in Actions
        raise ValueError("NTFY_TOPIC_URL environment variable is not configured")

    body = f"{url}\n\n{diff_summary}"
    title = "🏠 New housing listings"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8", errors="ignore"),
            headers={
                "Title": title.encode("utf-8", errors="ignore"),
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
    except Exception as e:
        print(f"[ERROR] Sending alert: {e}")
        raise


# =========================================
# Main dynamic monitor
# =========================================


def run_dynamic_once() -> None:
    apt_state: Dict[str, List[str]] = load_json(APT_STATE_FILE)
    text_state: Dict[str, str] = load_json(TEXT_STATE_FILE)

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic site {url}")

        try:
            new_text = fetch_rendered_text(url)
        except Exception as e:
            print(f"[ERROR] Failed to render {url}: {e}")
            continue

        if len(new_text) < 50:
            print(f"[WARN] {url} returned very short text ({len(new_text)} chars), skipping")
            continue

        new_apartments = extract_apartment_ids(new_text, url)
        old_apartments_list = apt_state.get(url, [])
        old_apartments = set(old_apartments_list)

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
                f"[CHANGE] {url}: "
                f"+{len(added)} apartments, -{len(removed)} apartments"
            )

            if added:
                debug_print(f"[dynamic] sample added on {url}: {list(added)[:2]}")
            if removed:
                debug_print(f"[dynamic] sample removed on {url}: {list(removed)[:2]}")

            # Only alert if there are new apartments.
            diff_summary = format_apartment_changes(added, removed)
            if added and diff_summary:
                send_ntfy_alert(url, diff_summary)

            apt_state[url] = sorted(new_apartments)
            text_state[url] = new_text
            changed_any = True
        else:
            print(f"[NOCHANGE] {url} - same apartment set")

    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_STATE_FILE, text_state)
    else:
        print("[INFO] No dynamic changes to save")


if __name__ == "__main__":
    run_dynamic_once()
