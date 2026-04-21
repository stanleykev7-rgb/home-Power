"""
fetch_energy.py — AC Energy Tracker v4
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fetches Add Electricity events from Tuya Cloud device log since last fetch.
Each event is an incremental kWh value for a ~10-min reporting window.
Divisor: 1000 (confirmed from Device Logs e.g. raw 127 = 0.13 kWh)

Gap Estimation:
  On session start, fetches cur_current events in the gap window to find
  the exact switch-on moment (current > AC_ON_MA threshold). Estimates
  consumption from switch-on to first add_ele event and adds gap_kwh to
  the session start entry. Shown as estimated in dashboard.

KSEB ToD Rates (above 250 units slab):
  T1 Day   06:00–18:00  ₹6.75/unit
  T2 Peak  18:00–22:00  ₹9.375/unit
  T3 Night 22:00–06:00  ₹7.50/unit
"""

import os, json, sys
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
STANDBY_MAX_KWH   = 0.02    # below this = AC off / standby
SESSION_START_KWH = 0.05    # above this after standby = new session
SESSION_GAP_MINS  = 15      # gap > this between events = new session
AC_ON_MA          = 1000    # cur_current > this = AC definitely running (mA)
MIN_GAP_CORRECTION = 0.01   # ignore corrections smaller than this (kWh)
MAX_GAP_CORRECTION = 0.5    # cap correction at this (kWh)

# ── helpers ───────────────────────────────────────────────────────────────────
def get_tod_slot(dt_ist: datetime) -> str:
    h = dt_ist.hour
    if 6 <= h < 18:    return "T1_day"
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
    """Returns (is_session_start, gap_mins, last_standby_ts_ms)"""
    if not log:
        return True, 0, None
    last     = log[-1]
    last_ts  = datetime.fromisoformat(last["ts"])
    last_kwh = last.get("kwh", 0)
    gap_mins = (current_ts - last_ts).total_seconds() / 60

    # New session: gap > threshold AND jumped from standby to active
    if gap_mins > SESSION_GAP_MINS and last_kwh <= STANDBY_MAX_KWH and current_kwh >= SESSION_START_KWH:
        last_standby_ms = int(last["ts_epoch"] * 1000)
        return True, round(gap_mins, 1), last_standby_ms

    # Also new session if very large time gap regardless
    if gap_mins > 60:
        last_standby_ms = int(last["ts_epoch"] * 1000)
        return True, round(gap_mins, 1), last_standby_ms

    return False, round(gap_mins, 1), None

