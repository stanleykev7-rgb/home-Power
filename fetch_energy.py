"""
fetch_energy.py
Polls Tuya Cloud for the Wipro smart plug's cumulative kWh counter (add_ele),
computes the delta since the last reading, tags it with the KSEB ToD slot,
and appends to data/energy_log.json.

KSEB ToD Rates (above 250 units slab):
  T1 Day   06:00–18:00  ₹6.75/unit  (standard −10%)
  T2 Peak  18:00–22:00  ₹9.375/unit (standard +25%)
  T3 Night 22:00–06:00  ₹7.50/unit  (standard rate)

Power cut handling:
  If add_ele counter resets (current < previous × 0.5),
  estimate consumption from instantaneous wattage × elapsed time.
"""

import os, json, sys
from datetime import datetime, timezone, timedelta
import tinytuya

# ── config ────────────────────────────────────────────────────────────────────
API_ID     = os.environ["TUYA_API_ID"]
API_SECRET = os.environ["TUYA_API_SECRET"]
DEVICE_ID  = os.environ["TUYA_DEVICE_ID"]
REGION     = os.environ.get("TUYA_REGION", "in")
DATA_FILE  = "data/energy_log.json"

# KSEB ToD rates
TOD = {
    "T1_day":   {"label": "Day (T1)",   "rate": 6.750},
    "T2_peak":  {"label": "Peak (T2)",  "rate": 9.375},
    "T3_night": {"label": "Night (T3)", "rate": 7.500},
}

IST = timezone(timedelta(hours=5, minutes=30))
POLL_INTERVAL_MINS = 15  # expected interval between polls

# ── helpers ───────────────────────────────────────────────────────────────────
def get_tod_slot(dt_ist: datetime) -> str:
    h = dt_ist.hour
    if 6 <= h < 18:
        return "T1_day"
    elif 18 <= h < 22:
        return "T2_peak"
    else:
        return "T3_night"

def load_log() -> list:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_log(log: list):
    with open(DATA_FILE, "w") as f:
        json.dump(log, f, indent=2)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)
    slot    = get_tod_slot(now_ist)
    rate    = TOD[slot]["rate"]

    # Connect to Tuya Cloud
    cloud = tinytuya.Cloud(
        apiRegion=REGION,
        apiKey=API_ID,
        apiSecret=API_SECRET,
    )

    # Fetch device status
    status = cloud.getstatus(DEVICE_ID)
    if not status or "result" not in status:
        print(f"ERROR: bad response from Tuya Cloud: {status}")
        sys.exit(1)

    dps = {item["code"]: item["value"] for item in status.get("result", [])}
    print("DPS:", dps)

    raw_ele = dps.get("add_ele", None)
    if raw_ele is None:
        print("WARNING: add_ele not found in DPS. Available keys:", list(dps.keys()))
        sys.exit(1)

    cumulative_kwh  = raw_ele / 100.0
    cur_power_raw   = dps.get("cur_power", 0)
    cur_watts       = cur_power_raw / 10.0
    cur_voltage_raw = dps.get("cur_voltage", 0)
    cur_volts       = cur_voltage_raw / 10.0
    cur_current_raw = dps.get("cur_current", 0)
    cur_amps        = cur_current_raw / 1000.0

    # Load log
    log = load_log()

    # ── power cut detection ───────────────────────────────────────────────────
    power_cut    = False
    delta_kwh    = 0.0
    event_note   = ""

    if log:
        last         = log[-1]
        prev_cumul   = last["cumulative_kwh"]
        prev_ts      = datetime.fromisoformat(last["ts"])
        elapsed_hrs  = (now_ist - prev_ts).total_seconds() / 3600

        if cumulative_kwh < prev_cumul * 0.5 and prev_cumul > 0.1:
            # Counter reset detected — power cut or plug restart
            power_cut  = True
            event_note = "power_cut_estimated"
            # Estimate from instantaneous wattage × elapsed time
            # Use average of last known watts and current watts
            last_watts    = last.get("watts", 0) or 0
            avg_watts     = (last_watts + cur_watts) / 2
            delta_kwh     = max(0.0, (avg_watts / 1000.0) * elapsed_hrs)
            print(f"⚠️  Power cut detected! Counter reset {prev_cumul:.3f} → {cumulative_kwh:.3f} kWh")
            print(f"   Estimating {delta_kwh:.4f} kWh from avg {avg_watts:.1f}W over {elapsed_hrs:.2f}hrs")
        else:
            delta_kwh = max(0.0, cumulative_kwh - prev_cumul)
    else:
        # First run — no delta
        delta_kwh  = 0.0
        event_note = "first_run"

    cost = round(delta_kwh * rate, 4)

    entry = {
        "ts":             now_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
        "ts_epoch":       int(now_utc.timestamp()),
        "slot":           slot,
        "slot_label":     TOD[slot]["label"],
        "rate":           rate,
        "delta_kwh":      round(delta_kwh, 4),
        "cost":           cost,
        "cumulative_kwh": round(cumulative_kwh, 3),
        "watts":          cur_watts,
        "volts":          cur_volts,
        "amps":           cur_amps,
        "power_cut":      power_cut,
        "note":           event_note,
    }

    log.append(entry)
    save_log(log)

    flag = "⚠️ " if power_cut else "✅"
    print(f"{flag} {now_ist.strftime('%Y-%m-%d %H:%M IST')} | {TOD[slot]['label']} @ ₹{rate} | "
          f"Δ {delta_kwh:.4f} kWh | Cost ₹{cost:.4f} | {cur_watts}W"
          + (" [ESTIMATED]" if power_cut else ""))

if __name__ == "__main__":
    main()
