import hashlib
import json
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import difflib

# --------------- CONFIGURATION ---------------

# Static pages only. JS heavy pages are handled in monitor_dynamic.py
URLS = [
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://cgmrcompliance.com/housing-opportunities-1",
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
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://www.taxaceny.com/projects-8",
    "https://tfc.com/about/affordable-re-rentals",
    "https://www.thebridgeny.org/news-and-media",
    "https://wavecrestrentals.com/section.php?id=1",
    "https://yourneighborhoodhousing.com/",
]

HEADERS = {
    "User-Agent": "PrivPageRefresh/1.0 (+https://github.com/BossalDom/Priv-Page-Refresh)"
}

HASH_FILE = Path("hashes.json")
TEXT_FILE = Path("page_texts.json")

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL")

# Debug flag – set DEBUG: "true" in the workflow env if you want extra logs
DEBUG = os.environ.get("DEBUG", "").lower() == "true"


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


# --------------- STATE HELPERS ---------------

def load_json(path: Path) -> dict:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Could not read {path}: {e}")
    return {}


def save_json(path: Path, data: dict) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not write {path}: {e}")


# --------------- CONTENT FILTERS ---------------

def normalize_whitespace(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def filter_resideny_open_market(text: str) -> str:
    """For Reside New York: drop sidebar Featured Properties."""
    marker = "Featured Properties"
    idx = text.find(marker)
    if idx != -1:
        text = text[:idx]
    return text


def filter_ahg(text: str) -> str:
    """For Affordable Housing Group: skip dated intro at top."""
    marker = "LOW INCOME HOUSING OPPORTUNITIES"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


def filter_streeteasy(text: str) -> str:
    """For StreetEasy: focus on building details, ignore search noise."""
    start_marker = "Riverton Square"
    end_marker = "Search homes nearby"
    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)
    if start_idx != -1:
        if end_idx != -1 and end_idx > start_idx:
            text = text[start_idx:end_idx]
        else:
            text = text[start_idx:]
    return text


def filter_google_sites(text: str) -> str:
    """For the HPD Google Sites page: trim header/nav."""
    marker = "HPD"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://ahgleasing.com/": filter_ahg,
    "https://streeteasy.com/building/riverton-square": filter_streeteasy,
    "https://sites.google.com/affordablelivingnyc.com/hpd/home": filter_google_sites,
}

# Expanded patterns for better listing detection
LISTING_KEYWORDS = [
    # core listing terms
    r"\b(?:apartment|apt|unit|studio|bedroom|br)\b",
    r"\b(?:rent|rental|lease|available|vacancy)\b",
    r"\b(?:household|income|ami|annual)\b",

    # numeric indicators
    r"\$\d{1,3}(?:,\d{3})*",          # currency amounts
    r"\b\d+\s*(?:bed|br|bedroom)\b",  # 2 bed, 3 bedroom, etc
    r"\b(?:one|two|three|1|2|3)[\s-]?bedroom\b",

    # building/location identifiers
    r"\b(?:floor|bldg|building|address|location)\b",
    r"\b(?:sq\.?\s*ft|square\s+feet)\b",

    # application terms
    r"\b(?:apply|application|waitlist|lottery)\b",
]

LISTING_REGEXES = [re.compile(p, re.IGNORECASE) for p in LISTING_KEYWORDS]

# Things to ignore (nav, footer, socials, generic one word links)
IGNORE_PATTERNS = [
    r"^(?:skip to|menu|search|login|sign in|subscribe|newsletter)",
    r"^(?:facebook|twitter|instagram|linkedin|youtube)",
    r"^(?:privacy policy|terms|copyright|cookies)",
    r"^\s*[×✕✖]\s*$",
    r"^(?:home|about|contact|careers|media|events)\s*$",
]

IGNORE_REGEXES = [re.compile(p, re.IGNORECASE) for p in IGNORE_PATTERNS]


