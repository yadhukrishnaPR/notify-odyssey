"""
BMS Ticket Checker — Persistent Loop Mode for GitHub Actions.
State is persisted via a JSON artifact. Tracks and alerts via ntfy.sh.
Routes API requests through local Cloudflare WARP proxy.
"""

import os
import re
import sys
import json
import time
import threading
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these or set via env vars
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    "url": os.getenv(
        "BMS_URL",
        "https://in.bookmyshow.com/movies/hyderabad/spider-man-brand-new-day/buytickets/ET00502600"
    ),
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = from URL
    "theatre": os.getenv("BMS_THEATRE", ""),       # substring filter, empty = all
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
    "ntfy_topic": os.getenv("NTFY_TOPIC", ""),     # Your custom ntfy topic
}

STATE_FILE = "bms_state.json"

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}


# ──────────────────────────────────────────────────────────────────────
# DATA
# ──────────────────────────────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)

@dataclass
class DateInfo:
    date_code: str
    status: str


# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),
        "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    
    # Route through the local Cloudflare WARP SOCKS5 proxy
    proxies = {
        "http": "socks5h://127.0.0.1:40000",
        "https": "socks5h://127.0.0.1:40000"
    }

    try:
        resp = requests.get(
            API_URL, 
            headers=headers, 
            params=params, 
            proxies=proxies, 
            timeout=15
        )
        now_log = datetime.now().strftime("%H:%M:%S")
        print(f"[{now_log}] Scrape [{date_code or 'URL'}]: HTTP {resp.status_code}")
        
        if resp.status_code == 200:
            return resp.json()
            
    except requests.RequestException as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scrape Failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates


def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not date_code and re.match(
                            r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "")
                                     or sa.get("attributes", "")),
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=ca,
                        ))
                    shows.append(show)
    return shows


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    kws = [k.strip().lower() for k in theatre_filter.split(",")
           if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",")
                    if d.strip()) if date_codes else set()

    for s in shows:
        if kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
                continue

        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue

        result.append(s)
    return result


# ──────────────────────────────────────────────────────────────────────
# STATE (for change detection between runs)
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def build_state(shows, dates):
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
            }

    date_state = {
        d.date_code: d.status for d in dates
    }

    return {"shows": show_state, "dates": date_state}


def detect_changes(old_state, new_state):
    changes = []

    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    for dc, status in new_dates.items():
        old_status = old_dates.get(dc)
        if (old_status == "NOT_OPEN"
                and status in ("BOOKABLE", "AVAILABLE")):
            changes.append(f"📅 NEW DATE OPENED: {dc}")

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        changes.append(
            f"🆕 NEW: {s['venue']} {s['time']} [{s['date']}] "
            f"— {s['cat']} ₹{s['price']}"
        )

    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(
                new_s["status"], ("UNKNOWN", "⚪")
            )
            changes.append(
                f"{ico} BACK: {new_s['venue']} {new_s['time']} "
                f"[{new_s['date']}] — {new_s['cat']} → {lbl}"
            )

    return changes


# ──────────────────────────────────────────────────────────────────────
# NOTIFICATION (ntfy.sh)
# ──────────────────────────────────────────────────────────────────────
def _ntfy_worker(topic, message, headers):
    """Background worker that sends the notification 10 times, 1 minute apart."""
    for i in range(10):
        try:
            resp = requests.post(
                f"https://ntfy.sh/{topic}",
                data=message.encode('utf-8'),
                headers=headers,
                timeout=10
            )
            if resp.status_code == 200:
                print(f"  ✅ [Thread] Push {i+1}/10 sent to ntfy.sh/{topic}")
            else:
                print(f"  ❌ [Thread] ntfy failed [{resp.status_code}]: {resp.text}")
        except requests.RequestException as e:
            print(f"  ❌ [Thread] ntfy error: {e}")
        
        if i < 9:
            time.sleep(60)

def send_ntfy_alert(changes, movie_info):
    topic = CONFIG["ntfy_topic"].strip()
    if not topic:
        print("  ⚠️  Skipping ntfy — NTFY_TOPIC not set.")
        return

    movie_name = movie_info.get("name", "Movie")
    message = "\n".join(changes)

    headers = {
        "Title": f"🎬 BMS Alert: {movie_name}",
        "Priority": "urgent",
        "Tags": "ticket,popcorn"
    }

    print("  🚀 Spawning background thread for 10-minute notification sequence...")
    # Spawn the background worker as a daemon thread
    thread = threading.Thread(
        target=_ntfy_worker,
        args=(topic, message, headers),
        daemon=True
    )
    thread.start()


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def main():
    parsed = parse_bms_url(CONFIG["url"])
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")

    if not event_code or not region_slug:
        print("  ❌ Invalid BMS_URL. Could not extract event/region.")
        sys.exit(1)

    region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

    raw_dates = CONFIG["dates"].strip()
    if raw_dates:
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]

    print("🚀 Starting 5h 55m BMS Monitor on GitHub Actions")
    print(f"  Event: {event_code} | Target Date: {date_list}")
    
    # ────────────────────────────────────────────────────────────
    # PHASE 1: HYDRATE INITIAL STATE (Eager Evaluation)
    # ────────────────────────────────────────────────────────────
    print("💾 Loading initial state from disk...")
    current_state = load_state()
    if not current_state:
        print("  ℹ️ No existing state found. Proceeding with cold start (all scraped shows treated as NEW).")
    else:
        num_shows = len(current_state.get('shows', {}))
        print(f"  ✅ Loaded existing state with {num_shows} tracked shows.")
        
    start_time = time.time()
    # Exactly 5 hours and 55 minutes runner limit protection
    max_duration = (5 * 3600) + (55 * 60) 

    while (time.time() - start_time) < max_duration:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        all_shows, all_dates = [], []
        movie_info = {"name": "Unknown", "language": ""}

        try:
            for dc in date_list:
                data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
                if not data:
                    continue

                if movie_info["name"] == "Unknown":
                    movie_info = parse_movie_info(data)

                all_dates.extend(parse_dates(data))
                all_shows.extend(parse_shows(data))

            if not all_shows:
                pass # Suppress noisy "No showtimes found" logs if dates aren't open yet
            else:
                filtered = filter_shows(all_shows, CONFIG["theatre"], CONFIG["time_period"], CONFIG["dates"])
                
                # Build the state for this exact moment
                new_state = build_state(filtered, all_dates)

                # ────────────────────────────────────────────────────────────
                # PHASE 2: IMMEDIATE COMPARISON & ALERT
                # ────────────────────────────────────────────────────────────
                changes = detect_changes(current_state, new_state)

                if changes:
                    print(f"\n[{now_str}] ⚡ {len(changes)} change(s) detected:")
                    for c in changes:
                        print(f"     {c}")
                    send_ntfy_alert(changes, movie_info)

                # ────────────────────────────────────────────────────────────
                # PHASE 3: SYNCHRONIZE STATE (Memory + Disk)
                # ────────────────────────────────────────────────────────────
                current_state = new_state
                save_state(current_state)

        except Exception as e:
            print(f"[{now_str}] ❌ Error during execution: {e}")
        
        # Flush stdout explicitly to ensure GitHub Actions live logs update immediately
        sys.stdout.flush() 
        time.sleep(30)
        
    print("🛑 Reached 5h 55m limit. Exiting cleanly to trigger GitHub Action commit.")


if __name__ == "__main__":
    main()
