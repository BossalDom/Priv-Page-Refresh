#!/usr/bin/env python3
"""
Dynamic housing monitor.

Targets pages that list apartments (iafford, AFNY, Reside, MGNY, etc),
extracts a stable set of "apartment ids", and sends an ntfy alert when
the set changes (new listings added or existing ones removed).

Relies on env:
    NTFY_TOPIC_URL   – ntfy topic URL
    DEBUG            – "true" to print extra logs
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Set

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

ROOT = Path(__file__).parent

APT_STATE_FILE = ROOT / "dynamic_apartments.json"
TEXT_STATE_FILE = ROOT / "dynamic_texts.json"

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DYNAMIC_URLS = [
    # Core listing pages
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://residenewyork.com/property-status/open-market/",
    "https://mgnyconsulting.com/listings/",
    # JS-heavy / portal pages
    "https://city5.nyc/",
    "https://ibis.powerappsportals.com/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    # Add more apartment-listing URLs here
]

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


def load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Could not read {path}: {e}")
        return {}


def save_json(path: Path, data: Dict[str, object]) -> None:
    """Atomic JSON write to avoid corrupting state on crashes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        shutil.move(str(tmp_path), str(path))
    except Exception as e:
        print(f"[ERROR] Could not save {path}: {e}")
        try:
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------
# Content filters for specific sites
# ---------------------------------------------------------------------


def filter_resideny_open_market(text: str) -> str:
    marker = "Open Market"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


def filter_mgny(text: str) -> str:
    marker = "Listings"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://mgnyconsulting.com/listings/": filter_mgny,
}


def apply_content_filters(url: str, text: str) -> str:
    fn = CONTENT_FILTERS.get(url)
    if fn:
        text = fn(text)
    return text


# ---------------------------------------------------------------------
# Playwright fetch
# ---------------------------------------------------------------------


def fetch_rendered_text(url: str, max_retries: int = 2) -> Optional[str]:
    """
    Use Playwright to render the page.

    Retries with increasing timeouts for heavier JS sites.
    """
    html: Optional[str] = None

    for attempt in range(max_retries + 1):
        delay = random.uniform(2, 5)
        print(
            f"[INFO] Waiting {delay:.1f}s before fetching {url} "
            f"(attempt {attempt + 1})"
        )
        time.sleep(delay)

        timeout = 45000 + attempt * 15000  # 45 s, 60 s, 75 s

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=WEB_HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                page.wait_for_timeout(5000 + attempt * 2000)
                html = page.content()
                browser.close()
                break
            except Exception as e:
                print(f"[WARN] Attempt {attempt + 1} failed for {url}: {e}")
                browser.close()
                if attempt < max_retries:
                    time.sleep(5)
                else:
                    return None

    if not html:
        return None

    if "forbidden" in html.lower() or "access denied" in html.lower():
        print(f"[WARN] {url} appears blocked when fetching")
        return None

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw text length for {url}: {len(raw_text)}")

    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    text = "\n".join(lines)
    text = apply_content_filters(url, text)
    text = normalize_whitespace(text)
    debug_print(f"[dynamic] Filtered text length for {url}: {len(text)}")

    return text


# ---------------------------------------------------------------------
# Apartment-id extraction
# ---------------------------------------------------------------------