def extract_relevant_content(text: str) -> str:
    """
    Extract listing relevant content while keeping a bit of context
    around those lines. Less aggressive than strict line filtering.
    """
    lines = text.splitlines()
    relevant_lines: list[str] = []
    context_window: list[str] = []  # last 1-2 lines to give context

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        # skip obvious nav/footer junk
        if any(rx.match(line) for rx in IGNORE_REGEXES):
            continue

        has_listing_content = any(rx.search(line) for rx in LISTING_REGEXES)

        if has_listing_content:
            # include recent context, then this line
            if context_window:
                relevant_lines.extend(context_window)
            relevant_lines.append(line)
            context_window = []
        else:
            # keep as possible context (cap at 2 lines)
            context_window.append(line)
            if len(context_window) > 2:
                context_window.pop(0)

    result = "\n".join(relevant_lines)

    # If we got almost nothing, fall back to original filtered only by site rules
    # to avoid missing sites with unusual structure.
    if len(result) < 100:
        return text

    return result


def apply_content_filters(url: str, text: str) -> str:
    """Site specific filters then general listing extraction."""
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter:
        text = site_filter(text)

    text = extract_relevant_content(text)
    return text


# --------------- FETCH AND DIFF ---------------

def fetch_page_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")

    debug_print(f"Raw text length for {url}: {len(raw_text)} chars")

    # basic cleanup of empty lines
    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())

    text = apply_content_filters(url, text)

    debug_print(f"Filtered text length for {url}: {len(text)} chars")
    debug_print(f"First 200 chars: {text[:200]}")

    text = normalize_whitespace(text)
    return text


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 5,
    context_chars: int = 120,
    max_chars: int = 1200,
) -> str | None:
    """
    Improved diff that highlights additions and removals.
    We care most about additions (new listings).
    """
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    additions: list[str] = []
    removals: list[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("insert", "replace"):
            new_seg = new_text[j1:j2].strip()
            if new_seg and len(new_seg) >= 10:
                start = max(0, j1 - context_chars)
                end = min(len(new_text), j2 + context_chars)
                snippet = new_text[start:end].strip()
                additions.append(f"➕ {snippet}")

        if tag in ("delete", "replace"):
            old_seg = old_text[i1:i2].strip()
            if old_seg and len(old_seg) >= 10:
                removals.append(f"➖ {old_seg[:100]}")

    snippets: list[str] = []
    snippets.extend(additions[:max_snippets])
    if len(snippets) < max_snippets:
        snippets.extend(removals[: max_snippets - len(snippets)])

    if not snippets:
        return None

    summary = "\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[...truncated]"
    return summary


# --------------- NOTIFICATIONS ---------------

def send_ntfy_alert(url: str, diff_summary: str | None) -> None:
    if not diff_summary:
        print(f"[INFO] Change on {url} was too minor or filtered out. No alert sent.")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set in environment. Set it as a GitHub Actions secret.")
        print(f"[ALERT] Would have sent notification for {url}:\n{diff_summary}")
        raise ValueError("NTFY_TOPIC_URL environment variable not configured")

    body = f"{url}\n\nChanges:\n{diff_summary}"
    title = "Housing site updated"

    try:
        resp = requests.post(
        NTFY_TOPIC_URL,
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "4",
            "Tags": "house,warning",
            "Click": url,
        },
        timeout=15,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
            raise RuntimeError(f"Notification failed: {resp.status_code}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert: {e}")
        raise


# --------------- MAIN LOOP ---------------

def run_once() -> None:
    hash_state = load_json(HASH_FILE)
    text_state = load_json(TEXT_FILE)
    changed_any = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        new_text = fetch_page_text(url)
        if new_text is None:
            continue

        new_hash = hash_text(new_text)
        old_hash = hash_state.get(url)
        old_text = text_state.get(url)

        if old_hash is None or old_text is None:
            print(f"[INIT] Recording initial state for {url}")
            hash_state[url] = new_hash
            text_state[url] = new_text
            changed_any = True
            continue

        if new_hash != old_hash:
            print(f"[CHANGE] Detected change on {url}")
            diff_summary = summarize_diff(old_text, new_text)
            if diff_summary:
                print("[DIFF]\n" + diff_summary)
            send_ntfy_alert(url, diff_summary)
            hash_state[url] = new_hash
            text_state[url] = new_text
            changed_any = True
        else:
            print(f"[NOCHANGE] No relevant change on {url}")

    if changed_any:
        save_json(HASH_FILE, hash_state)
        save_json(TEXT_FILE, text_state)
    else:
        print("[INFO] No changes to save.")


if __name__ == "__main__":
    run_once()
