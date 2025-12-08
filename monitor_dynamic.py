#!/usr/bin/env python3
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Set

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# === FILES ===
APT_FILE = "dynamic_apartments.json"
TEXT_FILE = "dynamic_texts.json"
FAILURE_FILE = "dynamic_failures.json"
COOLDOWN_FILE = "dynamic_cooldowns.json"
LAST_ALERT_FILE = "dynamic_last_alert.json"

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()
DEBUG = os.environ.get("DEBUG", "false").lower() in ("true", "1", "yes")


def debug_print(msg: str) -> None:
    if DEBUG:
        print(msg)


def load_json(fname: str) -> Dict:
    p = Path(fname)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                print(f"[WARN] {fname} not a dict, resetting")
                return {}
            return data
    except json.JSONDecodeError as e:
        print(f"[ERROR] {fname} parse error: {e}, resetting")
        return {}


def save_json(fname: str, data: Dict) -> None:
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def track_failure(url: str) -> None:
    failures = load_json(FAILURE_FILE)
    failures[url] = failures.get(url, 0) + 1
    save_json(FAILURE_FILE, failures)


def reset_failure_count(url: str) -> None:
    failures = load_json(FAILURE_FILE)
    if url in failures:
        del failures[url]
        save_json(FAILURE_FILE, failures)


def cooldown_seconds(url: str) -> float:
    cooldowns = load_json(COOLDOWN_FILE)
    now = time.time()
    until = cooldowns.get(url, 0)
    return max(0.0, until - now)


def set_cooldown(url: str, seconds: float) -> None:
    cooldowns = load_json(COOLDOWN_FILE)
    cooldowns[url] = time.time() + seconds
    save_json(COOLDOWN_FILE, cooldowns)


