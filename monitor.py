import os
import json
import hashlib
import difflib
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ------------- Configuration -------------

URLS = [
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://city5.nyc/",
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://fifthave.org/re-rental-availabilities/",
    "https://ihrerentals.com/",
    "https://kgupright.com/",
    "https://www.langsampropertyservices.com/affordable-rental-opportunities",
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

# URLs where we want apartment-based detection instead of raw text diff
APARTMENT_URLS = {
    "https://residenewyork.com/property-status/open-market/",
    "https://ahgleasing.com/",
    # Add more later if you want apartment-based change detection
}

STATE_DIR = Path(".")
HASH_FILE = STATE_DIR / "hashes.json"
TEXT_FILE = STATE_DIR / "page_texts.json"
APT_FILE = STATE_DIR / "static_apartments.json"

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


# -------- Utility helpers --------

def load_json(path: Path):
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not load {path}: {e}")
        return {}


def save_json(path: Path, data) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Could not save {path}: {e}")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------- Content filtering --------

LISTING_KEYWORDS = [
    r"\b(?:apartment|apt|unit|studio|bedroom|br)\b",
    r"\b(?:rent|rental|lease|available|vacancy)\b",
    r"\b(?:household|income|ami|annual)\b",
    r"\$\d{1,3}(?:,\d{3})*",
    r"\b\d+\s*(?:bed|br|bedroom)\b",
    r"\b(?:one|two|three|1|2|3)[\s-]?bedroom",
    r"\b(?:floor|bldg|building|address|location)\b",
    r"\b(?:sq\.?\s*ft|square\s+feet)\b",
    r"\b(?:apply|application|waitlist|lottery)\b",
]
LISTING_REGEXES = [re.compile(p, re.IGNORECASE) for p in LISTING_KEYWORDS]

IGNORE_PATTERNS = [
    r"^(?:skip to|menu|search|login|sign in|subscribe|newsletter)",
    r"^(?:facebook|twitter|instagram|linkedin|youtube)",
    r"^(?:privacy policy|terms|copyright|cookies)",
    r"^\s*[×✕✖]\s*$",
    r"^(?:home|about|contact|careers|media|events)\s*$",
]
IGNORE_REGEXES = [re.compile(p, re.IGNORECASE) for p in IGNORE_PATTERNS]


def extract_relevant_content(text: str) -> str:
    """Try to keep listing-like content and a bit of context."""
    lines = text.splitlines()
    relevant_lines = []
    context_window = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        if any(rx.match(line) for rx in IGNORE_REGEXES):
            continue

        has_listing = any(rx.search(line) for rx in LISTING_REGEXES)

        if has_listing:
            relevant_lines.extend(context_window)
            relevant_lines.append(line)
            context_window = []
        else:
            context_window.append(line)
            if len(context_window) > 2:
                context_window.pop(0)

    result = "\n".join(relevant_lines)
    if len(result) < 100:
        return text
    return result


# --- Site specific filters ---

def filter_resideny_open_market(text: str) -> str:
    """
    Reside New York Open Market:
    try to keep the main listings column, not featured/closed sections.
    """
    start_marker = "Open Market"
    end_markers = ["Closed Project", "Closed Projects", "Blog", "Contact"]

    idx = text.find(start_marker)
    if idx != -1:
        text = text[idx:]

    end_idx = len(text)
    for m in end_markers:
        j = text.find(m)
        if j != -1:
            end_idx = min(end_idx, j)
    text = text[:end_idx]

    return text


def filter_ahg(text: str) -> str:
    """AHG leasing: keep from 'LOW INCOME HOUSING OPPORTUNITIES' onward."""
    marker = "LOW INCOME HOUSING OPPORTUNITIES"
    idx = text.upper().find(marker)
    if idx != -1:
        text = text[idx:]
    return text


def filter_google_sites(text: str) -> str:
    marker = "HPD"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://ahgleasing.com/": filter_ahg,
    "https://sites.google.com/affordablelivingnyc.com/hpd/home": filter_google_sites,
}


def apply_content_filters(url: str, text: str) -> str:
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter:
        text = site_filter(text)
    text = extract_relevant_content(text)
    return text


