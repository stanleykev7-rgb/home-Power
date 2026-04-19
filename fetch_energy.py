"""
fetch_energy.py — AC Energy Tracker v3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fetches Add Electricity events from Tuya Cloud device log since last fetch.
Each event is an incremental kWh value for a ~10-min reporting window.
Divisor: 1000 (confirmed from Device Logs showing e.g. raw 127 = 0.13 kWh)

KSEB ToD Rates (above 250 units slab):
  T1 Day   06:00–18:00  ₹6.75/unit
  T2 Peak  18:00–22:00  ₹9.375/unit
  T3 Night 22:00–06:00  ₹7.50/unit
"""

import os, json, sys, time
from datetime import datetime, timezone, timedelta
import tinytuya

# ── config ────────────────────────────────────────────────────────────────────
API_ID     = os.environ["TUYA_API_ID"]
API_SECRET = os.environ["TUYA_API_SECRET"]
DEVICE_ID  = os.environ["TUYA_DEVICE_ID"]
REGION     = os.environ.get("TUYA_REGION", "in")
DATA_FILE  = "data/energy_log_ac1.json"

IST = timezone(timedelta(hours=5, minutes=30))

TOD = {
    "T1_day":   {"label": "Day (T1)",   "rate": 6.750},
    "T2_peak":  {"label": "Peak (T2)",  "rate": 9.375},
    "T3_night": {"label": "Night (T3)", "rate": 7.500},
}

# Session detection thresholds
STANDBY_MAX_KWH   = 0.02   # below this = AC off / standby
SESSION_START_KWH = 0.05   # above this after standby = new session
SESSION_GAP_MINS  = 15     # gap > this between events = new session

# ── helpers ───────────────────────────────────────────────────────────────────
def get_tod_slot(dt_ist: datetime) -> str:
    h = dt_ist.hour
    if 6 <= h < 18:   return "T1_day"
    elif 18 <= h < 22: return "T2_peak"
    else:              return "T3_night"

def load_log() -> list:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            try:    return json.load(f)
            except: return []
    return []

def save_log(log: list):
    with open(DATA_FILE, "w") as f:
        json.dump(log, f, indent=2)

