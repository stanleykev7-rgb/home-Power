"""
fetch_energy.py — AC Energy Tracker v5 (Multi-Device)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fetches Add Electricity events for all 3 AC plugs from Tuya Cloud.
Each device writes to its own log file.

Devices:
  AC1 — Main Room     → data/energy_log_ac1.json
  AC2 — Bedroom 1     → data/energy_log_ac2.json
  AC3 — Kevin's Room  → data/energy_log_ac3.json

KSEB ToD Rates (above 250 units slab):
  T1 Day   06:00–18:00  ₹6.75/unit
  T2 Peak  18:00–22:00  ₹9.375/unit
  T3 Night 22:00–06:00  ₹7.50/unit
"""

import os, json, sys
from datetime import datetime, timezone, timedelta
import tinytuya

# ── device config ─────────────────────────────────────────────────────────────
DEVICES = [
    {
        "id":       os.environ.get("TUYA_DEVICE_ID"),
        "name":     "Main Room AC",
        "key":      "ac1",
        "file":     "data/energy_log_ac1.json",
    },
    {
        "id":       os.environ.get("TUYA_DEVICE_ID_AC2"),
        "name":     "Bedroom 1 AC",
        "key":      "ac2",
        "file":     "data/energy_log_ac2.json",
    },
    {
        "id":       os.environ.get("TUYA_DEVICE_ID_AC3"),
        "name":     "Kevin's Room AC",
        "key":      "ac3",
        "file":     "data/energy_log_ac3.json",
    },
]

API_ID     = os.environ["TUYA_API_ID"]
API_SECRET = os.environ["TUYA_API_SECRET"]
REGION     = os.environ.get("TUYA_REGION", "in")

IST = timezone(timedelta(hours=5, minutes=30))

TOD = {
    "T1_day":   {"label": "Day (T1)",   "rate": 6.750},
    "T2_peak":  {"label": "Peak (T2)",  "rate": 9.375},
    "T3_night": {"label": "Night (T3)", "rate": 7.500},
}

# Thresholds
STANDBY_MAX_KWH    = 0.02
SESSION_START_KWH  = 0.05
SESSION_GAP_MINS   = 15
AC_ON_MA           = 1000
MIN_GAP_CORRECTION = 0.01
MAX_GAP_CORRECTION = 0.5

# ── helpers ───────────────────────────────────────────────────────────────────
def get_tod_slot(dt_ist):
    h = dt_ist.hour
    if 6 <= h < 18:    return "T1_day"
    elif 18 <= h < 22: return "T2_peak"
    else:              return "T3_night"

def load_log(filepath):
    os.makedirs("data", exist_ok=True)
    if os.path.exists(filepath):
        with open(filepath) as f:
            try:    return json.load(f)
            except: return []
    return []

def save_log(filepath, log):
    with open(filepath, "w") as f:
        json.dump(log, f, indent=2)

def detect_session_start(log, current_kwh, current_ts):
    if not log:
        return True, 0, None
    last     = log[-1]
    last_ts  = datetime.fromisoformat(last["ts"])
    last_kwh = last.get("kwh", 0)
    gap_mins = (current_ts - last_ts).total_seconds() / 60

    if gap_mins > SESSION_GAP_MINS and last_kwh <= STANDBY_MAX_KWH and current_kwh >= SESSION_START_KWH:
        return True, round(gap_mins, 1), int(last["ts_epoch"] * 1000)
    if gap_mins > 60:
        return True, round(gap_mins, 1), int(last["ts_epoch"] * 1000)
    return False, round(gap_mins, 1), None