def fetch_page_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=45)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")

    debug_print(f"[static] Raw text length for {url}: {len(raw_text)}")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = apply_content_filters(url, text)

    debug_print(f"[static] Filtered text length for {url}: {len(text)}")
    debug_print(f"[static] First 200 chars: {text[:200]}")

    text = normalize_whitespace(text)
    return text


# -------- Diff and apartment helpers --------

def summarize_diff(old_text: str, new_text: str,
                   max_snippets: int = 4,
                   context_chars: int = 120,
                   max_chars: int = 1000) -> str | None:
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    additions = []
    removals = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("insert", "replace"):
            seg = new_text[j1:j2].strip()
            if seg and len(seg) >= 20:
                start = max(0, j1 - context_chars)
                end = min(len(new_text), j2 + context_chars)
                snippet = new_text[start:end].strip()
                additions.append(f"➕ {snippet}")

        if tag in ("delete", "replace"):
            seg = old_text[i1:i2].strip()
            if seg and len(seg) >= 20:
                removals.append(f"➖ {seg[:160]}")

    snippets = additions[:max_snippets]
    if len(snippets) < max_snippets:
        snippets.extend(removals[:max_snippets - len(snippets)])

    if not snippets:
        return None

    summary = "\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[diff truncated]"
    return summary


def extract_apartment_ids(text: str, url: str) -> set[str]:
    """
    Extract identifiers that represent individual apartments or listings.
    Shared logic with the dynamic monitor.
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

    debug_print(f"[static] Extracted {len(apartments)} apartment ids for {url}")
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
            parts.append(f"  ... and {len(added) - 10} more")

    if removed:
        parts.append("\n❌ REMOVED (may just be filled):")
        for apt in sorted(removed)[:5]:
            parts.append(f"  • {apt}")
        if len(removed) > 5:
            parts.append(f"  ... and {len(removed) - 5} more")

    return "\n".join(parts)


# -------- Notifications --------

def send_ntfy_alert(url: str, body: str) -> None:
    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set; skipping alert")
        print(f"[ALERT] Would have sent for {url}:\n{body}")
        return

    full_body = f"{url}\n\n{body}"
    title = "🏠 Housing website updated"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=full_body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "4",
                "Tags": "house",
                "Click": url,
            },
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert: {e}")


# -------- Main runner --------

def run_once() -> None:
    hashes = load_json(HASH_FILE)
    texts = load_json(TEXT_FILE)
    apartments_state = load_json(APT_FILE)

    changed = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        text = fetch_page_text(url)
        if not text:
            continue

        # Apartment-based mode
        if url in APARTMENT_URLS:
            new_apts = extract_apartment_ids(text, url)
            old_apts = set(apartments_state.get(url, []))

            if not old_apts:
                print(f"[INIT] Recording {len(new_apts)} apartments for {url}")
                apartments_state[url] = sorted(new_apts)
                texts[url] = text
                changed = True
                continue

            added = new_apts - old_apts
            removed = old_apts - new_apts

            if added or removed:
                print(f"[CHANGE] {url}: +{len(added)}, -{len(removed)} apartments")
                summary = format_apartment_changes(added, removed)
                if summary and added:
                    # Only alert when something new appears
                    send_ntfy_alert(url, summary)
                apartments_state[url] = sorted(new_apts)
                texts[url] = text
                changed = True
            else:
                print(f"[NOCHANGE] {url} apartments unchanged")
            continue

        # Normal text-based mode
        new_hash = hash_text(text)
        old_hash = hashes.get(url)
        old_text = texts.get(url)

        if old_hash is None or old_text is None:
            print(f"[INIT] Storing baseline for {url}")
            hashes[url] = new_hash
            texts[url] = text
            changed = True
            continue

        if new_hash != old_hash:
            print(f"[CHANGE] Detected change on {url}")
            summary = summarize_diff(old_text, text)
            if summary:
                send_ntfy_alert(url, summary)
            hashes[url] = new_hash
            texts[url] = text
            changed = True
        else:
            print(f"[NOCHANGE] {url}")

    if changed:
        save_json(HASH_FILE, hashes)
        save_json(TEXT_FILE, texts)
        save_json(APT_FILE, apartments_state)
    else:
        print("[INFO] No state changes to save")


if __name__ == "__main__":
    run_once()
