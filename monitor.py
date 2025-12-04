from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Set

import requests
from bs4 import BeautifulSoup

# ============================================================
# Configuration
# ============================================================

STATIC_URLS: List[str] = [
    "https://iaffordny.com/re-rentals",
    "https://afny.org/re-rentals",
    "https://mgnyconsulting.com/listings/",
    "https://residenewyork.com/property-status/open-market/",
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()

HASH_FILE = Path("hashes.json")          # kept for backward compatibility
TEXT_FILE = Path("page_texts.json")      # raw text for debugging
APT_STATE_FILE = Path("static_apartments.json")

DEBUG = os.environ.get("DEBUG", "").lower() == "true"


# ============================================================
# Helpers
# ============================================================

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
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Could not save {path}: {e}")


def normalize_whitespace(text: str) -> str:
    # Normalize spaces inside lines and trim
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"\s+", " ", line)
        lines.append(line)
    return "\n".join(lines)


# ============================================================
# Fetching and basic filtering
# ============================================================

def fetch_page_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"{url} raw length: {len(raw_text)}")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())

    # Very light site specific trimming where it is cheap to do
    if "residenewyork.com" in url:
        # Trim header and footer noise around "Open Market"
        marker = "Open Market"
        idx = text.find(marker)
        if idx != -1:
            text = text[idx:]

    if "affordablelivingnyc.com" in url:
        marker = "HPD"
        idx = text.find(marker)
        if idx != -1:
            text = text[idx:]

    text = normalize_whitespace(text)
    debug_print(f"{url} filtered length: {len(text)}")
    return text


# ============================================================
# Apartment ID extraction
# ============================================================

def extract_apartment_ids(text: str, url: str) -> Set[str]:
    """
    Convert the page text into a stable set of "listing IDs".
    These IDs should not change if formatting or ordering changes.
    """

    # Generic method based on Unit / Apt / Apartment lines.
    apartments = extract_by_unit_lines(text, url)

    # If the generic method finds nothing, fall back to some very
    # simple building level patterns so we at least have a baseline.
    if not apartments:
        apartments = extract_building_level_ids(text, url)

    debug_print(f"[static] extracted {len(apartments)} ids for {url}")
    return apartments


def extract_by_unit_lines(text: str, url: str) -> Set[str]:
    """
    Look for lines with "Unit", "Apt", or "Apartment" followed by an id,
    then pair them with a nearby building line.
    Works well for Reside, MGNY, IAFFORD, AFNY.
    """
    apartments: Set[str] = set()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    skip_words = [
        "results",
        "neighborhood",
        "household",
        "income",
        "amenities",
        "subway",
        "faq",
        "how to apply",
        "beds",
    ]

    for idx, line in enumerate(lines):
        m = re.search(r"\b(Unit|Apt|Apartment)\s+([0-9A-Za-z]+)", line, re.IGNORECASE)
        if not m:
            continue

        unit_token = f"{m.group(1).title()} {m.group(2)}"

        # Walk back a few lines to find a likely building line.
        building_line = None
        for j in range(idx - 1, max(-1, idx - 5), -1):
            if j < 0:
                break
            prev = lines[j]
            lower_prev = prev.lower()
            if any(word in lower_prev for word in skip_words):
                continue
            # Prefer lines that look like addresses or buildings.
            if (
                "apartments" in lower_prev
                or "apartment" in lower_prev
                or re.search(r"\d+\s+\w+", prev)
            ):
                building_line = prev
                break

        if building_line is None:
            building_line = line

        identifier = f"{building_line} | {unit_token}"
        identifier = re.sub(r"\s+", " ", identifier)
        apartments.add(identifier)

    return apartments