def estimate_gap(cloud, device_id, last_standby_ms, session_start_ms, first_add_ele_kwh):
    try:
        result = cloud.getdevicelog(
            device_id,
            start=last_standby_ms,
            end=session_start_ms,
            size=100,
            max_fetches=1,
        )
        logs = result.get("result", {}).get("logs", [])
        if not logs:
            return 0.0, "no_gap_events"

        logs.sort(key=lambda x: x["event_time"])

        switch_on_ts_ms = None
        power_readings  = []

        for ev in logs:
            ts_ms = ev["event_time"]
            if ev.get("code") == "cur_current":
                if int(ev["value"]) > AC_ON_MA and switch_on_ts_ms is None:
                    switch_on_ts_ms = ts_ms
                    print(f"    ⚡ Switch-on at {datetime.fromtimestamp(ts_ms/1000, tz=IST).strftime('%H:%M:%S')} ({ev['value']}mA)")
            elif ev.get("code") == "cur_power" and switch_on_ts_ms and ts_ms >= switch_on_ts_ms:
                power_readings.append(int(ev["value"]) / 10.0)

        if switch_on_ts_ms is None:
            return 0.0, "no_switch_on_found"

        gap_hrs   = (session_start_ms - switch_on_ts_ms) / 3_600_000
        if gap_hrs <= 0:
            return 0.0, "gap_negative"

        avg_watts = sum(power_readings) / len(power_readings) if power_readings else None
        if not avg_watts or avg_watts < 50:
            return 0.0, "no_power_readings"

        correction = max(0.0, min((avg_watts / 1000.0) * gap_hrs - first_add_ele_kwh, MAX_GAP_CORRECTION))
        if correction < MIN_GAP_CORRECTION:
            return 0.0, "correction_too_small"

        gap_mins = round((session_start_ms - switch_on_ts_ms) / 60_000, 1)
        print(f"    ⚡ Gap: {gap_mins}min @ {round(avg_watts)}W → +{correction:.4f} kWh")
        return round(correction, 4), f"gap_{gap_mins}min_{round(avg_watts)}W"

    except Exception as e:
        print(f"    ⚠️  Gap estimation error: {e}")
        return 0.0, f"error_{str(e)[:30]}"

