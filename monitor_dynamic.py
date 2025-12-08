#!/usr/bin/env python3
"""
Dynamic site monitor - FIXED VERSION
Properly extracts apartment listings from each site based on their actual format.
"""
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
    except Exception:
        pass


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fetch_rendered_html(url: str, max_retries: int = 2) -> Optional[str]:
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
    return text


# =============================================================================
# APARTMENT EXTRACTION - Site-specific extractors
# =============================================================================

def extract_apartment_ids(text: str, url: str) -> Set[str]:
    """Route to site-specific extractors based on domain."""
    
    # Normalize encoding issues
    text = text.replace("Â", " ").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    
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
        return set()  # This is a directory page, not listings
    if "nychdc.com" in url:
        return extract_ids_nychdc(text)
    if "prontohousingrentals.com" in url:
        return extract_ids_pronto(text)
    if "ahgleasing.com" in url:
        return extract_ids_ahg(text)
    if "sjpny.com" in url:
        return extract_ids_sjp(text)
    if "langsampropertyservices.com" in url:
        return extract_ids_langsam(text)
    if "springmanagement.net" in url:
        return extract_ids_spring(text)
    if "sbmgmt.sitemanager.rentmanager.com" in url:
        return extract_ids_reclaim(text)
    if "tfc.com" in url:
        return extract_ids_tfc(text)
    if "wavecrestrentals.com" in url:
        return extract_ids_wavecrest(text)
    if "riseboro.org" in url:
        return extract_ids_riseboro(text)

    # Generic fallback
    return extract_ids_generic(text)


def extract_ids_iafford_afny(text: str) -> Set[str]:
    """
    iAfford NY / AFNY: Extract address + unit combinations.
    
    Format examples from actual site:
    - "3508 Tryon Avenue Unit 6D 1125"
    - "555 Waverly Avenue- Multiple Units 0825"
    - "The Urban 144-74 Northern Boulevard -Multiple Units"
    - "1759-63 West Farms Road Apartments - 0925 Unit 6I"
    """
    apartments: Set[str] = set()
    
    # Pattern 1: Street address with unit number
    # "3508 Tryon Avenue Unit 6D" or "536 East 183rd Street Apartments - 1125 Unit 3F"
    pattern1 = re.compile(
        r'(\d+(?:-\d+)?\s+[A-Za-z0-9 ]+?(?:Street|Avenue|Road|Boulevard|Place|Drive|Pkwy|Parkway))'
        r'(?:\s+Apartments?)?'
        r'(?:\s*[-–]\s*(?:Multiple\s+Units|\d{4}))?\s*'
        r'(?:Unit\s+([A-Z0-9]{1,5}))?',
        re.IGNORECASE
    )
    
    for match in pattern1.finditer(text):
        address = match.group(1).strip()
        unit = match.group(2)
        if unit:
            apt_id = f"{address} Unit {unit}"
        else:
            apt_id = address
        # Clean up
        apt_id = re.sub(r'\s+', ' ', apt_id).strip()
        if len(apt_id) >= 10:  # Reasonable minimum
            apartments.add(apt_id)
    
    # Pattern 2: Named buildings like "The Urban" or "THE AURA"
    pattern2 = re.compile(
        r'(The\s+[A-Z][a-z]+|THE\s+[A-Z]+)\s+'
        r'(\d+(?:-\d+)?\s+[A-Za-z0-9 ]+?(?:Street|Avenue|Boulevard|Road))',
        re.IGNORECASE
    )
    for match in pattern2.finditer(text):
        name = match.group(1).strip()
        address = match.group(2).strip()
        apt_id = f"{name} {address}"
        apartments.add(apt_id)
    
    # Pattern 3: Just unit references with context (for sites that list units separately)
    # Look for "Unit XY" where XY is alphanumeric
    pattern3 = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Street|Avenue|Road|Place))[^U]*Unit\s+([A-Z0-9]{1,5})',
        re.IGNORECASE
    )
    for match in pattern3.finditer(text):
        address = match.group(1).strip()
        unit = match.group(2)
        apt_id = f"{address} Unit {unit}"
        apt_id = re.sub(r'\s+', ' ', apt_id).strip()
        apartments.add(apt_id)

    debug_print(f"[dynamic] iafford/afny extracted {len(apartments)} ids")
    return apartments


