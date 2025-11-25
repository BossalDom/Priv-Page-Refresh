import hashlib
import json
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import difflib
from playwright.sync_api import sync_playwright

# --------------- CONFIGURATION ---------------

DYNAMIC_URLS = [
    "https://afny.org/re-rentals",
    "https://iaffordny.com/re-rentals",
    "https://mgnyconsulting.com/listings/",
]

HEADERS = {
    "User-Agent": "PrivPageRefreshDynamic/1.0 (+https://github.com/BossalDom/Priv-Page-Refresh)"
}

HASH_FILE = Path("dynamic_hashes.json")
TEXT_FILE = Path("dynamic_texts.json")

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL")


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
    marker = "Featured Properties"
    idx = text.find(marker)
    if idx != -1:
        text = text[:idx]
    return text


def filter_ahg(text: str) -> str:
    marker = "LOW INCOME HOUSING OPPORTUNITIES"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return text


CONTENT_FILTERS = {
    "https://residenewyork.com/property-status/open-market/": filter_resideny_open_market,
    "https://ahgleasing.com/": filter_ahg,
}

LISTING_LINE_PATTERNS = [
    r"\bApartment\b",
    r"\bApt\b",
    r"\bUnit\b",
    r"\bUnits\b",
    r"\bRent\b",
    r"Per Month",
    r"\bBedroom\b",
    r"\bBedrooms\b",
    r"\bHousehold\b",
    r"\bResults\b",
    r"\bBR\b",
    r"\$[0-9]",
]

LISTING_LINE_REGEXES = [re.compile(p, re.IGNORECASE) for p in LISTING_LINE_PATTERNS]


def keep_listing_lines_only(text: str) -> str:
    lines_in = text.splitlines()
    lines_out: list[str] = []

    for line in lines_in:
        line = line.strip()
        if not line:
            continue
        if any(rx.search(line) for rx in LISTING_LINE_REGEXES):
            lines_out.append(line)

    if not lines_out:
        return text

    return "\n".join(lines_out)


def apply_content_filters(url: str, text: str) -> str:
    site_filter = CONTENT_FILTERS.get(url)
    if site_filter is not None:
        text = site_filter(text)

    text = keep_listing_lines_only(text)
    return text


# --------------- FETCH AND DIFF ---------------

def fetch_rendered_text(url: str) -> str:
    """
    Use Playwright to load the page with JavaScript executed,
    then return filtered, normalized text.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    text = apply_content_filters(url, text)
    text = normalize_whitespace(text)
    return text


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_diff(old_text: str, new_text: str,
                   max_snippets: int = 3,
                   context_chars: int = 80,
                   max_chars: int = 800) -> str | None:
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    snippets: list[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        new_seg = new_text[j1:j2].strip()
        if not new_seg:
            continue
        if len(new_seg) < 3:
            continue

        start = max(0, j1 - context_chars)
        end = min(len(new_text), j2 + context_chars)

        before = new_text[start:j1]
        changed = new_text[j1:j2]
        after = new_text[j2:end]

        snippet = (before + "[[" + changed + "]]" + after).strip()
        snippets.append(snippet)

        if len(snippets) >= max_snippets:
            break

    if not snippets:
        return None

    summary = "\n---\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n[diff truncated]"
    return summary


# --------------- NOTIFICATIONS ---------------

def send_ntfy_alert(url: str, diff_summary: str | None) -> None:
    if not diff_summary:
        print(f"[INFO] Dynamic change on {url} was too minor or filtered out. No alert sent.")
        return

    if not NTFY_TOPIC_URL:
        print("[ERROR] NTFY_TOPIC_URL not set in environment. Set it as a GitHub Actions secret.")
        print(f"[ALERT] Would have sent notification for {url}:\n{diff_summary}")
        raise ValueError("NTFY_TOPIC_URL environment variable not configured")

    body = f"{url}\n\nChanges:\n{diff_summary}"
    title = "Dynamic housing site updated"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-
