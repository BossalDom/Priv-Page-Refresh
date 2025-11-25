from playwright.sync_api import sync_playwright
import hashlib
import json
import os
import difflib
import re
from pathlib import Path
import requests

# --------------- CONFIGURATION ---------------

DYNAMIC_URLS = [
    "https://afny.org/re-rentals",
    "https://iaffordny.com/re-rentals",
    "https://mgnyconsulting.com/listings/",
]

HASH_FILE = Path("dynamic_hashes.json")
TEXT_FILE = Path("dynamic_texts.json")

NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL")

# --------------- STATE HELPERS ---------------------


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


# --------------- TEXT NORMALIZATION / DIFF ---------


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_rendered_text(url: str) -> str:
    """
    Use Playwright to load the page with JavaScript executed, then return normalized body text.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="PersonalDynamicMonitor/1.0")
        page.goto(url, wait_until="networkidle", timeout=45000)
        raw = page.inner_text("body")
        browser.close()
        return normalize_text(raw)


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 3,
    context_chars: int = 80,
    max_chars: int = 800,
) -> str:
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    snippets: list[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        new_segment = new_text[j1:j2].strip()
        if not new_segment:
            continue

        if len(new_segment) <= 1 and (i2 - i1) <= 1:
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
        return "(Content changed but differences are too small or layout-only)"

    summary = "\n---\n".join(snippets)

    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n[diff truncated]"

    return summary


# --------------- NOTIFICATIONS ---------------------


def send_ntfy_alert(url: str, diff_summary: str | None) -> None:
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set, skipping notification")
        return

    if diff_summary:
        body = f"{url}\n\nChanges:\n{diff_summary}"
    else:
        body = f"The dynamic website changed:\n{url}"

    title = "Dynamic website updated"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "4"},
            timeout=15,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[WARN] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert: {e}")


# --------------- MAIN LOOP ------------------------


def run_dynamic_once() -> None:
    hash_state = load_json(HASH_FILE)
    text_state = load_json(TEXT_FILE)
    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic {url}")
        try:
            new_text = get_rendered_text(url)
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
            print("[DIFF]\n" + diff_summary)
            send_ntfy_alert(url, diff_summary)
            hash_state[url] = new_hash
            text_state[url] = new_text
            changed_any = True
        else:
            print(f"[NOCHANGE] No dynamic change on {url}")

    if changed_any:
        save_json(HASH_FILE, hash_state)
        save_json(TEXT_FILE, text_state)
    else:
        print("[INFO] No dynamic changes to save")


if __name__ == "__main__":
    run_dynamic_once()