def extract_ids_reside(text: str) -> Set[str]:
    """
    Reside NY: Building address + Unit number.
    
    Format: "673 Hart Street Apartment – Unit 3A"
            "Flushing Preservation | 137-20 45th Avenue Apartment – Unit 2X"
    """
    apartments: Set[str] = set()
    
    # Normalize dashes
    text = text.replace("–", "-").replace("—", "-")
    
    # Pattern 1: "Address Apartment(s) - Unit X"
    pattern1 = re.compile(
        r'(\d+(?:-\d+)?\s+[A-Za-z0-9 ]+?(?:Street|Avenue|Road|Boulevard|Place|Ave|St|Blvd))'
        r'\s+Apartments?\s*-\s*Unit\s+([A-Z0-9]{1,5})',
        re.IGNORECASE
    )
    for match in pattern1.finditer(text):
        address = match.group(1).strip()
        unit = match.group(2).upper()
        apt_id = f"{address} - Unit {unit}"
        apartments.add(re.sub(r'\s+', ' ', apt_id))
    
    # Pattern 2: "Building | Address - Unit X"
    pattern2 = re.compile(
        r'([A-Za-z ]+)\s*\|\s*(\d+[^-]+)\s*-\s*Unit\s+([A-Z0-9]{1,5})',
        re.IGNORECASE
    )
    for match in pattern2.finditer(text):
        name = match.group(1).strip()
        addr = match.group(2).strip()
        unit = match.group(3).upper()
        apt_id = f"{name} | {addr} - Unit {unit}"
        apartments.add(re.sub(r'\s+', ' ', apt_id))
    
    debug_print(f"[dynamic] ResideNY extracted {len(apartments)} ids")
    return apartments


def extract_ids_mgny(text: str) -> Set[str]:
    """
    MGNY: Extract building addresses.
    
    Format: "2547 Cruger Avenue 2547 Cruger Avenue, Bronx, NY 10467 $63,134"
    """
    apartments: Set[str] = set()
    
    # Pattern: Address followed by full address with city/zip
    pattern = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Street|Avenue|Road|Boulevard|Place))\s+'
        r'\d+\s+[A-Za-z ]+,\s*(?:Bronx|Brooklyn|Queens|Manhattan|New York|Far Rockaway)',
        re.IGNORECASE
    )
    
    for match in pattern.finditer(text):
        address = match.group(1).strip()
        address = re.sub(r'\s+', ' ', address)
        if len(address) >= 10:
            apartments.add(address)
    
    # Also catch "The X at Y" pattern
    pattern2 = re.compile(
        r'(The\s+[A-Za-z]+(?:\s+at\s+[A-Za-z ]+)?)',
        re.IGNORECASE
    )
    for match in pattern2.finditer(text):
        name = match.group(1).strip()
        if len(name) >= 8 and "the" not in name.lower().replace("the ", ""):
            apartments.add(name)
    
    debug_print(f"[dynamic] mgny extracted {len(apartments)} ids")
    return apartments


def extract_ids_fifthave(text: str) -> Set[str]:
    """
    Fifth Ave Committee: Building name + Unit number.
    
    Format: "The Axel - 539 Vanderbilt Avenue, Brooklyn NY Unit 3F"
           "3 Eleven 11th Avenue, Brooklyn NY Unit 617"
    """
    apartments: Set[str] = set()
    
    # Pattern 1: "The Axel - 539 Vanderbilt Avenue ... Unit 3F"
    pattern1 = re.compile(
        r'((?:The\s+)?[A-Za-z]+\s*-\s*\d+\s+[A-Za-z ]+(?:Avenue|Street))[^U]*Unit\s+(\d+[A-Z]?)',
        re.IGNORECASE
    )
    for match in pattern1.finditer(text):
        building = match.group(1).strip()
        unit = match.group(2)
        apt_id = f"{building} Unit {unit}"
        apartments.add(re.sub(r'\s+', ' ', apt_id))
    
    # Pattern 2: "3 Eleven 11th Avenue ... Unit 617" (number + word name)
    pattern2 = re.compile(
        r'(\d+\s+[A-Za-z]+\s+\d+[a-z]*\s+Avenue)[^U]*Unit\s+(\d+[A-Z]?)',
        re.IGNORECASE
    )
    for match in pattern2.finditer(text):
        building = match.group(1).strip()
        unit = match.group(2)
        apt_id = f"{building} Unit {unit}"
        apartments.add(re.sub(r'\s+', ' ', apt_id))
    
    # Pattern 3: Simple "Address ... Unit X"
    pattern3 = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Avenue|Street))[^U]{0,30}Unit\s+(\d+[A-Z]?)',
        re.IGNORECASE
    )
    for match in pattern3.finditer(text):
        addr = match.group(1).strip()
        unit = match.group(2)
        apt_id = f"{addr} Unit {unit}"
        apt_id = re.sub(r'\s+', ' ', apt_id)
        apartments.add(apt_id)
    
    debug_print(f"[dynamic] fifthave extracted {len(apartments)} ids")
    return apartments