def extract_ids_iafford_afny(text: str) -> Set[str]:
    """
    iafford and AFNY patterns.

    These sites usually show listings as lines like:

        'The Urban 144-74 Northern Boulevard -Multiple Units Rent: $2,104.89'
        '3508 Tryon Avenue Unit 6D 1125 Rent: $1,680'

    We treat everything up to 'Rent:' as the stable identifier and
    strip a few volatile suffixes.
    """
    apartments: Set[str] = set()

    pattern = re.compile(
        r"(?:^|\n)([A-Z][^\n]+?)\s+Rent:\s*\$[\d,]+",
        re.MULTILINE,
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        # Strip some noisy endings
        name = re.sub(
            r"\s+(Multiple Units|Unit\s+\w+|\d{4})$",
            "",
            name,
        ).strip()
        apartments.add(name)

    debug_print(f"[dynamic] iafford/afny buildings: {len(apartments)}")
    return apartments


def extract_ids_reside(text: str) -> Set[str]:
    """
    Reside New York open market page.

    We look for lines that clearly combine building + 'Unit'.
    """
    apartments: Set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # crude but effective: building name + 'Unit'
        if "Apartments" in line and "Unit" in line:
            apartments.add(line)

    debug_print(f"[dynamic] ResideNY: {len(apartments)}")
    return apartments


def extract_ids_mgny(text: str) -> Set[str]:
    """
    MGNY consulting listings.

    We treat each line that looks like '123 Main Street Apartments'
    or includes 'Unit' as a separate id.
    """
    apartments: Set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "Apartments" in line or "Apartment" in line:
            apartments.add(line)
        elif re.search(r"\bUnit\b", line):
            apartments.add(line)

    debug_print(f"[dynamic] MGNY: {len(apartments)}")
    return apartments


def extract_ids_generic(text: str) -> Set[str]:
    """
    Fallback extraction: lines with obvious rental keywords.
    """
    apartments: Set[str] = set()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(
            kw in s.lower()
            for kw in ("unit", "apartment", "apt", "bedroom", "studio", "rent")
        ):
            apartments.add(s)

    debug_print(f"[dynamic] generic ids: {len(apartments)}")
    return apartments


SITE_EXTRACTORS = {
    "iaffordny.com": extract_ids_iafford_afny,
    "afny.org": extract_ids_iafford_afny,
    "residenewyork.com": extract_ids_reside,
    "mgnyconsulting.com": extract_ids_mgny,
}


def extract_apartment_ids(text: str, url: str) -> Set[str]:
    url_lower = url.lower()
    for domain, func in SITE_EXTRACTORS.items():
        if domain in url_lower:
            return func(text)
    return extract_ids_generic(text)


def is_valid_apartment_id(apt_id: str) -> bool:
    """
    Sanity-check extracted ids so nav/footer junk does not count as apartments.
    """
    # Needs at least one digit (address or unit)
    if not re.search(r"\d", apt_id):
        return False

    if len(apt_id) > 160:
        return False

    noise_words = [
        "cookie",
        "privacy",
        "terms",
        "copyright",
        "menu",
        "login",
        "sign up",
        "newsletter",
    ]
    lower = apt_id.lower()
    if any(w in lower for w in noise_words):
        return False

    return True


# ---------------------------------------------------------------------
# Diff + ntfy
# ---------------------------------------------------------------------


def format_apartment_changes(
    added: Set[str],
    removed: Set[str],
    max_added: int = 10,
    max_removed: int = 5,
) -> Optional[str]:
    if not added and not removed:
        return None

    parts = []

    if added:
        parts.append("NEW LISTINGS:")
        for apt in sorted(added)[:max_added]:
            parts.append(f"  • {apt}")
        if len(added) > max_added:
            parts.append(f"  • ... and {len(added) - max_added} more")

    if removed:
        parts.append("")
        parts.append("REMOVED:")
        for apt in sorted(removed)[:max_removed]:
            parts.append(f"  • {apt}")
        if len(removed) > max_removed:
            parts.append(f"  • ... and {len(removed) - max_removed} more")

    return "\n".join(parts).strip() or None


def send_ntfy_alert(url: str, diff_summary: str) -> None:
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set – would have sent alert")
        print(diff_summary)
        return

    body = f"{url}\n\n{diff_summary}"

    headers = {
        # ASCII only, to avoid latin-1 header encoding failures
        "Title": "New housing listings",
        "Priority": "4",
        "Tags": "housing,monitor",
        "Click": url,
    }

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert for {url}: {e}")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def run_dynamic_once() -> None:
    apt_state_raw = load_json(APT_STATE_FILE)
    text_state_raw = load_json(TEXT_STATE_FILE)

    # ensure dict[str, list[str]]
    apt_state: Dict[str, list] = {
        k: list(v) if isinstance(v, list) else [] for k, v in apt_state_raw.items()
    }
    text_state: Dict[str, str] = {
        k: str(v) for k, v in text_state_raw.items()
    }

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic site {url}")
        text = fetch_rendered_text(url)
        if text is None:
            print(f"[WARN] No text extracted for {url}")
            continue

        new_apartments = extract_apartment_ids(text, url)
        new_apartments = {a for a in new_apartments if is_valid_apartment_id(a)}
        debug_print(
            f"[dynamic] {url} final apartment count after validation: "
            f"{len(new_apartments)}"
        )

        old_list = apt_state.get(url, [])
        old_apartments = set(old_list)

        if not old_apartments:
            print(
                f"[INIT] Recording {len(new_apartments)} apartments for baseline on {url}"
            )
            apt_state[url] = sorted(new_apartments)
            text_state[url] = text
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if not added and not removed:
            print(f"[NOCHANGE] {url} – apartments unchanged")
            continue

        print(
            f"[CHANGE] {url}: +{len(added)} apartments, "
            f"-{len(removed)} apartments"
        )

        if added:
            print(f"  Added sample: {list(added)[:3]}")
        if removed:
            print(f"  Removed sample: {list(removed)[:3]}")

        diff_summary = format_apartment_changes(added, removed)

        # To reduce noise, only send alert when there is at least one new listing
        if added and diff_summary:
            send_ntfy_alert(url, diff_summary)

        apt_state[url] = sorted(new_apartments)
        text_state[url] = text
        changed_any = True

    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_STATE_FILE, text_state)
    else:
        print("[INFO] No dynamic state changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
