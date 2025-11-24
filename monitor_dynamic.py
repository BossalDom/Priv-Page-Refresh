from playwright.sync_api import sync_playwright
import hashlib
import json
import os
import difflib
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


def load_json(path: Path):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Could not read {path}: {e}")
    return {}


def save_json(path: Path, data: dict):
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not write {path}: {e}")


# --------------- PAGE FETCH / DIFF -----------------


def get_rendered_text(url: str) -> str | None:
    """Load page with JavaScript executed, then return body text."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="PersonalDynamicMonitor/1.0")
        page.goto(url, wait_until="networkidle", timeout=45000)
        text = page.inner_text("body")
        browser.close()
        return text


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_diff(old_text: str, new_text: str,
                   max_lines: int = 20,
                   max_chars: int = 800) -> str:
    """
    Return a short summary of what changed between old_text and new_text.
    Shows only added lines, truncated.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile="old", tofile="new",
        lineterm=""
    )

    added = []
    for line in diff:
        # Keep only newly added lines, ignore diff headers
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])

    if not added:
        return "(Content changed but diff is empty or too complex)"

    snippet = "\n".join(added[:max_lines])

    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "\n[diff truncated]"

    return snippet


# --------------- NOTIFICATIONS ---------------------


def send_ntfy_alert(url: str, diff_summary: str | None):
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


def run_dynamic_once():
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

        if new_text is None:
            continue

        new_hash = hash_text(new_text)
        old_hash = hash_state.get(url)
        old_text = text_state.get(url)

        # First time seeing this URL or no stored text yet
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