def extract_building_level_ids(text: str, url: str) -> Set[str]:
    """
    Fallback to building level identifiers when no unit based ones are found.
    For IAFFORD/AFNY we only use the building name before 'Rent:'.
    """
    apartments: Set[str] = set()
    lower = text.lower()

    # IAFFORD / AFNY pattern: "Some Building Name ... Rent:"
    if "iaffordny.com" in url or "afny.org" in url:
        pattern = re.compile(
            r"([A-Z][A-Za-z0-9 .,'/-]+?)(?:\s+Multiple Units)?\s+Rent:",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            name = m.group(1).strip()
            identifier = re.sub(r"\s+", " ", name)
            apartments.add(identifier)

    # MGNY: addresses like "2010 Walton Avenue Apartments"
    if "mgnyconsulting.com" in url:
        pattern = re.compile(
            r"\d{3,5}\s+[A-Za-z0-9 .,'/-]+Apartments?",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            apartments.add(re.sub(r"\s+", " ", m.group(0).strip()))

    # Reside basic building names if nothing else
    if "residenewyork.com" in url:
        pattern = re.compile(
            r"[A-Z][A-Za-z0-9 .,'/-]+Apartments?",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            apartments.add(re.sub(r"\s+", " ", m.group(0).strip()))

    # If still empty, take a very small number of long lines as a last resort
    if not apartments:
        for line in text.splitlines():
            if len(line) > 40:
                apartments.add(line.strip())
                if len(apartments) >= 20:
                    break

    return apartments


# ============================================================
# Diff formatting and notifications
# ============================================================

def format_apartment_changes(added: Set[str], removed: Set[str]) -> str | None:
    if not added and not removed:
        return None

    lines: List[str] = []

    if added:
        lines.append("NEW LISTINGS:")
        for apt in sorted(added)[:10]:
            lines.append(f"  • {apt}")
        if len(added) > 10:
            lines.append(f"  • ... and {len(added) - 10} more")

    if removed:
        lines.append("")
        lines.append("REMOVED:")
        for apt in sorted(removed)[:5]:
            lines.append(f"  • {apt}")
        if len(removed) > 5:
            lines.append(f"  • ... and {len(removed) - 5} more")

    return "\n".join(lines)


def send_ntfy_alert(url: str, diff_summary: str, level: str = "normal") -> None:
    """
    Send an ntfy notification.
    Headers must be latin-1 compatible, so no emojis here.
    """
    if not diff_summary:
        return

    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL is not set. Would have sent alert:")
        print(diff_summary)
        return

    if level == "high":
        title = "New housing listings"
        priority = "5"
        tags = "house,alert"
    elif level == "low":
        title = "Housing listings updated"
        priority = "3"
        tags = "house,info"
    else:
        title = "Housing listings update"
        priority = "4"
        tags = "house"

    body = f"{url}\n\n{diff_summary}"

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
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
        print(f"[ERROR] Sending ntfy alert: {e}")


# ============================================================
# Main runner
# ============================================================

def run_once() -> None:
    hash_state = load_json(HASH_FILE)        # kept for compatibility
    text_state = load_json(TEXT_FILE)
    apt_state = load_json(APT_STATE_FILE)

    changed_any = False

    for url in STATIC_URLS:
        print(f"[INFO] Checking {url}")

        text = fetch_page_text(url)
        if text is None:
            continue

        new_apartments = extract_apartment_ids(text, url)
        old_apartments = set(apt_state.get(url, []))

        if not old_apartments:
            print(f"[INIT] Capturing baseline for {url}: {len(new_apartments)} apartments")
            apt_state[url] = sorted(new_apartments)
            text_state[url] = text
            changed_any = True
            continue

        added = new_apartments - old_apartments
        removed = old_apartments - new_apartments

        if added or removed:
            print(f"[CHANGE] {url}: +{len(added)} / -{len(removed)} apartments")

            summary = format_apartment_changes(added, removed)

            # High priority if there are new listings
            if added and summary:
                send_ntfy_alert(url, summary, level="high")
            elif not added and len(removed) > 5 and summary:
                # Many removals often means a real change or a filter change
                send_ntfy_alert(url, summary, level="low")

            apt_state[url] = sorted(new_apartments)
            text_state[url] = text
            changed_any = True
        else:
            print(f"[NOCHANGE] {url} - same apartments")

    if changed_any:
        save_json(APT_STATE_FILE, apt_state)
        save_json(TEXT_FILE, text_state)
        save_json(HASH_FILE, hash_state)  # nothing new, but keep file present
    else:
        print("[INFO] No changes to save.")


if __name__ == "__main__":
    run_once()
