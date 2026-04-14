# AC Energy Tracker

Track your AC power usage via Wipro Smart Plug + KSEB ToD billing — no Raspberry Pi, no local server. Just GitHub Actions + GitHub Pages.

## How It Works

```
GitHub Actions (every 15 min)
  → Tuya Cloud API (fetch cumulative kWh counter)
  → Compute delta since last reading
  → Tag with KSEB ToD slot (T1/T2/T3)
  → Append to data/energy_log.json
  → Commit & push

GitHub Pages
  → Serves index.html dashboard
  → Reads data/energy_log.json
  → Shows today's usage, monthly chart, bill estimate
```

## KSEB ToD Rates (Above 250 units slab)

| Slot | Time | Rate |
|------|------|------|
| T1 Day | 6am – 6pm | ₹6.75/unit |
| T2 Peak | 6pm – 10pm | ₹9.375/unit |
| T3 Night | 10pm – 6am | ₹7.50/unit |

---

## Setup

### Step 1 — Tuya IoT Developer Account

1. Go to [iot.tuya.com](https://iot.tuya.com) and sign up
2. Create a new Cloud project → select **India (in)** as region
3. Under **API Management → API Products**, subscribe to:
   - Smart Home Devices Management
   - Authorization
   - Smart Home Family Management
4. Link your Smart Life app: Project → Devices → Link App Account → scan QR code
5. Note down:
   - **API ID** (Access ID / Client ID)
   - **API Secret** (Access Secret)
   - **Device ID** (from Devices list)

### Step 2 — Set Up This Repo

1. Fork or clone this repo to your GitHub account
2. Go to **Settings → Secrets and variables → Actions → New repository secret**
3. Add these secrets:

   | Secret name | Value |
   |-------------|-------|
   | `TUYA_API_ID` | Your Tuya API ID |
   | `TUYA_API_SECRET` | Your Tuya API Secret |
   | `TUYA_DEVICE_ID` | Your plug's Device ID |
   | `TUYA_REGION` | `in` |

### Step 3 — Enable GitHub Pages

1. Settings → Pages → Source: **Deploy from a branch**
2. Branch: **main**, folder: **/ (root)**
3. Save — your dashboard will be at `https://yourusername.github.io/repo-name`

### Step 4 — Enable Actions

1. Go to **Actions** tab → enable workflows if prompted
2. Run the workflow manually once to verify it works
3. After that it runs automatically every 15 minutes

---

## First Run Note

The first run establishes the baseline cumulative kWh reading. The **second run** onwards will show actual delta (consumption). This is normal.

## Troubleshooting

**`add_ele` not found in DPS**
The script prints all available DPS keys on error. Check the Actions run log and update `fetch_energy.py` line:
```python
raw_ele = dps.get("add_ele", dps.get("cur_power", None))
```
Replace `add_ele` with the actual key name your plug reports (could be `add_ele`, `EnergyConsumed`, etc.)

**Workflow not running**
GitHub pauses scheduled workflows if the repo has no activity for 60 days. Re-enable from the Actions tab.

**Data looks wrong**
Check if `add_ele` units are ÷100 or ÷10 for your specific plug model. Edit `fetch_energy.py`:
```python
cumulative_kwh = raw_ele / 100.0  # change to /10.0 if values seem 10x off
```