def extract_ids_cgm(text: str) -> Set[str]:
    """CGM RCCompliance - typically just shows SRO units."""
    apartments: Set[str] = set()
    
    # If the page mentions SRO units available
    if "SRO" in text.upper() and "available" in text.lower():
        apartments.add("SRO Units Available")
    
    # Look for any address patterns
    pattern = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Street|Avenue|Road))',
        re.IGNORECASE
    )
    for match in pattern.finditer(text):
        addr = match.group(1).strip()
        if len(addr) >= 10:
            apartments.add(addr)
    
    debug_print(f"[dynamic] cgm extracted {len(apartments)} ids")
    return apartments


def extract_ids_clinton(text: str) -> Set[str]:
    """Clinton Management - check if they have availabilities."""
    apartments: Set[str] = set()
    
    # They usually say "No availabilities found" when empty
    if "no availabilities" in text.lower():
        return set()
    
    # Look for building names
    pattern = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Street|Avenue|Road|Place|Boulevard))',
        re.IGNORECASE
    )
    for match in pattern.finditer(text):
        addr = match.group(1).strip()
        if len(addr) >= 10 and len(addr) <= 60:
            apartments.add(addr)
    
    debug_print(f"[dynamic] clinton extracted {len(apartments)} ids")
    return apartments


def extract_ids_nychdc(text: str) -> Set[str]:
    """
    NYC HDC Re-rentals page.
    
    Format: Building names like "Riverwalk Park", "The Balton", etc.
    """
    apartments: Set[str] = set()
    
    # Look for building names followed by addresses
    pattern = re.compile(
        r'((?:The\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+'
        r'(\d+\s+[A-Za-z ]+(?:Street|Avenue|Boulevard|Main))',
        re.IGNORECASE
    )
    
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        address = match.group(2).strip()
        # Skip UI text
        if name.lower() in ['view', 'advertisement', 'summary', 'details']:
            continue
        apt_id = f"{name} - {address}"
        apartments.add(apt_id)
    
    # Also look for specific building names we know
    known_buildings = [
        "Riverwalk Park", "The Balton", "One East Harlem", 
        "Bronx Point", "Van Dyke", "The Carolina", "Coney Island Associates"
    ]
    for building in known_buildings:
        if building.lower() in text.lower():
            apartments.add(building)
    
    debug_print(f"[dynamic] nychdc extracted {len(apartments)} ids")
    return apartments


def extract_ids_pronto(text: str) -> Set[str]:
    """
    Pronto Housing: Extract building names and unit numbers.
    
    Format: "VIA Phase II - 625 W. 57th St." with units like "1809 120% studio"
    """
    apartments: Set[str] = set()
    
    # Building names with addresses
    buildings = [
        ("VIA Phase II", r"VIA Phase II"),
        ("The Larstrand", r"The Larstrand"),
        ("Hoyt & Horn", r"Hoyt & Horn"),
        ("Alexander Crossing", r"Alexander Crossing"),
        ("7W21", r"7W21|7 West 21st"),
        ("Caesura", r"Caesura"),
        ("EOS Phase II", r"E[OŌ]S Phase II"),
        ("SVEN", r"SVEN"),
    ]
    
    for name, pattern in buildings:
        if re.search(pattern, text, re.IGNORECASE):
            apartments.add(name)
    
    # Also extract specific unit numbers like "04E", "07A", "1809"
    unit_pattern = re.compile(r'\b(\d{2,4}[A-Z]?)\s*-?\s*(?:\d+%|studio|bedroom)', re.IGNORECASE)
    for match in unit_pattern.finditer(text):
        unit = match.group(1)
        apartments.add(f"Unit {unit}")
    
    debug_print(f"[dynamic] pronto extracted {len(apartments)} ids")
    return apartments


