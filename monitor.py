import requests
from bs4 import BeautifulSoup
import hashlib
import json
import os
import difflib
import re
from pathlib import Path

# --------------- CONFIGURATION ---------------

URLS = [
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
    """Collapse whitespace so layout-only changes are ignored."""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_page_text(url: str) -> str | None:
    try:
        headers = {"User-Agent": "PersonalMonitor/1.0 (+github actions)"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        raw = soup.get_text(separator=" ", strip=True)
        return normalize_text(raw)

    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_diff(
    old_text: str,
    new_text: str,
    max_snippets: int = 3,
    context_chars: int = 80,
    max_chars: int = 800,
) -> str:
    """
    Show a few short snippets from new_text where changes occurred.
    Changed portions are wrapped in [[double brackets]].
    """
    sm = difflib.SequenceMatcher(None, old_text, new_text)
    snippets: list[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        new_segment = new_text[j1:j2].strip()
        if not new_segment:
            continue

        # Ignore extremely tiny changes that are just whitespace/punctuation
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
        body = f"The website changed:\n{url}"

    title = "Website updated"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "4"},
            timeout=10,
        )
        if 200 <= resp.status_code < 300:
            print(f"[OK] Alert sent for {url}")
        else:
            print(f"[WARN] ntfy returned {resp.status_code} for {url}")
    except Exception as e:
        print(f"[ERROR] Sending ntfy alert: {e}")


# --------------- MAIN LOOP ------------------------


def run_once() -> None:
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
