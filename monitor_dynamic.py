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
    "https://city5.nyc/",
    "https://ibis.powerappsportals.com/",
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
]

HEADERS = {
    "User-Agent": "PrivPageRefreshDynamic/1.0 (+https://github.com/BossalDom/Priv-Page-Refresh)"
}

HASH_FILE = Path("dynamic_hashes.json")
TEXT_FILE = Path("dynamic_texts.json")

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL")

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
    lines = text.splitlines()
    relevant_lines: list[str] = []
    context_window: list[str] = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        if any(rx.match(line) for rx in IGNORE_REGEXES):
            continue

        has_listing_content = any(rx.search(line) for rx in LISTING_REGEXES)

        if has_listing_content:
            if context_window:
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


def apply_content_filters(url: str, text: str) -> str:
    text = extract_relevant_content(text)
    return text


# --------------- FETCH AND DIFF ---------------

def fetch_rendered_text(url: str) -> str:
    """
    Use Playwright to load the page with JavaScript executed.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        # small extra wait in case of delayed content
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text(separator="\n")
    debug_print(f"[dynamic] Raw text length for {url}: {len(raw_text)} chars")

    text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
    text = apply_content_filters(url, text)

    debug_print(f"[dynamic] Filtered text length for {url}: {len(text)} chars")
    debug_print(f"[dynamic] First 200 chars: {text[:200]}")

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
            print(f"[OK] Dynamic alert sent for {url}")
        else:
            print(f"[ERROR] ntfy returned {resp.status_code} for dynamic url {url}")
            raise RuntimeError(f"Notification failed: {resp.status_code}")
    except Exception as e:
        print(f"[ERROR] Sending dynamic ntfy alert: {e}")
        raise


# --------------- MAIN LOOP ---------------

def run_dynamic_once() -> None:
    hash_state = load_json(HASH_FILE)
    text_state = load_json(TEXT_FILE)
    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic {url}")
        try:
            new_text = fetch_rendered_text(url)
        except Exception as e:
            print(f"[ERROR] Rendering {url}: {e}")
            continue

        new_hash = hash_text(new_text)
        old_hash = hash_state.get(url)
        old_text = text_state.get(url)

        if old_hash is None or old_text is None:
            print(f"[INIT] Recording initial dynamic state for {url}")
            hash_state[url] = new_hash
            text_state[url] = new_text
            changed_any = True
            continue

        if new_hash != old_hash:
            print(f"[CHANGE] Detected dynamic change on {url}")
            diff_summary = summarize_diff(old_text, new_text)
            if diff_summary:
                print("[DIFF]\n" + diff_summary)
            send_ntfy_alert(url, diff_summary)
            hash_state[url] = new_hash
            text_state[url] = new_text
            changed_any = True
        else:
            print(f"[NOCHANGE] No relevant dynamic change on {url}")

    if changed_any:
        save_json(HASH_FILE, hash_state)
        save_json(TEXT_FILE, text_state)
    else:
        print("[INFO] No dynamic changes to save.")


if __name__ == "__main__":
    run_dynamic_once()