# ── process one device ────────────────────────────────────────────────────────
def process_device(cloud, device, now_utc, now_ist):
    device_id = device["id"]
    name      = device["name"]
    filepath  = device["file"]

    if not device_id:
        print(f"\n⏭  {name} — no device ID configured, skipping")
        return

    print(f"\n{'─'*60}")
    print(f"📱 {name} ({device_id[:8]}...)")
    print(f"{'─'*60}")

    # Live status
    status = cloud.getstatus(device_id)
    if not status or "result" not in status:
        print(f"  ERROR: bad status response: {status}")
        return

    dps       = {item["code"]: item["value"] for item in status.get("result", [])}
    cur_watts = dps.get("cur_power",   0) / 10.0
    cur_volts = dps.get("cur_voltage", 0) / 10.0
    cur_amps  = dps.get("cur_current", 0) / 1000.0
    ac_on     = dps.get("switch_1", False)
    print(f"  Live: {cur_watts}W  {cur_volts}V  {cur_amps}A  AC={'ON' if ac_on else 'OFF'}")

    # Fetch window
    log = load_log(filepath)
    if log:
        fetch_start_ms = (log[-1]["ts_epoch"] - 60) * 1000
    else:
        midnight_ist   = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        fetch_start_ms = int(midnight_ist.timestamp() * 1000)
    fetch_end_ms = int(now_utc.timestamp() * 1000)

    print(f"  Fetching from {datetime.fromtimestamp(fetch_start_ms/1000, tz=IST).strftime('%H:%M')} to {now_ist.strftime('%H:%M')} IST")

    # Paginated fetch
    all_events = []
    row_key    = None
    page       = 1
    while True:
        kwargs = {"start": fetch_start_ms, "end": fetch_end_ms, "size": 20, "max_fetches": 1}
        if row_key:
            kwargs["start_row_key"] = row_key
        result   = cloud.getdevicelog(device_id, **kwargs)
        res      = result.get("result", {})
        logs     = res.get("logs", [])
        ele      = [x for x in logs if x.get("code") == "add_ele"]
        all_events.extend(ele)
        has_next = res.get("has_next", False)
        row_key  = res.get("current_row_key")
        page    += 1
        if not has_next or not row_key or page > 20:
            break

    print(f"  Found {len(all_events)} new add_ele events")

    # Process
    all_events.sort(key=lambda x: x["event_time"])
    logged_epochs = {e["ts_epoch"] for e in log}
    new_entries   = []

    for event in all_events:
        event_ts_utc = datetime.fromtimestamp(event["event_time"] / 1000, tz=timezone.utc)
        event_ts_ist = event_ts_utc.astimezone(IST)
        event_epoch  = int(event_ts_utc.timestamp())

        if event_epoch in logged_epochs:
            continue

        kwh  = int(event["value"]) / 1000.0
        slot = get_tod_slot(event_ts_ist)
        rate = TOD[slot]["rate"]

        all_so_far = log + new_entries
        is_session_start, gap_mins, last_standby_ms = detect_session_start(all_so_far, kwh, event_ts_ist)

        gap_kwh = 0.0
        gap_estimated = False
        gap_note = ""

        if is_session_start and last_standby_ms and gap_mins >= 3:
            print(f"  🔋 Session start at {event_ts_ist.strftime('%H:%M')} gap={gap_mins}min — estimating...")
            gap_kwh, gap_note = estimate_gap(cloud, device_id, last_standby_ms, event["event_time"], kwh)
            if gap_kwh > 0:
                gap_estimated = True

        total_kwh  = round(kwh + gap_kwh, 4)
        total_cost = round(total_kwh * rate, 4)

        entry = {
            "ts":               event_ts_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "ts_epoch":         event_epoch,
            "device":           device["key"],
            "slot":             slot,
            "slot_label":       TOD[slot]["label"],
            "rate":             rate,
            "kwh":              round(kwh, 4),
            "gap_kwh":          round(gap_kwh, 4),
            "total_kwh":        total_kwh,
            "cost":             total_cost,
            "watts":            cur_watts,
            "volts":            cur_volts,
            "amps":             cur_amps,
            "ac_on":            ac_on,
            "session_start":    is_session_start,
            "session_gap_mins": gap_mins,
            "gap_estimated":    gap_estimated,
            "gap_note":         gap_note,
            "source":           "tuya_event",
        }

        new_entries.append(entry)
        logged_epochs.add(event_epoch)

        flag = "🔋" if is_session_start else "  "
        est  = f" +{gap_kwh:.4f} gap" if gap_estimated else ""
        print(f"  {flag} {event_ts_ist.strftime('%H:%M')} | {TOD[slot]['label']} | {kwh:.3f} kWh{est} | ₹{total_cost:.4f}")

    if not new_entries:
        print(f"  No new events.")
        if log and ac_on:
            log[-1]["watts"] = cur_watts
            log[-1]["volts"] = cur_volts
            log[-1]["amps"]  = cur_amps
    else:
        new_entries[-1]["watts"] = cur_watts
        new_entries[-1]["volts"] = cur_volts
        new_entries[-1]["amps"]  = cur_amps
        log.extend(new_entries)

    save_log(filepath, log)

    total_kwh  = sum(e["total_kwh"] for e in new_entries)
    total_cost = sum(e["cost"]      for e in new_entries)
    total_gap  = sum(e["gap_kwh"]   for e in new_entries)
    print(f"  ✅ {len(new_entries)} events | {total_kwh:.3f} kWh (incl. +{total_gap:.3f} est.) | ₹{total_cost:.4f}")

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)

    print(f"⚡ AC Energy Tracker — {now_ist.strftime('%Y-%m-%d %H:%M IST')}")

    cloud = tinytuya.Cloud(
        apiRegion=REGION,
        apiKey=API_ID,
        apiSecret=API_SECRET,
    )

    for device in DEVICES:
        process_device(cloud, device, now_utc, now_ist)

    print(f"\n{'─'*60}")
    print("✅ All devices processed")

if __name__ == "__main__":
    main()
