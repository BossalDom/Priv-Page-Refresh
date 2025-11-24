import requests
from bs4 import BeautifulSoup
import hashlib
import json
import os

# --------------- CONFIGURATION ---------------

# Put all the pages you want to monitor here.
# Each one is a string in quotes, separated by commas.
URLS = [
    "https://www.nyc.gov/site/hpd/services-and-information/find-affordable-housing-re-rentals.page",
    "https://afny.org/re-rentals",
    "https://www.google.com/search?q=Affordable+for+New+York+CGMR+Compliance+City5+Clinton+Management+Fifth+Avenue+Committee+iAfford+Infinite+Horizons+Ibis+Advisors+K%26G+Upright+Langsam+Property+Services+MGNY+Micki+Garcia+Realty+Pronto+Housing+Reclaim+HDFC+Related+Management+Company+Reside+New+York+RiseBoro+Riverton+Square+SJP+Tax+Consultants+Inc.+SOIS+Spring+Leasing+and+Management+Stanton+Norfolk+Inc.+Tax+Solute+Taxace+NY+TF+Cornerstone+The+Bridge+Wavecrest+Rentals+Your+Neighborhood+Housing&rlz=1C1GCEA_enUS1173US1173&sourceid=chrome&ie=UTF-8",
    "https://cgmrcompliance.com/housing-opportunities-1",
    "https://city5.nyc/",
    "https://www.clintonmanagement.com/availabilities/affordable/",
    "https://fifthave.org/re-rental-availabilities/",
    "https://iaffordny.com/re-rentals",
    "https://ihrerentals.com/",
    "https://ibis.powerappsportals.com/",
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
    "https://east-village-homes-owner-llc.rentcafewebsite.com/",
    "https://sites.google.com/affordablelivingnyc.com/hpd/home",
    "https://www.taxaceny.com/projects-8",
    "https://tfc.com/about/affordable-re-rentals",
    "https://www.thebridgeny.org/news-and-media",
    "https://wavecrestrentals.com/section.php?id=1",
    "https://yourneighborhoodhousing.com/",
]


HASH_FILE = "hashes.json"

# ntfy topic URL comes from a GitHub secret, not hard coded
NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL")

# --------------- HELPERS ---------------------


def load_state():
    if os.path.exists(HASH_FILE):
        try:
            with open(HASH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Could not read {HASH_FILE}: {e}")
    return {}


def save_state(state):
    try:
        with open(HASH_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not write {HASH_FILE}: {e}")


def get_page_hash(url):
    try:
        headers = {
            "User-Agent": "PersonalMonitor/1.0 (+github actions)"
        }
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    except Exception as e:
        print(f"[ERROR] Fetching {url}: {e}")
        return None


def send_ntfy_alert(url):
    if not NTFY_TOPIC_URL:
        print("[WARN] NTFY_TOPIC_URL not set, skipping notification")
        return

    message = f"The website changed:\n{url}"
    title = "Website updated"

    try:
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=message.encode("utf-8"),
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


def run_once():
    state = load_state()
    changed_any = False

    for url in URLS:
        print(f"[INFO] Checking {url}")
        new_hash = get_page_hash(url)
        if new_hash is None:
            continue

        old_hash = state.get(url)

        # First time seeing this URL in state
        if old_hash is None:
            print(f"[INIT] Recording initial hash for {url}")
            state[url] = new_hash
            changed_any = True
            continue

        if new_hash != old_hash:
            print(f"[CHANGE] Detected change on {url}")
            send_ntfy_alert(url)
            state[url] = new_hash
            changed_any = True
        else:
            print(f"[NOCHANGE] No change on {url}")

    if changed_any:
        save_state(state)
    else:
        print("[INFO] No changes to save")


if __name__ == "__main__":
    run_once()
