#!/usr/bin/env python3
"""
Static website monitor.

Uses requests + BeautifulSoup to fetch pages, normalizes text,
applies some light filters, and notifies via ntfy when content changes.

State is stored in:
  - hashes.json        (hash of normalized text)
  - page_texts.json    (last normalized text per url)
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# ------------- config -------------

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# List of sites that do not need JS rendering
# Fill this with the sites you want treated as simple static pages
URLS: List[str] = [
    # Example static sites
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://yourneighborhoodhousing.com/",
    # add or remove as needed
]

HASH_FILE = Path("hashes.json")
TEXT_FILE = Path("page_texts.json")

# -------- helpers for state --------


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[WARN] Could not read {path}: {exc}")
        return {}


def save_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    tmp.replace(path)


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- content filters ----------

LISTING_KEYWORDS = [
    r"\b(apartment|apt|unit|studio|bedroom|br)\b",
    r"\b(rent|rental|lease|available|vacancy)\b",
    r"\b(household|income|ami|annual)\b",
    r"\$\d{1,3}(?:,\d{3})*",
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
    r"^[\s×✕✖]\s*$",
    r"^(home|about|contact|careers|media|events)\s*$",
]

IGNORE_REGEXES = [re.compile(p, re.IGNORECASE) for p in IGNORE_PATTERNS]


def extract_relevant_content(text: str) -> str:
    """
    Extract listing relevant content while preserving some context.

    Less aggressive than pure line based filtering.
    """
    lines = text.splitlines()
    relevant: List[str] = []
    context: List[str] = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        if any(rx.match(line) for rx in IGNORE_REGEXES):
            continue

        has_listing = any(rx.search(line) for rx in LISTING_REGEXES)

        if has_listing:
            relevant.extend(context)
            relevant.append(line)
            context = []
        else:
            context.append(line)
            if len(context) > 2:
                context.pop(0)

    result = "\n".join(relevant)

    # If we stripped almost everything, return original text
    if len(result) < 100:
        return text

    return result


# Site specific filters (simple versions, can expand later)


def filter_resideny_open_market(text: str) -> str:
    marker = "Open Market"
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


def filter_ahg(text: str) -> str:
    marker = "Affordable Housing Group"
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


# ---------- fetch and diff ----------


def fetch_page_text(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] Fetching {url}: {exc}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_text = soup.get_text(separator="\n")
    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = apply_content_filters(url, text)
    text = normalize_whitespace(text)
    return text


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 5,
    context_chars: int = 120,
    max_chars: int = 1200,
) -> Optional[str]:
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    additions: List[str] = []
    removals: List[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("insert", "replace"):
            new_seg = new_text[j1:j2].strip()
            if new_seg and len(new_seg) >= 10:
                start = max(0, j1 - context_chars)
                end = min(len(new_text), j2 + context_chars)
                snippet = new_text[start:end].strip()
                additions.append(f"+ {snippet}")

        if tag in ("delete", "replace"):
            old_seg = old_text[i1:i2].strip()
            if old_seg and len(old_seg) >= 10:
                removals.append(f"- {old_seg[:100]}")

    snippets: List[str] = []
    snippets.extend(additions[:max_snippets])
    if len(snippets) < max_snippets:
        snippets.extend(removals[: max_snippets - len(snippets)])

    if not snippets:
        return None

    summary = "\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n\n[...truncated]"
    return summary


# ---------- notifications ----------


def send_ntfy_alert(url: str, diff_summary: Optional[str]) -> None:
    """
    Send ntfy notification for static pages.

    Headers must be plain ASCII to satisfy latin 1 encoding.
    Emojis can go in the body, not in the headers.
    """
    if not diff_summary:
        print(f"[INFO] Change on {url} was too minor. No alert sent.")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set. Configure it in GitHub secrets.")
        print(f"[ALERT] Would have sent for {url}:\n{diff_summary}")
        raise ValueError("NTFY_TOPIC_URL environment variable not configured")

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
            print(f"[ERROR] ntfy returned {resp.status_code} for {url}")
            raise RuntimeError(f"Notification failed: {resp.status_code}")
    except Exception as exc:
        print(f"[ERROR] Sending ntfy alert: {exc}")
        raise


# ---------- main loop ----------


def run_once() -> None:
    hashes = load_json(HASH_FILE)
    texts = load_json(TEXT_FILE)

    changed_any = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        new_text = fetch_page_text(url)
        if new_text is None:
            continue

        new_hash = hash_text(new_text)
        old_hash = hashes.get(url)

        if old_hash is None:
            print(f"[INIT] Recording baseline for {url}")
            hashes[url] = new_hash
            texts[url] = new_text
            changed_any = True
            continue

        if new_hash == old_hash:
            print(f"[NOCHANGE] {url}")
            continue

        print(f"[CHANGE] {url}")
        old_text = texts.get(url, "")
        diff_summary = summarize_diff(old_text, new_text)
        send_ntfy_alert(url, diff_summary)

        hashes[url] = new_hash
        texts[url] = new_text
        changed_any = True

    if changed_any:
        save_json(HASH_FILE, hashes)
        save_json(TEXT_FILE, texts)
    else:
        print("[INFO] No updates to save.")


if __name__ == "__main__":
    run_once()