def extract_ids_ahg(text: str) -> Set[str]:
    """
    AHG Leasing: Extract building names and addresses.
    
    Format: "Abington House at 500 W. 30th Street"
    """
    apartments: Set[str] = set()
    
    # Pattern: Building name at address
    pattern = re.compile(
        r'((?:The\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+at\s+'
        r'(\d+\s+[A-Za-z0-9. ]+(?:Street|Avenue|Ave))',
        re.IGNORECASE
    )
    
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        address = match.group(2).strip()
        apt_id = f"{name} at {address}"
        apartments.add(apt_id)
    
    # Known buildings
    known = ["Abington House", "The Easton", "451 Tenth Avenue", "553W30"]
    for building in known:
        if building.lower() in text.lower():
            apartments.add(building)
    
    debug_print(f"[dynamic] ahg extracted {len(apartments)} ids")
    return apartments


def extract_ids_sjp(text: str) -> Set[str]:
    """
    SJP Tax Consultants: Extract available apartments.
    
    Format: "Available Re-Rental Apartment in Astoria, Queens"
    """
    apartments: Set[str] = set()
    
    # Pattern: Available ... in Location
    pattern = re.compile(
        r'Available\s+Re-?Rental\s+Apartment\s+in\s+([A-Za-z]+,\s*[A-Za-z]+)',
        re.IGNORECASE
    )
    
    for match in pattern.finditer(text):
        location = match.group(1).strip()
        apt_id = f"Re-Rental in {location}"
        apartments.add(apt_id)
    
    # Also check for specific addresses
    addr_pattern = re.compile(
        r'(\d+(?:-\d+)?\s+[A-Za-z ]+(?:Street|Avenue|Road|Place))',
        re.IGNORECASE
    )
    for match in addr_pattern.finditer(text):
        addr = match.group(1).strip()
        if len(addr) >= 10 and len(addr) <= 50:
            apartments.add(addr)
    
    debug_print(f"[dynamic] sjp extracted {len(apartments)} ids")
    return apartments


def extract_ids_langsam(text: str) -> Set[str]:
    """
    Langsam Property Services: Extract unit listings.
    
    Format: "603 Pontiac Place unit #3C"
    """
    apartments: Set[str] = set()
    
    pattern = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Place|Street|Avenue|Road))\s*'
        r'(?:unit|apt|#)\s*#?([A-Z0-9]+)',
        re.IGNORECASE
    )
    
    for match in pattern.finditer(text):
        address = match.group(1).strip()
        unit = match.group(2)
        apt_id = f"{address} Unit {unit}"
        apartments.add(apt_id)
    
    debug_print(f"[dynamic] langsam extracted {len(apartments)} ids")
    return apartments


def extract_ids_spring(text: str) -> Set[str]:
    """
    Spring Leasing: Extract building names.
    
    Format: "1488 New York Avenue", "THE BEDFORD", "RADROC"
    """
    apartments: Set[str] = set()
    
    # Known buildings
    known = ["1488 New York Avenue", "321 E 60th Street", "RADROC", "THE BEDFORD"]
    for building in known:
        if building.lower().replace(" ", "") in text.lower().replace(" ", ""):
            apartments.add(building)
    
    debug_print(f"[dynamic] spring extracted {len(apartments)} ids")
    return apartments


def extract_ids_reclaim(text: str) -> Set[str]:
    """
    Reclaim HDFC: Extract building addresses.
    """
    apartments: Set[str] = set()
    
    pattern = re.compile(
        r'(\d+(?:-\d+)?\s+[A-Za-z ]+(?:Avenue|Street|Pkwy|Parkway)),\s*Bronx',
        re.IGNORECASE
    )
    
    for match in pattern.finditer(text):
        addr = match.group(1).strip()
        apartments.add(addr)
    
    debug_print(f"[dynamic] reclaim extracted {len(apartments)} ids")
    return apartments