def estimate_gap(cloud, last_standby_ms: int, session_start_ms: int,
                 first_add_ele_kwh: float) -> tuple:
    """
    Fetch cur_current events in the gap window to find exact switch-on time.
    Returns (gap_kwh, gap_note) where gap_kwh is the estimated missing consumption.
    """
    try:
        # Fetch ALL event types in the gap window (not just add_ele)
        result = cloud.getdevicelog(
            DEVICE_ID,
            start=last_standby_ms,
            end=session_start_ms,
            size=100,
            max_fetches=1,
        )
        logs = result.get("result", {}).get("logs", [])

        if not logs:
            return 0.0, "no_gap_events"

        # Sort oldest first
        logs.sort(key=lambda x: x["event_time"])

        # Find first cur_current event where current > AC_ON_MA
        switch_on_ts_ms = None
        switch_on_watts = None

        # Also collect all cur_power readings in gap for averaging
        power_readings = []

        for ev in logs:
            ts_ms = ev["event_time"]
            if ev["code"] == "cur_current":
                current_ma = int(ev["value"])
                if current_ma > AC_ON_MA and switch_on_ts_ms is None:
                    switch_on_ts_ms = ts_ms
                    print(f"  ⚡ Switch-on detected at {datetime.fromtimestamp(ts_ms/1000, tz=IST).strftime('%H:%M:%S')} ({current_ma}mA)")

            elif ev["code"] == "cur_power" and switch_on_ts_ms and ts_ms >= switch_on_ts_ms:
                power_readings.append(int(ev["value"]) / 10.0)  # convert to W

        if switch_on_ts_ms is None:
            return 0.0, "no_switch_on_found"

        # Calculate gap from switch-on to first add_ele event
        gap_secs = (session_start_ms - switch_on_ts_ms) / 1000
        gap_hrs  = gap_secs / 3600

        if gap_hrs <= 0:
            return 0.0, "gap_negative"

        # Average watts during gap
        avg_watts = sum(power_readings) / len(power_readings) if power_readings else None

        if avg_watts is None or avg_watts < 50:
            # Fall back — no power readings, can't estimate accurately
            return 0.0, "no_power_readings"

        # Total estimated consumption during gap
        estimated_total_kwh = (avg_watts / 1000.0) * gap_hrs

        # Subtract what first add_ele event already captured
        correction = estimated_total_kwh - first_add_ele_kwh

        # Guard: floor at 0, cap at max
        correction = max(0.0, min(correction, MAX_GAP_CORRECTION))

        if correction < MIN_GAP_CORRECTION:
            return 0.0, "correction_too_small"

        gap_mins_actual = round(gap_secs / 60, 1)
        note = f"gap_estimated_{gap_mins_actual}min_{round(avg_watts)}W"
        print(f"  ⚡ Gap: {gap_mins_actual}min @ avg {round(avg_watts)}W → +{correction:.4f} kWh correction")

        return round(correction, 4), note

    except Exception as e:
        print(f"  ⚠️  Gap estimation error: {e}")
        return 0.0, f"error_{str(e)[:30]}"

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

    # ── Step 1: Get live status ───────────────────────────────────────────────
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

    if log:
        last_epoch     = log[-1]["ts_epoch"]
        fetch_start_ms = (last_epoch - 60) * 1000
    else:
        midnight_ist   = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        fetch_start_ms = int(midnight_ist.timestamp() * 1000)

    fetch_end_ms = int(now_utc.timestamp() * 1000)

    print(f"Fetching events from {datetime.fromtimestamp(fetch_start_ms/1000, tz=IST).strftime('%H:%M')} to {now_ist.strftime('%H:%M')} IST")

    all_events = []
    row_key    = None
    page       = 1

    while True:
        kwargs = {
            "start":       fetch_start_ms,
            "end":         fetch_end_ms,
            "size":        20,
            "max_fetches": 1,
        }
        if row_key:
            kwargs["start_row_key"] = row_key

        result   = cloud.getdevicelog(DEVICE_ID, **kwargs)
        res      = result.get("result", {})
        logs     = res.get("logs", [])
        ele_events = [x for x in logs if x["code"] == "add_ele"]
        all_events.extend(ele_events)

        has_next = res.get("has_next", False)
        row_key  = res.get("current_row_key")

        print(f"  Page {page}: {len(ele_events)} add_ele events, has_next={has_next}")
        page += 1

        if not has_next or not row_key or page > 20:
            break

    print(f"Total new add_ele events: {len(all_events)}")

    # ── Step 3: Process each event ────────────────────────────────────────────
    all_events.sort(key=lambda x: x["event_time"])
    logged_epochs = {e["ts_epoch"] for e in log}
    new_entries   = []

    for event in all_events:
        event_ts_ms  = event["event_time"]
        event_ts_utc = datetime.fromtimestamp(event_ts_ms / 1000, tz=timezone.utc)
        event_ts_ist = event_ts_utc.astimezone(IST)
        event_epoch  = int(event_ts_utc.timestamp())

        if event_epoch in logged_epochs:
            continue

        raw_value = int(event["value"])
        kwh       = raw_value / 1000.0
        slot      = get_tod_slot(event_ts_ist)
        rate      = TOD[slot]["rate"]

        # Session detection
        all_so_far = log + new_entries
        is_session_start, gap_mins, last_standby_ms = detect_session_start(
            all_so_far, kwh, event_ts_ist
        )

        # Gap estimation on session start
        gap_kwh       = 0.0
        gap_estimated = False
        gap_note      = ""

        if is_session_start and last_standby_ms is not None and gap_mins >= 3:
            print(f"  🔋 Session start detected, gap={gap_mins}min — estimating gap consumption...")
            gap_kwh, gap_note = estimate_gap(
                cloud,
                last_standby_ms,
                event_ts_ms,
                kwh,
            )
            if gap_kwh > 0:
                gap_estimated = True

        # Total kWh including gap estimate
        total_kwh  = round(kwh + gap_kwh, 4)
        total_cost = round(total_kwh * rate, 4)

        entry = {
            "ts":              event_ts_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "ts_epoch":        event_epoch,
            "slot":            slot,
            "slot_label":      TOD[slot]["label"],
            "rate":            rate,
            "kwh":             round(kwh, 4),       # raw event kWh
            "gap_kwh":         round(gap_kwh, 4),   # estimated gap addition
            "total_kwh":       total_kwh,            # kwh + gap_kwh
            "cost":            total_cost,
            "watts":           cur_watts,
            "volts":           cur_volts,
            "amps":            cur_amps,
            "ac_on":           ac_on,
            "session_start":   is_session_start,
            "session_gap_mins": gap_mins,
            "gap_estimated":   gap_estimated,
            "gap_note":        gap_note,
            "source":          "tuya_event",
        }

        new_entries.append(entry)
        logged_epochs.add(event_epoch)

        flag = "🔋" if is_session_start else "  "
        est  = f" +{gap_kwh:.4f} gap" if gap_estimated else ""
        print(f"  {flag} {event_ts_ist.strftime('%H:%M')} | {TOD[slot]['label']} | {kwh:.3f} kWh{est} | ₹{total_cost:.4f}")

    # ── Step 4: Handle no new events ─────────────────────────────────────────
    if not new_entries:
        print(f"No new events. Live: {cur_watts}W, AC {'ON' if ac_on else 'OFF'}")
        if log and ac_on:
            log[-1]["watts"] = cur_watts
            log[-1]["volts"] = cur_volts
            log[-1]["amps"]  = cur_amps
    else:
        new_entries[-1]["watts"] = cur_watts
        new_entries[-1]["volts"] = cur_volts
        new_entries[-1]["amps"]  = cur_amps
        log.extend(new_entries)

    save_log(log)

    total_new_kwh  = sum(e["total_kwh"] for e in new_entries)
    total_new_cost = sum(e["cost"]      for e in new_entries)
    total_gap_kwh  = sum(e["gap_kwh"]   for e in new_entries)
    print(f"\n✅ {now_ist.strftime('%Y-%m-%d %H:%M IST')} | {len(new_entries)} new events | "
          f"{total_new_kwh:.3f} kWh (incl. +{total_gap_kwh:.3f} estimated) | ₹{total_new_cost:.4f} | Live: {cur_watts}W")

if __name__ == "__main__":
    main()
