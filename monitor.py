import os
import json
import hashlib
import difflib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests
from bs4 import BeautifulSoup

# ============================================================
# Configuration
# ============================================================

# Static sites to monitor with plain HTTP requests.
# Edit this list to match your current repo.
URLS: List[str] = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://residenewyork.com/property-status/open-market/",
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://myrscnyconsulting.com/listings/",
    # Add or remove URLs as needed
]

RESIDENY_URL = "https://residenewyork.com/property-status/open-market/"

HASH_FILE = Path("hashes.json")
TEXT_FILE = Path("page_texts.json")
STATIC_APT_FILE = Path("static_apartments.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()
DEBUG = os.environ.get("DEBUG", "").lower() == "true"


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


# ============================================================
# Utility helpers
# ============================================================

def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to load {path}: {e}")
        return {}


def save_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Failed to save {path}: {e}")


def normalize_whitespace(text: str) -> str:
    # Collapse multiple spaces and normalize line breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# Content filtering
# ============================================================

# Expanded patterns for listing related lines
LISTING_KEYWORDS = [
    r"\b(?:apartment|apt|unit|studio|bedroom|br)\b",
    r"\b(?:rent|rental|lease|available|vacancy)\b",
    r"\b(?:household|income|ami|annual)\b",
    r"\$\d{1,3}(?:,\d{3})*",
    r"\b\d+\s*(?:bed|br|bedroom)\b",
    r"\b(?:one|two|three|1|2|3)[\s-]?bedroom\b",
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
    """
    Extract listing relevant content while preserving some context.
    """
    lines = text.splitlines()
    relevant_lines: List[str] = []
    context_window: List[str] = []

    for raw_line in lines:
        line = raw_line.strip()
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

    # If we ended up with almost nothing, fall back to original text
    if len(result) < 100:
        return text

    return result


# Site specific content filters

def filter_resideny_open_market(text: str) -> str:
    """
    Focus on the Open Market listings area for Reside New York.
    This is a light filter. Real diffing is done later by apartment IDs.
    """
    marker = "Open Market"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


def filter_google_sites(text: str) -> str:
    marker = "HPD"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


CONTENT_FILTERS = {
    RESIDENY_URL: filter_resideny_open_market,
    "https://sites.google.com/affordablelivingnyc.com/hpd/home": filter_google_sites,
}


def apply_content_filters(url: str, text: str) -> str:
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter:
        text = site_filter(text)
    text = extract_relevant_content(text)
    return text


# ============================================================
# Fetching
# ============================================================

def fetch_page_text(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[fetch] Raw text length for {url}: {len(raw_text)}")

    text_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    text = "\n".join(text_lines)
    text = apply_content_filters(url, text)
    debug_print(f"[fetch] Filtered text length for {url}: {len(text)}")

    return normalize_whitespace(text)


# ============================================================
# Diffing helpers
# ============================================================

def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 5,
    context_chars: int = 120,
    max_chars: int = 1200,
) -> Optional[str]:
    """
    Produce a short summary of textual changes between two versions.
    """
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    additions: List[str] = []
    removals: List[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("insert", "replace"):
            seg = new_text[j1:j2].strip()
            if seg and len(seg) >= 10:
                start = max(0, j1 - context_chars)
                end = min(len(new_text), j2 + context_chars)
                snippet = new_text[start:end].strip()
                additions.append(f"➕ {snippet}")

        if tag in ("delete", "replace"):
            seg = old_text[i1:i2].strip()
            if seg and len(seg) >= 10:
                removals.append(f"➖ {seg[:100]}")

    snippets: List[str] = []
    snippets.extend(additions[:max_snippets])
    if len(snippets) < max_snippets:
        snippets.extend(removals[: max_snippets - len(snippets)])

    if not snippets:
        return None

    summary = "\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[...truncated]"
    return summary


# ============================================================
# Reside New York apartment ID logic
# ============================================================

def extract_resideny_ids(text: str) -> Set[str]:
    """
    Extract stable apartment identifiers from Reside New York Open Market page.

    Example target id:
      "Dunbar Apartments 246 West 150th Street Apartment Unit 2C"
    """
    # Normalize separators and punctuation
    normalized = text.replace("|", " ")
    normalized = normalized.replace("–", "-")

    # Building name plus the word "Apartment" then a dash then Unit X
    pattern = re.compile(
        r"([A-Z0-9][A-Za-z0-9 .,'/]+?\sApartment[s]?)\s*-\s*Unit\s*([0-9A-Z]+)",
        re.IGNORECASE,
    )

    ids: Set[str] = set()

    for match in pattern.finditer(normalized):
        building = " ".join(match.group(1).split())[:80]  # collapse extra spaces
        unit = match.group(2).upper()
        ids.add(f"{building} Unit {unit}")

    debug_print(f"[reside] Extracted {len(ids)} apartment ids")
    return ids


def format_apartment_changes(added: Set[str], removed: Set[str]) -> Optional[str]:
    """
    Create a readable summary of apartment additions and removals.
    """
    if not added and not removed:
        return None

    lines: List[str] = []

    if added:
        lines.append("🆕 NEW LISTINGS:")
        for apt in sorted(added)[:10]:
            lines.append(f"  • {apt}")
        if len(added) > 10:
            lines.append(f"  … and {len(added) - 10} more")

    if removed:
        lines.append("")
        lines.append("❌ REMOVED:")
        for apt in sorted(removed)[:5]:
            lines.append(f"  • {apt}")
        if len(removed) > 5:
            lines.append(f"  … and {len(removed) - 5} more")

    return "\n".join(lines)


# ============================================================
# Notifications
# ============================================================

def send_ntfy_alert(url: str, diff_summary: Optional[str]) -> None:
    if not diff_summary:
        print(f"[INFO] No meaningful changes on {url}")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set in environment")
        print(f"[ALERT] Would notify for {url}:\n{diff_summary}")
        # Fail so it is obvious in GitHub Actions
        raise ValueError("NTFY_TOPIC_URL environment variable not configured")

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
        print(f"[ERROR] Sending ntfy alert: {e}")
        raise


# ============================================================
# Main run loop
# ============================================================

def run_once() -> None:
    hashes = load_json(HASH_FILE)
    texts = load_json(TEXT_FILE)
    apt_state = load_json(STATIC_APT_FILE)

    changed_any = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        text = fetch_page_text(url)
        if text is None:
            continue

        # Special handling for Reside New York
        if url == RESIDENY_URL:
            new_ids = extract_resideny_ids(text)
            old_ids = set(apt_state.get(url, []))

            if not old_ids:
                # First run: record baseline but do not alert
                print(f"[INIT] Recording {len(new_ids)} ResideNY apartments")
                apt_state[url] = sorted(new_ids)
                texts[url] = text
                changed_any = True
                continue

            added = new_ids - old_ids
            removed = old_ids - new_ids

            if added or removed:
                debug_print(
                    f"[reside] +{len(added)} new, -{len(removed)} removed apartments"
                )
                summary = format_apartment_changes(added, removed)

                # Only alert when there are additions
                if added and summary:
                    send_ntfy_alert(url, summary)

                apt_state[url] = sorted(new_ids)
                texts[url] = text
                changed_any = True
            else:
                print("[NOCHANGE] ResideNY apartments unchanged")

            # Skip generic hash based comparison
            continue

        # Generic hash based monitoring for all other sites
        old_text = texts.get(url, "")
        new_hash = hash_text(text)

        if old_text and new_hash == hashes.get(url):
            print(f"[NOCHANGE] {url}")
            continue

        if not old_text:
            print(f"[INIT] Capturing baseline for {url}")
            hashes[url] = new_hash
            texts[url] = text
            changed_any = True
            continue

        diff_summary = summarize_diff(old_text, text)
        if diff_summary:
            send_ntfy_alert(url, diff_summary)

        hashes[url] = new_hash
        texts[url] = text
        changed_any = True

    if changed_any:
        save_json(HASH_FILE, hashes)
        save_json(TEXT_FILE, texts)
        save_json(STATIC_APT_FILE, apt_state)
    else:
        print("[INFO] No changes to save.")


if __name__ == "__main__":
    run_once()