def cleanup_playwright_tmp() -> None:
    if sync_playwright is None:
        return
    try:
        tmp_dir = Path("/tmp")
        for item in tmp_dir.glob("playwright-*"):
            try:
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    import shutil
                    shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass
        try:
            for tmp_path in tmp_dir.glob("*"):
                if tmp_path.name.startswith("tmp") and tmp_path.suffix in (".png", ".jpg"):
                    tmp_path.unlink()
        except Exception:
            pass
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
    until = cooldowns.get(url, 0)
    if now < until:
        wait = until - now
        print(f"[COOLDOWN] {url} on cooldown for {int(wait)}s, skipping")
        return None

    if sync_playwright is None:
        print(f"[ERROR] playwright not installed, can't fetch {url}")
        return None

    for attempt in range(1, max_retries + 1):
        try:
            cleanup_playwright_tmp()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )
                page.goto(url, wait_until="networkidle", timeout=45000)
                time.sleep(2)
                html = page.content()
                browser.close()
                debug_print(f"[dynamic] Rendered {url} successfully (attempt {attempt})")
                return html
        except Exception as e:
            debug_print(f"[dynamic] Fetch attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                print(f"[ERROR] All attempts failed for {url}: {e}")
                set_cooldown(url, 300)
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
    Filter out garbage: single words, short strings, malformed data.
    """
    if len(apt_id) < 4:
        return False
    if apt_id.lower() in {"unit", "rent", "property", "apartment", "view", "avenue", "street"}:
        return False
    # Reject if only letters (no numbers at all)
    if re.fullmatch(r"[a-z]+", apt_id, re.IGNORECASE):
        return False
    # Reject malformed mixed strings like "5pe" or "3xyz"
    if re.search(r'\d+[a-z]{2,}', apt_id, re.IGNORECASE):
        return False
    return True


def extract_apartment_ids(text: str, url: str) -> Set[str]:
    """
    Route to site-specific extractors based on domain.
    """
    if "iaffordny.com" in url or "afny.org" in url:
        return extract_ids_iafford_afny(text)
    if "residenewyork.com" in url:
        return extract_ids_reside(text)
    if "mgnyconsulting.com" in url:
        return extract_ids_mgny(text)
    if "fifthave.org" in url:
        return extract_ids_fifthave(text)
    if "cgmrcompliance.com" in url:
        return extract_ids_cgm(text)
    if "clintonmanagement.com" in url:
        return extract_ids_clinton(text)
    if "nyc.gov" in url:
        return extract_ids_nychpd(text)

    return set()


def extract_ids_clinton(text: str) -> Set[str]:
    """
    Clinton Management: looks for building + address patterns.
    """
    apartments: Set[str] = set()
    
    pattern = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?(?:Avenue|Street|Road|Place|Apartments?|Apartment))\s+(?=NYC|Brooklyn|Bronx|Queens|Manhattan|\$)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        apartments.add(match.group(1).strip())
    
    debug_print(f"[dynamic] clinton extracted {len(apartments)} ids")
    return apartments


def extract_ids_nychpd(text: str) -> Set[str]:
    """
    NYC HPD: similar to Clinton, building + street patterns.
    """
    apartments: Set[str] = set()
    
    pattern = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?(?:Avenue|Street|Road|Place|Apartments?|Apartment))\s+(?=Brooklyn|Bronx|Queens|Manhattan|\$)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        apartments.add(match.group(1).strip())
    
    debug_print(f"[dynamic] nychpd extracted {len(apartments)} ids")
    return apartments


def extract_ids_cgm(text: str) -> Set[str]:
    """
    CGM RCCompliance: building names with rent nearby.
    """
    apartments: Set[str] = set()
    
    pattern = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?(?:Apartments?|Apartment))\s+(?=\$|\d+\s+BR)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        apartments.add(match.group(1).strip())
    
    debug_print(f"[dynamic] cgm extracted {len(apartments)} ids")
    return apartments


def extract_ids_iafford_afny(text: str) -> Set[str]:
    """
    iAfford NY / AFNY: building names followed by property details.
    """
    apartments: Set[str] = set()
    
    pattern = re.compile(
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?(?:Apartments?|Apartment))\s+(?=\d+\s+Bedrooms?|\$)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        apartments.add(match.group(1).strip())
    
    debug_print(f"[dynamic] iafford/afny extracted {len(apartments)} ids")
    return apartments


def extract_ids_reside(text: str) -> Set[str]:
    """
    Reside NY: Building + optional unit, handling weird dash encoding.

    Examples in text:
      673 Hart Street Apartment – Unit 3A
      850 Flatbush Apartments – Unit 7A
      Flushing Preservation | 137-20 45th Avenue Apartment – Unit 4B
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
        r"(\d+\s+[A-Z][A-Za-z0-9 .,'-]+?(?:Avenue|Street|Road)\s+Apartments?)\s+(?=\$)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        apartments.add(match.group(1).strip())

    debug_print(f"[dynamic] mgny extracted {len(apartments)} ids")
    return apartments


def extract_ids_fifthave(text: str) -> Set[str]:
    """
    Fifth Ave Committee: Unit numbers like "Unit 20F", "Unit 3F", "Unit 617".
    Must be digits optionally followed by a single letter.
    """
    apartments: Set[str] = set()

    pattern = re.compile(r"(Unit\s+\d+[A-Z]?)\b", re.IGNORECASE)
    for match in pattern.finditer(text):
        apt = match.group(1).strip()
        # Additional validation: unit number should be reasonable (1-9999)
        unit_num = re.search(r'\d+', apt)
        if unit_num and 1 <= int(unit_num.group()) <= 9999:
            apartments.add(apt)

    if len(apartments) > 50:
        debug_print(
            f"[dynamic] fifthave extracted suspiciously many ({len(apartments)}) - likely noise"
        )
        return set()

    debug_print(f"[dynamic] fifthave extracted {len(apartments)} ids")
    return apartments


def format_apartment_changes(added: Set[str], removed: Set[str]) -> str:
    """
    Build alert message focusing on additions only.
    """
    lines = []
    if added:
        lines.append("New apartments detected:")
        for apt in sorted(added)[:30]:
            lines.append(f"+ {apt}")
        if len(added) > 30:
            lines.append(f"... and {len(added) - 30} more")

    if len(removed) > 5:
        lines.append("")
        lines.append("Apartments removed:")
        for apt in sorted(removed)[:30]:
            lines.append(f"- {apt}")
        if len(removed) > 30:
            lines.append(f"... and {len(removed) - 30} more")

    summary = "\n".join(lines)
    return summary


def send_ntfy_alert(url: str, summary: str, priority: str = "3") -> None:
    if not summary.strip():
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
    "https://unc-inc.com/affordable-housing/",
    "https://urbanhomewerks.com/current-opportunities",
    "https://urbanhomeworksaff.com/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    "https://www.whedco.org/real-estate/affordable-housing-rentals/",
]


def run_dynamic_once() -> None:
    text_state = load_json(TEXT_FILE)
    apt_state_raw = load_json(APT_FILE)
    apt_state: Dict[str, list] = {k: list(v) for k, v in apt_state_raw.items()}
    
    print(f"[INFO] Loaded state for {len(apt_state)} URLs")

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
        print(f"[INFO] State saved. Total URLs tracked: {len(apt_state)}")
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
