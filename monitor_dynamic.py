#!/usr/bin/env python3
"""
Dynamic site monitor.

Renders JavaScript heavy pages with Playwright, extracts apartment like
identifiers per site, compares sets to the previous run, and sends ntfy
alerts when apartments are added or removed.
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
FAILURE_FILE = ROOT / "dynamic_failures.json"
LAST_ALERT_FILE = ROOT / "dynamic_last_alert.json"
COOLDOWN_FILE = ROOT / "dynamic_cooldowns.json"

FAILURE_ALERT_THRESHOLD = 10
ALERT_COOLDOWN_HOURS = 24

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64 "
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
    """Atomic JSON write."""
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
    """
    Render with exponential backoff and simple cooldown to avoid hammering
    permanently broken or blocking sites.
    """
    cooldowns = load_json(COOLDOWN_FILE)
    now = time.time()

    cooldown_until = cooldowns.get(url)
    if isinstance(cooldown_until, (int, float)) and now < cooldown_until:
        debug_print(f"[dynamic] {url} in cooldown until {cooldown_until}")
        return None

    for attempt in range(max_retries + 1):
        timeout = int(30000 * (1.5 ** attempt))  # 30s, 45s, 67s
        jitter = random.uniform(1, 3)
        time.sleep(jitter)

        debug_print(
            f"[dynamic] Fetch attempt {attempt + 1} for {url}, timeout {timeout} ms"
        )

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=WEB_USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )

                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                page.wait_for_timeout(3000 + attempt * 2000)

                html = page.content()
                browser.close()

                if "forbidden" in html.lower() or "access denied" in html.lower():
                    raise RuntimeError("Site blocking detected")

                # Success, clear cooldown entry if present
                if url in cooldowns:
                    del cooldowns[url]
                    save_json(COOLDOWN_FILE, cooldowns)

                debug_print(f"[dynamic] HTML length for {url}: {len(html)}")
                return html

        except Exception as e:
            print(f"[WARN] Attempt {attempt + 1} failed for {url}: {e}")
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
            else:
                # Put site in 1 hour cooldown
                cooldowns[url] = time.time() + 3600
                save_json(COOLDOWN_FILE, cooldowns)

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
    """
    Filter out obvious noise while allowing real listings.

    Must contain either:
      - a digit, or
      - "unit" / "apartment" / "building".
    """
    if not apt_id or len(apt_id) < 5:
        return False

    if len(apt_id) > 200:
        return False

    has_number = bool(re.search(r"\d", apt_id))
    has_marker = bool(
        re.search(r"\b(?:unit|apartment|building)\b", apt_id, re.IGNORECASE)
    )

    if not (has_number or has_marker):
        return False

    noise_patterns = [
        r"^(menu|login|sign|subscribe|newsletter|cookie|privacy|terms|copyright)\b",
        r"^\d{1,2}:\d{2}",
        r"^page \d+",
    ]
    lowered = apt_id.lower()
    for pat in noise_patterns:
        if re.match(pat, lowered):
            return False

    return True


def track_failure(url: str) -> None:
    failures = load_json(FAILURE_FILE)
    last_alerts = load_json(LAST_ALERT_FILE)

    failures[url] = failures.get(url, 0) + 1

    if failures[url] >= FAILURE_ALERT_THRESHOLD:
        last_alert = float(last_alerts.get(url, 0))
        hours_since = (time.time() - last_alert) / 3600 if last_alert else 999

        if hours_since >= ALERT_COOLDOWN_HOURS:
            msg = (
                f"Site has failed {failures[url]} consecutive checks.\n"
                f"Possible causes:\n"
                f"- Site blocking bot traffic\n"
                f"- Site redesign\n"
                f"- Extractor pattern needs update"
            )
            send_ntfy_alert(url, msg, priority="3")
            last_alerts[url] = time.time()
            save_json(LAST_ALERT_FILE, last_alerts)

    save_json(FAILURE_FILE, failures)


def reset_failure_count(url: str) -> None:
    failures = load_json(FAILURE_FILE)
    if url in failures and failures[url] > 0:
        failures[url] = 0
        save_json(FAILURE_FILE, failures)


# ---------------------------------------------------------------------
# Site specific extractors
# ---------------------------------------------------------------------


def extract_ids_iafford_afny(text: str) -> Set[str]:
    """
    iafford and AFNY: Extract building names that appear just before 'Rent:'.

    Example:
      The Urban 144-74 Northern Boulevard -Multiple Units
      Rent: $2,104.89 - $2,162.77
    """
    apartments: Set[str] = set()

    pattern = re.compile(
        r"(?:^|\n)([A-Z][^\n]+?)(?:\s+\d{4})?\s*\n?\s*Rent:",
        re.MULTILINE | re.DOTALL,
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        name = name.replace("Â", "").replace("â€", "-")

        name = re.sub(r"\s+-\s*$", "", name)
        name = re.sub(r"\s+\d{4}$", "", name)

        if len(name) > 10:
            apartments.add(name)

    debug_print(f"[dynamic] iafford/afny extracted {len(apartments)} ids")
    return apartments


def extract_ids_reside(text: str) -> Set[str]:
    """
    Reside NY: Building + optional unit, handling weird dash encoding.

    Examples in text:
      673 Hart Street Apartment â€" Unit 3A
      850 Flatbush Apartments â€" Unit 7A
      Flushing Preservation | 137-20 45th Avenue Apartment â€" Unit 4B
    """
    apartments: Set[str] = set()

    text = text.replace("â€", "-").replace("—", "-").replace("–", "-")

    pattern1 = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?(?:Apartments?|Apartment)?)\s*-\s*Unit\s+([A-Z0-9]+)",
        re.IGNORECASE,
    )
    for match in pattern1.finditer(text):
        building, unit = match.groups()
        apartments.add(f"{building.strip()} - Unit {unit}")

    pattern2 = re.compile(
        r"([A-Z][A-Za-z0-9 .,'-]+?)\s*\|\s*(\d+[^\n]+?)\s*-\s*Unit\s+([A-Z0-9]+)",
        re.IGNORECASE,
    )
    for match in pattern2.finditer(text):
        name, addr, unit = match.groups()
        apartments.add(f"{name.strip()} {addr.strip()} - Unit {unit}")

    pattern3 = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?(?:Apartments?|Apartment))\s+(?=Affordable|Open Market|\$)",
        re.IGNORECASE,
    )
    for match in pattern3.finditer(text):
        apartments.add(match.group(1).strip())

    debug_print(f"[dynamic] ResideNY extracted {len(apartments)} ids")
    return apartments


def extract_ids_mgny(text: str) -> Set[str]:
    """
    MGNY: Building names like "2010 Walton Avenue Apartments" with nearby rent.
    """
    apartments: Set[str] = set()

    pattern = re.compile(
        r"(\d+\s+[A-Z][A-Za-z ]{5,}?\s+Apartments)[^\n]{0,100}?\$\s*\d",
        re.IGNORECASE,
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        if re.match(r"\d+\s+[A-Z][a-z]+\s+[A-Z]", name):
            apartments.add(name)

    debug_print(f"[dynamic] MGNY extracted {len(apartments)} ids")
    return apartments


def extract_ids_generic(text: str) -> Set[str]:
    """
    Generic fallback: look for address like strings near rent phrases.
    """
    apartments: Set[str] = set()

    chunks = re.split(r"(?=Rent:|\$\s*\d{3,})", text)

    addr_pattern = re.compile(
        r"\b(\d+\s+[A-Z][A-Za-z]{2,}\s+"
        r"(?:Street|Avenue|Road|Boulevard|Drive|Place|Court|Way|Lane)[^.\n]{0,30})",
        re.IGNORECASE,
    )

    for chunk in chunks:
        if not re.search(r"(Rent:|Monthly Rent|\$\s*\d{3,})", chunk):
            continue

        match = addr_pattern.search(chunk[:200])
        if match:
            addr = match.group(1).strip()
            rent_match = re.search(r"\$\s*([\d,]+)", chunk)
            if rent_match:
                apartments.add(f"{addr} ${rent_match.group(1)}")
            else:
                apartments.add(addr)

    debug_print(f"[dynamic] generic extracted {len(apartments)} ids")
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
        print(f"[INFO] No summary for {url}, no alert sent")
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
            track_failure(url)
            continue

        reset_failure_count(url)

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
