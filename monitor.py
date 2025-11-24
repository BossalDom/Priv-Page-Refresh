import requests
from bs4 import BeautifulSoup
import hashlib
import json
import os
import difflib
from pathlib import Path

# --------------- CONFIGURATION ---------------

URLS = [
    # put all your static URLs here
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://city5.nyc/",
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://fifthave.org/re-rental-availabilities/",
    "https://ihrerentals.com/",
    "https://ibis.powerappsportals.com/",
    "https://kgupright.com/",
    "https://www.langsampropertyservices.com/affordable-rental-opportunities",
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
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://www.taxaceny.com/projects-8",
    "https://tfc.com/about/affordable-re-rentals",
    "https://www.thebridgeny.org/news-and-media",
    "https://wavecrestrentals.com/section.php?id=1",
    "https://yourneighborhoodhousing.com/",
]

HASH_FILE = Path("hashes.json")
TEXT_FILE = Path("page_texts.json")

# ntfy topic URL comes from GitHub secret
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


def fetch_page_text(url: str) -> str | None:
    try:
        headers = {
            "User-Agent": "PersonalMonitor/1.0 (+github actions)"
        }
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        return text

    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_diff(old_text: str, new_text: str,
                   max_lines: int = 20,
                   max_chars: int = 800) -> str:
    """
    Return a short summary of what changed between old_text and new_text.
    Shows only added/changed lines, truncated.
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
        # Optionally you could also keep lines starting with "-" for removals

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
        body = f"The website changed:\n{url}"

    title = "Website updated"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "4",
            },
            timeout=10,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[WARN] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert: {e}")


# --------------- MAIN LOOP ------------------------


def run_once():
    hash_state = load_json(HASH_FILE)
    text_state = load_json(TEXT_FILE)
    changed_any = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        new_text = fetch_page_text(url)
        if new_text is None:
            continue

        new_hash = hash_text(new_text)
        old_hash = hash_state.get(url)
        old_text = text_state.get(url)

        # First time seeing this URL
        if old_hash is None or old_text is None:
            print(f"[INIT] Recording initial state for {url}")
            hash_state[url] = new_hash
            text_state[url] = new_text
            changed_any = True
            continue

        if new_hash != old_hash:
            print(f"[CHANGE] Detected change on {url}")
            diff_summary = summarize_diff(old_text, new_text)
            print("[DIFF]\n" + diff_summary)
            send_ntfy_alert(url, diff_summary)
            hash_state[url] = new_hash
            text_state[url] = new_text
            changed_any = True
        else:
            print(f"[NOCHANGE] No change on {url}")

    if changed_any:
        save_json(HASH_FILE, hash_state)
        save_json(TEXT_FILE, text_state)
    else:
        print("[INFO] No changes to save")


if __name__ == "__main__":
    run_once()
