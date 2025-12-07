#!/usr/bin/env python3
"""
Dynamic site monitor.

Uses Playwright to render JavaScript heavy pages, extracts apartment-like
identifiers, compares them to previous runs, and sends ntfy alerts only
when apartments are added or removed.

Relies on env:
    NTFY_TOPIC_URL   - ntfy topic URL
    DEBUG            - "true" for extra logging
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

ROOT = Path(__file__).parent

TEXT_FILE = ROOT / "dynamic_texts.json"
APT_FILE = ROOT / "dynamic_apartments.json"

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


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
    """Atomic JSON write to avoid corrupt state."""
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


def fetch_rendered_html(url: str, max_retries: int = 2) -> Optional[str]:
    """Render a page with Playwright and return HTML or None."""
    for attempt in range(max_retries + 1):
        timeout = 45000 + attempt * 15000
        jitter = random.uniform(2, 5)
        time.sleep(jitter)

        debug_print(f"[dynamic] Fetch attempt {attempt + 1} for {url}, timeout {timeout} ms")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=WEB_USER_AGENT,
                viewport={"width": 1200, "height": 900},
                locale="en-US",
            )
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                page.wait_for_timeout(5000 + attempt * 2000)

                html = page.content()
                if "forbidden" in html.lower() or "access denied" in html.lower():
                    raise RuntimeError("Site blocking detected")

                browser.close()
                debug_print(f"[dynamic] HTML length for {url}: {len(html)}")
                return html
            except Exception as e:
                print(f"[WARN] Attempt {attempt + 1} failed for {url}: {e}")
                browser.close()
                if attempt < max_retries:
                    time.sleep(5)
                else:
                    print(f"[ERROR] Giving up on {url} after {attempt + 1} attempts")
    return None


def fetch_rendered_text(url: str) -> Optional[str]:
    html = fetch_rendered_html(url)
    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw text length for {url}: {len(raw_text)}")

    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    text = "\n".join(lines)
    text = normalize_whitespace(text)

    debug_print(f"[dynamic] Normalized text length for {url}: {len(text)}")
    debug_print(f"[dynamic] Sample for {url}: {text[:300]}")
    return text


def is_valid_apartment_id(apt_id: str) -> bool:
    """Filter out obvious noise."""
    if not apt_id:
        return False

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
        "subscribe",
        "newsletter",
    ]
    lowered = apt_id.lower()
    if any(word in lowered for word in noise_words):
        return False

    return True


# ---------------------------------------------------------------------
# Site specific extractors
# ---------------------------------------------------------------------


def extract_ids_iafford_afny(text: str) -> Set[str]:
    """
    iafford and AFNY show buildings as lines that end before 'Rent:'.
    We use the portion before 'Rent:' as the ID and only strip a trailing
    four digit code like '0825'. We keep 'Multiple Units' and 'Unit 3F'.
    """
    apartments: Set[str] = set()

    pattern = re.compile(
        r"(?:^|\n)([A-Z][^\n]+?)\s+Rent:",
        re.MULTILINE,
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        # Remove trailing 4 digit codes, keep other descriptors
        name = re.sub(r"\s+\d{4}$", "", name).strip()
        apartments.add(name)

    debug_print(f"[dynamic] iafford/afny buildings: {len(apartments)}")
    return apartments


def extract_ids_reside(text: str) -> Set[str]:
    """
    Reside NY: use building names on the open market page.
    """
    apartments: Set[str] = set()

    pattern = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?)(?:\s+Apartments|\s+-|\s+Unit\b)",
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        apartments.add(name)

    debug_print(f"[dynamic] ResideNY ids: {len(apartments)}")
    return apartments


def extract_ids_mgny(text: str) -> Set[str]:
    """
    MGNY Listings: '2010 Walton Avenue Apartments' style.
    """
    apartments: Set[str] = set()

    pattern = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?\s+Apartments)",
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        apartments.add(name)

    debug_print(f"[dynamic] MGNY ids: {len(apartments)}")
    return apartments


def extract_ids_generic(text: str) -> Set[str]:
    """
    Generic fallback for sites we have not tuned yet.
    Tries to pull out address-like strings that contain numbers plus a name.
    """
    apartments: Set[str] = set()

    for match in re.finditer(
        r"\b(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?)\b.*?\$\s*([\d,]+)",
        text,
    ):
        address, rent = match.groups()
        rent_clean = rent.replace(",", "")
        apartments.add(f"{address} ${rent_clean}")

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
    for domain, extractor in SITE_EXTRACTORS.items():
        if domain in url_lower:
            ids = extractor(text)
            debug_print(
                f"[dynamic] extractor {extractor.__name__} for {url} produced {len(ids)} raw ids"
            )
            return ids

    ids = extract_ids_generic(text)
    debug_print(f"[dynamic] extractor generic for {url} produced {len(ids)} raw ids")
    return ids


def format_apartment_changes(added: Set[str], removed: Set[str]) -> Optional[str]:
    if not added and not removed:
        return None

    parts = []

    if added:
        parts.append("New apartments detected:")
        for apt in sorted(added)[:10]:
            parts.append(f"  + {apt}")
        if len(added) > 10:
            parts.append(f"  ... and {len(added) - 10} more")

    if removed:
        parts.append("")
        parts.append("Apartments removed:")
        for apt in sorted(removed)[:5]:
            parts.append(f"  - {apt}")
        if len(removed) > 5:
            parts.append(f"  ... and {len(removed) - 5} more")

    summary = "\n".join(parts)
    if not summary.strip():
        return None

    return summary


def send_ntfy_alert(url: str, summary: str, priority: str = "4") -> None:
    if not summary:
        print(f"[INFO] No summary content for {url}, no alert sent")
        return

    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set, would have sent:")
        print(summary)
        return

    body = f"{url}\n\n{summary}"
    headers = {
        "Title": "Housing listings updated",
        "Priority": priority,
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
            print(f"[OK] ntfy alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert for {url}: {e}")


DYNAMIC_URLS = [
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://afny.org/re-rentals",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://city5.nyc/",
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://fifthave.org/re-rental-availabilities/",
    "https://iaffordny.com/re-rentals",
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
    "https://yourneighborhoodhousing.com/",
]


def run_dynamic_once() -> None:
    text_state = load_json(TEXT_FILE)
    apt_state_raw = load_json(APT_FILE)

    apt_state: Dict[str, list] = {k: list(v) for k, v in apt_state_raw.items()}

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic site {url}")
        text = fetch_rendered_text(url)
        if text is None:
            continue

        new_apartments_raw = extract_apartment_ids(text, url)
        new_apartments = {a for a in new_apartments_raw if is_valid_apartment_id(a)}

        if not new_apartments and ("rent" in text.lower() or "apartment" in text.lower()):
            print(
                f"[WARN] {url} appears to have rental content but extracted 0 apartments. "
                "Extractor or validation may be too strict."
            )
            debug_print(f"[dynamic] text sample for {url}: {text[:500]}")

        old_list = apt_state.get(url, [])
        old_apartments = set(old_list)

        if not old_apartments:
            print(f"[INIT] Baseline apartment set for {url}: {len(new_apartments)} units")
            apt_state[url] = sorted(new_apartments)
            text_state[url] = text
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if not added and not removed:
            print(f"[NOCHANGE] {url} - same apartment set")
            continue

        print(f"[CHANGE] {url}: +{len(added)} / -{len(removed)}")

        summary = format_apartment_changes(added, removed)

        if added and summary:
            send_ntfy_alert(url, summary, priority="4")
        elif len(removed) > 5 and summary:
            send_ntfy_alert(url, summary, priority="2")

        apt_state[url] = sorted(new_apartments)
        text_state[url] = text
        changed_any = True

    if changed_any:
        save_json(APT_FILE, apt_state)
        save_json(TEXT_FILE, text_state)
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