def extract_ids_tfc(text: str) -> Set[str]:
    """
    TF Cornerstone: Extract building names and addresses.
    """
    apartments: Set[str] = set()
    
    # Known TFC buildings
    known = [
        "5203 Center Blvd", "455 W 37th St", "595 Dean St", 
        "5241 Center Blvd"
    ]
    for building in known:
        if building.lower().replace(" ", "") in text.lower().replace(" ", ""):
            apartments.add(building)
    
    # Pattern: Address followed by building info
    pattern = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Street|Avenue|Blvd|Boulevard|St))',
        re.IGNORECASE
    )
    for match in pattern.finditer(text):
        addr = match.group(1).strip()
        if len(addr) >= 10 and len(addr) <= 40:
            apartments.add(addr)
    
    debug_print(f"[dynamic] tfc extracted {len(apartments)} ids")
    return apartments


def extract_ids_wavecrest(text: str) -> Set[str]:
    """
    Wavecrest Rentals: Check if accepting applications.
    """
    apartments: Set[str] = set()
    
    # They indicate status with text
    if "currently not accepting" in text.lower():
        return set()  # No listings available
    
    if "accepting applications" in text.lower() or "available" in text.lower():
        apartments.add("Wavecrest Units Available")
    
    debug_print(f"[dynamic] wavecrest extracted {len(apartments)} ids")
    return apartments


def extract_ids_riseboro(text: str) -> Set[str]:
    """
    RiseBoro: Extract housing program info.
    """
    apartments: Set[str] = set()
    
    if "accepting applications" in text.lower():
        apartments.add("Woodlawn Senior Living - Accepting Applications")
    
    if "section 8" in text.lower() or "section-8" in text.lower():
        apartments.add("Section 8 Units")
    
    debug_print(f"[dynamic] riseboro extracted {len(apartments)} ids")
    return apartments


def extract_ids_generic(text: str) -> Set[str]:
    """Generic fallback extractor."""
    apartments: Set[str] = set()
    
    # Look for Unit + number patterns
    unit_pattern = re.compile(r'Unit\s+([A-Z0-9]{1,5})\b', re.IGNORECASE)
    for match in unit_pattern.finditer(text):
        apartments.add(f"Unit {match.group(1).upper()}")
    
    # Look for addresses
    addr_pattern = re.compile(
        r'(\d+\s+[A-Za-z ]+(?:Street|Avenue|Road|Place|Boulevard))',
        re.IGNORECASE
    )
    for match in addr_pattern.finditer(text):
        addr = match.group(1).strip()
        if 10 <= len(addr) <= 50:
            apartments.add(addr)
    
    # Cap at reasonable number
    if len(apartments) > 50:
        debug_print(f"[dynamic] generic: too many ({len(apartments)}), returning empty")
        return set()
    
    debug_print(f"[dynamic] generic extracted {len(apartments)} ids")
    return apartments


def is_valid_apartment_id(apt_id: str) -> bool:
    """
    Validate apartment ID - more permissive than before.
    """
    if not apt_id or len(apt_id) < 5 or len(apt_id) > 150:
        return False
    
    # Reject entries with newlines
    if "\n" in apt_id or "\r" in apt_id:
        return False
    
    # Reject obvious UI text
    ui_text = [
        'per month', 'view property', 'click here', 'more info', 
        'apply now', 'learn more', 'read more', 'view advertisement',
        'summary', 'details', 'download', 'contact'
    ]
    apt_lower = apt_id.lower()
    for ui in ui_text:
        if ui in apt_lower:
            return False
    
    # Must have either a digit OR be a known building name pattern
    has_digit = bool(re.search(r'\d', apt_id))
    is_building_name = bool(re.match(r'^(?:The\s+)?[A-Z][a-z]+', apt_id))
    
    if not has_digit and not is_building_name:
        return False
    
    return True


