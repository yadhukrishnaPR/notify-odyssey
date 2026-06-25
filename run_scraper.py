import requests
import time
import json
import os
import re
import subprocess
from datetime import datetime

# --- CONFIGURATION ---
DATES = ["20260717", "20260718", "20260719"]
VENUE_CODE = "PRHN"
EVENT_CODE = "ET00452034"
STATE_FILE = "state.json"
MAX_RUNTIME_SECONDS = (5 * 3600) + (55 * 60) # 5 hours 55 mins

GET_HEADERS = {
    "Host": "in.bookmyshow.com",
    "Content-Type": "application/json",
    "X-Latitude": "17.385044",
    "X-Subregion-Code": "HYD",
    "X-App-Code": "MOBAND2",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 10; Android SDK built for x86_64 Build/QSR1.211112.011)",
    "X-App-Version": "18.2.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

POST_HEADERS = {
    "Host": "services-in.bookmyshow.com",
    "X-Timeout": "10",
    "X-Latitude": "17.385044",
    "X-Subregion-Code": "HYD",
    "X-App-Code": "MOBAND2",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 10; Android SDK built for x86_64 Build/QSR1.211112.011)",
    "X-App-Version": "18.2.3",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Encoding": "gzip, deflate, br"
}

def load_state():
    # Sync with remote before reading, to catch updates from the overlapping runner
    subprocess.run(["git", "pull", "origin", "main"], check=False)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_state(state, commit_msg="Update seat state"):
    # Pull rebase right before writing to avoid merge conflicts with the overlapping runner
    subprocess.run(["git", "pull", "origin", "main"], check=False)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    
    subprocess.run(["git", "add", STATE_FILE], check=False)
    # Check if there are changes to commit
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if STATE_FILE in status.stdout:
        subprocess.run(["git", "commit", "-m", commit_msg], check=False)
        # Push retry loop in case of intense race condition
        for _ in range(3):
            push_res = subprocess.run(["git", "push", "origin", "main"], check=False)
            if push_res.returncode == 0:
                break
            time.sleep(2)
            subprocess.run(["git", "pull", "origin", "main"], check=False)

def trigger_ntfy(message):
    print(f"ALERTING: {message}")
    for _ in range(3):
        try:
            requests.post(
                "https://ntfy.sh/odssy_stlyt",
                data=message.encode('utf-8'),
                headers={"Priority": "urgent"}
            )
        except Exception as e:
            print(f"Ntfy failed: {e}")
        time.sleep(15)

def fetch_sessions():
    sessions = []
    for date_code in DATES:
        url = f"https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary?eventCode={EVENT_CODE}&dateCode={date_code}&regionCode=HYD&venueCode={VENUE_CODE}"
        try:
            resp = requests.get(url, headers=GET_HEADERS, timeout=10)
            data = resp.json()
            for show in data.get("data", {}).get("showTimes", []):
                if show.get("attributes") == "PCX SCREEN":
                    sessions.append({
                        "sessionId": show["sessionId"],
                        "dateCode": show["showDateCode"],
                        "time": show["showTime"]
                    })
        except Exception as e:
            print(f"Error fetching sessions for {date_code}: {e}")
    return sessions

def fetch_seat_layout(session_id):
    url = "https://services-in.bookmyshow.com/doTrans.aspx"
    payload = f"strParam4=&strParam5=Y&strParam6=&strParam7=N&strParam1={session_id}&strParam2=WEB&strParam3=&strVenueCode={VENUE_CODE}&lngTransactionIdentifier=0&strAppCode=MOBAND2&strFormat=json&strCommand=GETSEATLAYOUT"
    try:
        resp = requests.post(url, headers=POST_HEADERS, data=payload, timeout=10)
        return resp.json().get("BookMyShow", {}).get("strData", "")
    except Exception as e:
        print(f"Error fetching layout for session {session_id}: {e}")
        return ""