def detect_session_start(log: list, current_kwh: float, current_ts: datetime) -> tuple:
    """Returns (is_session_start, gap_mins)"""
    if not log:
        return True, 0
    last = log[-1]
    last_ts  = datetime.fromisoformat(last["ts"])
    last_kwh = last.get("kwh", 0)
    gap_mins = (current_ts - last_ts).total_seconds() / 60

    # New session if: gap > threshold AND value jumped from standby to active
    if gap_mins > SESSION_GAP_MINS and last_kwh <= STANDBY_MAX_KWH and current_kwh >= SESSION_START_KWH:
        return True, round(gap_mins, 1)
    # Also new session if large time gap regardless
    if gap_mins > 60:
        return True, round(gap_mins, 1)
    return False, round(gap_mins, 1)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)

    # Connect to Tuya Cloud
    cloud = tinytuya.Cloud(
        apiRegion=REGION,
        apiKey=API_ID,
        apiSecret=API_SECRET,
    )

    # ── Step 1: Get live status (watts, volts, amps) ──────────────────────────
    status = cloud.getstatus(DEVICE_ID)
    if not status or "result" not in status:
        print(f"ERROR: bad status response: {status}")
        sys.exit(1)

    dps = {item["code"]: item["value"] for item in status.get("result", [])}
    print("DPS:", dps)

    cur_watts = dps.get("cur_power",   0) / 10.0
    cur_volts = dps.get("cur_voltage", 0) / 10.0
    cur_amps  = dps.get("cur_current", 0) / 1000.0
    ac_on     = dps.get("switch_1", False)

    # ── Step 2: Fetch Add Electricity events since last fetch ─────────────────
    log = load_log()

    # Determine fetch window start
    if False:
        # Start from last entry's timestamp (with 1 min overlap to avoid gaps)
        last_epoch = log[-1]["ts_epoch"]
        fetch_start_ms = (last_epoch - 60) * 1000
    else:
        # First run — fetch from midnight today
        midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        fetch_start_ms = int(midnight_ist.timestamp() * 1000)

    fetch_end_ms = int(now_utc.timestamp() * 1000)

    print(f"Fetching events from {datetime.fromtimestamp(fetch_start_ms/1000, tz=IST).strftime('%H:%M')} to {now_ist.strftime('%H:%M')} IST")

    # Fetch with pagination
    all_events = []
    row_key    = None
    page       = 1

    while True:
        kwargs = {
            "start": fetch_start_ms,
            "end":   fetch_end_ms,
            "size":  100,
            "max_fetches": 1,
        }
        if row_key:
            kwargs["start_row_key"] = row_key

        result = cloud.getdevicelog(DEVICE_ID, **kwargs)
        res    = result.get("result", {})
        logs   = res.get("logs", [])

        # Filter Add Electricity events only
        ele_events = [x for x in logs if x["code"] == "add_ele"]
        all_events.extend(ele_events)

        has_next = res.get("has_next", False)
        row_key  = res.get("current_row_key")

        print(f"  Page {page}: {len(ele_events)} add_ele events, has_next={has_next}")
        page += 1

        if not has_next or not row_key or page > 100:
            break

    print(f"Total new add_ele events: {len(all_events)}")

    # ── Step 3: Process each event ────────────────────────────────────────────
    # Sort oldest first
    all_events.sort(key=lambda x: x["event_time"])

    # Track already-logged timestamps to avoid duplicates
    logged_epochs = {e["ts_epoch"] for e in log}

    new_entries = []

    for event in all_events:
        event_ts_ms  = event["event_time"]
        event_ts_utc = datetime.fromtimestamp(event_ts_ms / 1000, tz=timezone.utc)
        event_ts_ist = event_ts_utc.astimezone(IST)
        event_epoch  = int(event_ts_utc.timestamp())

        # Skip duplicates
        if event_epoch in logged_epochs:
            continue

        raw_value = int(event["value"])
        kwh       = raw_value / 1000.0  # confirmed divisor
        slot      = get_tod_slot(event_ts_ist)
        rate      = TOD[slot]["rate"]
        cost      = round(kwh * rate, 4)

        # Session detection
        all_so_far = log + new_entries
        is_session_start, gap_mins = detect_session_start(all_so_far, kwh, event_ts_ist)

        entry = {
            "ts":              event_ts_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "ts_epoch":        event_epoch,
            "slot":            slot,
            "slot_label":      TOD[slot]["label"],
            "rate":            rate,
            "kwh":             round(kwh, 4),
            "cost":            cost,
            "watts":           cur_watts,   # live reading attached to latest batch
            "volts":           cur_volts,
            "amps":            cur_amps,
            "ac_on":           ac_on,
            "session_start":   is_session_start,
            "session_gap_mins": gap_mins,
            "source":          "tuya_event",
        }

        new_entries.append(entry)
        logged_epochs.add(event_epoch)

        flag = "🔋" if is_session_start else "  "
        print(f"  {flag} {event_ts_ist.strftime('%H:%M')} | {TOD[slot]['label']} | {kwh:.3f} kWh | ₹{cost:.4f}")

    # ── Step 4: If no new events, add a status-only heartbeat entry ───────────
    if not new_entries:
        # Just update live watts on latest entry if AC is on
        print(f"No new events. Live: {cur_watts}W, AC {'ON' if ac_on else 'OFF'}")
        # Update last entry's live reading
        if log and ac_on:
            log[-1]["watts"] = cur_watts
            log[-1]["volts"] = cur_volts
            log[-1]["amps"]  = cur_amps
    else:
        # Attach live reading to most recent new entry
        new_entries[-1]["watts"] = cur_watts
        new_entries[-1]["volts"] = cur_volts
        new_entries[-1]["amps"]  = cur_amps
        log.extend(new_entries)

    save_log(log)

    total_new_kwh  = sum(e["kwh"] for e in new_entries)
    total_new_cost = sum(e["cost"] for e in new_entries)
    print(f"\n✅ {now_ist.strftime('%Y-%m-%d %H:%M IST')} | {len(new_entries)} new events | {total_new_kwh:.3f} kWh | ₹{total_new_cost:.4f} | Live: {cur_watts}W")

if __name__ == "__main__":
    main()
