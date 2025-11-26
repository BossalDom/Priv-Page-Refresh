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
    "https://wavecrestrentals.com/section.php?id=1",
    "https://yourneighborhoodhousing.com/",
    "https://www.elhrerentals.com/",
]

STATE_DIR = Path(".")
HASH_FILE = STATE_DIR / "hashes.json"
TEXT_FILE = STATE_DIR / "page_texts.json"

# TF Cornerstone specific state file
TFC_INCOME_FILE = STATE_DIR / "tfc_income_options.json"
TFC_URL = "https://tfc.com/about/affordable-re-rentals"

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


def filter_tfc(text: str) -> str:
    """
    Focus on the affordable re-rentals section and income requirement related lines
    so that changes in those dropdowns drive the diff.
    """
    lower = text.lower()
    idx = lower.find("affordable re-rentals")
    if idx != -1:
        text = text[idx:]

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    keep: list[str] = []

    for line in lines:
        if any(
            key in line
            for key in [
                "Income Requirements",
                "Household Income",
                "Minimum Income",
                "Maximum Income",
                "% AMI",
                "AMI ",
            ]
        ):
            keep.append(line)
            continue

        # Keep lines that clearly look like unit summaries
        if re.search(
            r"\b(Studio|1 Bedroom|2 Bedroom|3 Bedroom|1BR|2BR|3BR)\b",
            line,
            re.IGNORECASE,
        ):
            keep.append(line)

    return "\n".join(keep) if keep else text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://ahgleasing.com/": filter_ahg,
    "https://sites.google.com/affordablelivingnyc.com/hpd/home": filter_google_sites,
    "https://streeteasy.com/building/riverton-square": filter_streeteasy,
    "https://tfc.com/about/affordable-re-rentals": filter_tfc,
}

LISTING_KEYWORDS = [
    r"\b(apartment|apt|unit|studio|bedroom|br)\b",
    r"\b(rent|rental|lease|available|vacancy)\b",
    r"\b(household|income|ami|annual)\b",
    r"\$\d{3,}",
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

    summary = "\n".join(snippets)
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
# TF Cornerstone income requirement tracking
# --------------------------------------------------


def extract_tfc_income_options(text: str) -> list[str]:
    """
    Pull out lines that describe income requirements and AMI bands.
    We treat each line as an option and compare sets between runs.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    options: list[str] = []

    for line in lines:
        if any(
            key in line
            for key in [
                "Income Requirements",
                "Household Income",
                "Minimum Income",
                "Maximum Income",
                "% AMI",
                "AMI ",
            ]
        ):
            options.append(line)
            continue

        # Lines with obvious dollar incomes are also interesting
        if re.search(r"\$\d{2,3},\d{3}", line):
            options.append(line)

    unique_sorted = sorted(set(options))
    return unique_sorted


def summarize_tfc_income_changes(
    old_set: set[str], new_set: set[str]
) -> str | None:
    added = new_set - old_set
    removed = old_set - new_set

    if not added and not removed:
        return None

    parts: list[str] = []

    if added:
        parts.append("New TF Cornerstone income options detected:")
        for opt in sorted(added):
            parts.append(f"  • {opt}")

    if removed:
        parts.append("")
        parts.append("Removed TF Cornerstone income options:")
        for opt in sorted(removed):
            parts.append(f"  • {opt}")

    return "\n".join(parts)


def send_tfc_income_alert(url: str, summary: str | None) -> None:
    if not summary:
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set for TF Cornerstone alert")
        print(f"[ALERT] Would notify for {url}:\n{summary}")
        raise ValueError("NTFY_TOPIC_URL not configured")

    body = f"{url}\n\n{summary}"
    title = "TF Cornerstone income requirements updated"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "house,tada",
                "Click": url,
            },
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            print("[OK] TF Cornerstone alert sent")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for TF Cornerstone")
            raise RuntimeError(f"Notification failed: {resp.status_code}")
    except Exception as exc:
        print(f"[ERROR] Sending TF Cornerstone alert: {exc}")
        raise


# --------------------------------------------------
# Main loop
# --------------------------------------------------


def run_once() -> None:
    hashes = load_json(HASH_FILE)
    texts = load_json(TEXT_FILE)
    tfc_income_state = load_json(TFC_INCOME_FILE)

    changed_any = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        new_text = fetch_page_text(url)
        if new_text is None:
            continue

        # Special logic for TF Cornerstone income requirement dropdowns
        if url == TFC_URL:
            options = extract_tfc_income_options(new_text)
            new_set = set(options)
            old_list = tfc_income_state.get(url, [])
            old_set = set(old_list)

            if not old_set:
                print(
                    f"[INIT] Recording {len(new_set)} TF Cornerstone "
                    f"income options"
                )
                tfc_income_state[url] = sorted(new_set)
                changed_any = True
            else:
                if new_set != old_set:
                    summary = summarize_tfc_income_changes(old_set, new_set)
                    send_tfc_income_alert(url, summary)
                    tfc_income_state[url] = sorted(new_set)
                    changed_any = True
                else:
                    print("[NOCHANGE] TF Cornerstone income options unchanged")

            # Still store latest text and hash but skip generic diff
            texts[url] = new_text
            hashes[url] = hash_text(new_text)
            continue

        # Generic handling for other sites
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
        save_json(TFC_INCOME_FILE, tfc_income_state)
    else:
        print("[INFO] No changes to save")


if __name__ == "__main__":
    run_once()