def format_apartment_changes(added: Set[str], removed: Set[str]) -> str:
    """Build alert message focusing on additions."""
    lines = []
    if added:
        lines.append("New apartments detected:")
        for apt in sorted(added)[:20]:
            lines.append(f"+ {apt}")
        if len(added) > 20:
            lines.append(f"... and {len(added) - 20} more")

    if len(removed) > 3:
        lines.append("")
        lines.append(f"({len(removed)} apartments removed)")

    return "\n".join(lines)


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


# =============================================================================
# SITES TO MONITOR
# =============================================================================

DYNAMIC_URLS = [
    # Sites with ACTUAL listings
    "https://afny.org/re-rentals",
    "https://iaffordny.com/re-rentals",
    "https://fifthave.org/re-rental-availabilities/",
    "https://mgnyconsulting.com/listings/",
    "https://www.prontohousingrentals.com/",
    "https://ahgleasing.com/",
    "https://www.sjpny.com/affordable-rerentals",
    "https://www.langsampropertyservices.com/affordable-rental-opportunities",
    "https://springmanagement.net/apartments-for-rent/",
    "https://sbmgmt.sitemanager.rentmanager.com/RECLAIMHDFC.aspx",
    "https://tfc.com/about/affordable-re-rentals",
    "https://wavecrestrentals.com/section.php?id=1",
    "https://riseboro.org/housing/woodlawn-senior-living/",
    "https://www.nychdc.com/find-re-rentals",
    "https://residenewyork.com/property-status/open-market/",
    
    # Sites that are directories or status pages (check for changes anyway)
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    
    # REMOVED - permanently broken
    # "https://city5.nyc/" - DNS failure (606 failures)
    # "https://ibis.powerappsportals.com/" - Always 500 error
    # "https://www.mickigarciarealty.com/" - 579 failures
    # "https://www.taxaceny.com/projects-8" - 579 failures  
    # "https://www.thebridgeny.org/news-and-media" - 579 failures
    # "https://ihrerentals.com/" - Often blocked
    # "https://kgupright.com/" - Marketing page only
    # "https://www.whedco.org/real-estate/affordable-housing-rentals/" - 404
]


def run_dynamic_once() -> None:
    text_state = load_json(TEXT_FILE)
    apt_state_raw = load_json(APT_FILE)
    
    # Deduplicate and validate existing state
    apt_state: Dict[str, list] = {}
    for url, apts in apt_state_raw.items():
        unique_apts = set(apts)
        valid_apts = {a for a in unique_apts if is_valid_apartment_id(a)}
        apt_state[url] = sorted(valid_apts)
    
    print(f"[INFO] Loaded state for {len(apt_state)} URLs")

    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking {url}")
        text = fetch_rendered_text(url)
        if text is None:
            track_failure(url)
            continue

        reset_failure_count(url)

        new_apartments_raw = extract_apartment_ids(text, url)
        new_apartments = {a for a in new_apartments_raw if is_valid_apartment_id(a)}
        
        print(f"[INFO] {url}: extracted {len(new_apartments)} apartments")
        if DEBUG and new_apartments:
            for apt in sorted(new_apartments)[:5]:
                print(f"  - {apt}")

        old_list = apt_state.get(url, [])
        old_apartments = set(old_list)

        if not old_apartments:
            print(f"[INIT] Baseline for {url}: {len(new_apartments)} units")
            apt_state[url] = sorted(new_apartments)
            text_state[url] = text
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if not added and not removed:
            print(f"[NOCHANGE] {url}")
            continue

        # Skip massive changes (likely extractor instability)
        if len(added) > 25 or len(removed) > 25:
            print(f"[SKIP] {url}: Massive change (+{len(added)} / -{len(removed)}) - likely noise")
            continue

        print(f"[CHANGE] {url}: +{len(added)} / -{len(removed)}")

        summary = format_apartment_changes(added, removed)

        if added and summary:
            send_ntfy_alert(url, summary, priority="4")
        elif len(removed) > 3 and summary:
            send_ntfy_alert(url, summary, priority="2")

        apt_state[url] = sorted(new_apartments)
        text_state[url] = text
        changed_any = True

    if changed_any:
        save_json(APT_FILE, apt_state)
        save_json(TEXT_FILE, text_state)
        print(f"[INFO] State saved. URLs tracked: {len(apt_state)}")
    else:
        print("[INFO] No changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
