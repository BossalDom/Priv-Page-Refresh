from playwright.sync_api import sync_playwright
import hashlib
import json
import os
from pathlib import Path

# --------------- CONFIGURATION ---------------

DYNAMIC_URLS = [
    "https://afny.org/re-rentals",
    "https://iaffordny.com/re-rentals",
    # add others here if they truly need JS rendering
]

STATE_FILE = Path("dynamic_hashes.json")
NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL")

# --------------- HELPERS ---------------------


def load_state():
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Could not read {STATE_FILE}: {e}")
    return {}


def save_state(state):
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not write {STATE_FILE}: {e}")


def get_rendered_text(url):
    """Use Playwright to load the page with JavaScript executed, then return body text."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="PersonalDynamicMonitor/1.0")
        page.goto(url, wait_until="networkidle", timeout=45000)
        text = page.inner_text("body")
        browser.close()
        return text


def get_page_hash(url):
    try:
        text = get_rendered_text(url)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Exception as e:
        print(f"[ERROR] Rendering {url}: {e}")
        return None


def send_ntfy_alert(url):
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set, skipping notification")
        return

    message = f"The dynamic page changed:\n{url}"
    title = "Dynamic website updated"

    try:
        import requests

        resp = requests.post(
            NTFY_TOPIC_URL,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "4"},
            timeout=15,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[WARN] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert: {e}")


def run_dynamic_once():
    state = load_state()
    changed_any = False

    for url in DYNAMIC_URLS:
        print(f"[INFO] Checking dynamic {url}")
        new_hash = get_page_hash(url)
        if new_hash is None:
            continue

        old_hash = state.get(url)

        if old_hash is None:
            print(f"[INIT] Recording initial dynamic hash for {url}")
            state[url] = new_hash
            changed_any = True
            continue

        if new_hash != old_hash:
            print(f"[CHANGE] Detected dynamic change on {url}")
            send_ntfy_alert(url)
            state[url] = new_hash
            changed_any = True
        else:
            print(f"[NOCHANGE] No dynamic change on {url}")

    if changed_any:
        save_state(state)
    else:
        print("[INFO] No dynamic changes to save")


if __name__ == "__main__":
    run_dynamic_once()