def parse_layout(str_data):
    if not str_data: return {}
    
    parts = str_data.split("||")
    rows_data = parts[1] if len(parts) > 1 else parts[0]
    rows = rows_data.split("|")
    
    available_seats_by_row = {}
    
    for row in rows:
        if not row or ":" not in row: continue
        elements = row.split(":")
        row_letter = elements[1]
        seats = elements[2:]
        
        available_in_row = []
        for seat in seats:
            # Match any status EXCEPT "2" (Booked). Must have exactly 2-digit type code, then captures the seat number.
            match = re.search(r"A[^2]\d{2}(\d+)\+", seat)
            if match:
                available_in_row.append(match.group(1))
                
        if available_in_row:
            available_seats_by_row[row_letter] = available_in_row
            
    return available_seats_by_row

def main():
    start_time = time.time()
    
    print("Fetching valid sessions...")
    target_sessions = fetch_sessions()
    print(f"Found {len(target_sessions)} PCX SCREEN sessions.")
    
    if not target_sessions:
        print("No valid sessions found. Exiting.")
        return

    # Check if first run ever (determines if we alert)
    state = load_state()
    is_first_run = len(state) == 0
    if is_first_run:
        print("Empty state found. Initializing baseline silently...")

    cycle_count = 1
    
    # Outer Loop: Run until 5 hours and 55 minutes is reached
    while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
        print(f"--- Starting Polling Cycle {cycle_count} ---")
        
        # Load state fresh at the start of loop in case overlapping runner updated it
        state = load_state()
        state_changed_this_cycle = False
        
        for session in target_sessions:
            s_id = session["sessionId"]
            s_date = session["dateCode"]
            s_time = session["time"]
            
            # 15 SECOND COOLDOWN BETWEEN API HITS
            time.sleep(15) 
            
            str_data = fetch_seat_layout(s_id)
            current_seats = parse_layout(str_data)
            
            # Count current total
            current_total = sum(len(seats) for seats in current_seats.values())
            
            # Initialize state block for session if missing
            if s_id not in state:
                state[s_id] = {"date": s_date, "time": s_time, "total": 0, "rows": {}}
            
            previous_total = state[s_id].get("total", 0)
            previous_rows = state[s_id].get("rows", {})
            
            # Check if seats unblocked (Total increased, or specific newly available seats)
            newly_unblocked_count = 0
            unblocked_rows_list = []
            
            for row, seats in current_seats.items():
                old_seats_in_row = previous_rows.get(row, [])
                # Find seats that are in current_seats but were NOT in old_seats
                new_seats = set(seats) - set(old_seats_in_row)
                
                if new_seats:
                    newly_unblocked_count += len(new_seats)
                    unblocked_rows_list.append(row)
            
            # Evaluate if an alert is needed
            if newly_unblocked_count > 0:
                print(f"Detected unblocks! Session {s_id} (+{newly_unblocked_count} seats)")
                if not is_first_run:
                    rows_str = ", ".join(sorted(unblocked_rows_list))
                    msg = f"Seats unblocked at {rows_str} row. Date: {s_date} Time: {s_time} total {newly_unblocked_count} seats are unblocked."
                    trigger_ntfy(msg)
                
                # We update the memory immediately
                state[s_id]["rows"] = current_seats
                state[s_id]["total"] = current_total
                state_changed_this_cycle = True

            # Important: We also need to update the state if seats were BOOKED (decreased)
            # So the baseline accurately reflects the drops, otherwise we never alert if it bounces back
            elif current_total < previous_total:
                state[s_id]["rows"] = current_seats
                state[s_id]["total"] = current_total
                state_changed_this_cycle = True
                print(f"Seats booked for Session {s_id}. Total dropped from {previous_total} to {current_total}.")

        # If anything changed in this cycle, push to Git immediately
        if state_changed_this_cycle:
            save_state(state, f"State update at cycle {cycle_count}")
            
        if is_first_run:
            is_first_run = False
            print("First run baseline established.")
            
        cycle_count += 1
        
    print("Time limit reached (5h 55m). Saving final state and gracefully shutting down.")
    final_state = load_state() # final fetch
    save_state(final_state, "Final runner shutdown save")

if __name__ == "__main__":
    main()
