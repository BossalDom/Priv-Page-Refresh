#!/usr/bin/env python3
import os
import json
import re
import difflib
import hashlib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------
# Configuration
# --------------------------------------------------

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "")

# Static or mostly static pages
URLS = [
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://fifthave.org/re-rental-availabilities/",
    "https://ihrerentals.com/",
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
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://www.taxaceny.com/projects-8",
    "https://tfc.com/about/affordable-re-rentals",
    "https://www.thebridgeny.org/news-and-media",
    "https://www.elhrerentals.com/",
    "https://wavecrestrentals.com/section.php?id=1",
    "https://yourneighborhoodhousing.com/",
]

STATE_DIR = Path(".")
HASH_FILE = STATE_DIR / "hashes.json"
TEXT_FILE = STATE_DIR / "page_texts.json"

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


# --------------------------------------------------
# Helpers for JSON state
# --------------------------------------------------


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[WARN] Could not read {path}: {exc}")
        return {}


def save_json(path: Path, data: dict) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"[ERROR] Could not write {path}: {exc}")


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# --------------------------------------------------
# Content filters
# --------------------------------------------------

def filter_resideny_open_market(text: str) -> str:
    marker = "Open Market"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


def filter_ahg(text: str) -> str:
    marker = "LOW INCOME HOUSING OPPORTUNITIES"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


def filter_google_sites(text: str) -> str:
    marker = "HPD"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


def filter_streeteasy(text: str) -> str:
    start_marker = "Riverton Square"
    end_marker = "Search homes nearby"

    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)

    if start_idx != -1:
        if end_idx != -1:
            text = text[start_idx:end_idx]
        else:
            text = text[start_idx:]
    return text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://ahgleasing.com/": filter_ahg,
    "https://sites.google.com/affordablelivingnyc.com/hpd/home": filter_google_sites,
    "https://streeteasy.com/building/riverton-square": filter_streeteasy,
}

LISTING_KEYWORDS = [
    r"\b(apartment|apt|unit|studio|bedroom|br)\b",
    r"\b(rent|rental|lease|available|vacancy)\b",
    r"\b(household|income|ami|annual)\b",
    r"\$\d{3,}",                      # dollar amounts
    r"\b\d+\s*(bed|br|bedroom)\b",
    r"\b(one|two|three|1|2|3)[\s-]?bedroom\b",
    r"\b(floor|bldg|building|address|location)\b",
    r"\b(sq\.?\s*ft|square\s+feet)\b",
    r"\b(apply|application|waitlist|lottery)\b",
]

LISTING_REGEXES = [re.compile(p, re.IGNORECASE) for p in LISTING_KEYWORDS]

IGNORE_PATTERNS = [
    r"^(skip to|menu|search|login|sign in|subscribe|newsletter)\b",
    r"^(facebook|twitter|instagram|linkedin|youtube)\b",
    r"^(privacy policy|terms|copyright|cookies)\b",
    r"^\s*[×✕✖]\s*$",
    r"^(home|about|contact|careers|media|events)\s*$",
]

IGNORE_REGEXES = [re.compile(p, re.IGNORECASE) for p in IGNORE_PATTERNS]


def extract_relevant_content(text: str) -> str:
    lines = text.splitlines()
    relevant = []
    context_window: list[str] = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        if any(rx.match(line) for rx in IGNORE_REGEXES):
            continue

        has_listing_content = any(rx.search(line) for rx in LISTING_REGEXES)

        if has_listing_content:
            relevant.extend(context_window)
            relevant.append(line)
            context_window = []
        else:
            context_window.append(line)
            if len(context_window) > 2:
                context_window.pop(0)

    result = "\n".join(relevant)

    if len(result) < 100:
        return text

    return result


def apply_content_filters(url: str, text: str) -> str:
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter:
        text = site_filter(text)
    text = extract_relevant_content(text)
    return text


# --------------------------------------------------
# Fetching and diffing
# --------------------------------------------------


def fetch_page_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] Fetching {url}: {exc}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")

    debug_print(f"[static] Raw text length for {url}: {len(raw_text)}")

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    text = "\n".join(lines)
    text = apply_content_filters(url, text)

    debug_print(f"[static] Filtered text length for {url}: {len(text)}")
    debug_print(f"[static] First 200 chars for {url}: {text[:200]}")

    text = normalize_whitespace(text)
    return text


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 5,
    context_chars: int = 120,
    max_chars: int = 1200,
) -> str | None:
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

    snippets: list[str] = additions[:max_snippets]
    if len(snippets) < max_snippets:
        snippets.extend(removals[: max_snippets - len(snippets)])

    if not snippets:
        return None

    summary = "\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[...truncated]"
    return summary


def send_ntfy_alert(url: str, diff_summary: str | None) -> None:
    if not diff_summary:
        print(f"[INFO] Change on {url} was too minor or filtered out")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set")
        print(f"[ALERT] Would notify for {url}:\n{diff_summary}")
        raise ValueError("NTFY_TOPIC_URL not configured")

    body = f"{url}\n\nChanges:\n{diff_summary}"
    title = "Housing website updated"

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
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code}")
            raise RuntimeError(f"Notification failed: {resp.status_code}")
    except Exception as exc:
        print(f"[ERROR] Sending alert: {exc}")
        raise


# --------------------------------------------------
# Main loop
# --------------------------------------------------


def run_once() -> None:
    hashes = load_json(HASH_FILE)
    texts = load_json(TEXT_FILE)

    changed_any = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        new_text = fetch_page_text(url)
        if new_text is None:
            continue

        old_text = texts.get(url)
        old_hash = hashes.get(url)

        new_hash = hash_text(new_text)

        if old_text is None or old_hash is None:
            print(f"[INIT] Recording baseline for {url}")
            texts[url] = new_text
            hashes[url] = new_hash
            changed_any = True
            continue

        if new_hash == old_hash:
            print(f"[NOCHANGE] {url}")
            continue

        diff_summary = summarize_diff(old_text, new_text)
        if diff_summary:
            send_ntfy_alert(url, diff_summary)
        else:
            print(f"[INFO] No significant diff for {url}")

        texts[url] = new_text
        hashes[url] = new_hash
        changed_any = True

    if changed_any:
        save_json(HASH_FILE, hashes)
        save_json(TEXT_FILE, texts)
    else:
        print("[INFO] No changes to save")


if __name__ == "__main__":
    run_once()
