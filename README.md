# OptiCharge – Solar & Grid-Aware EV Charging Controller

OptiCharge is a Python script to intelligently control EV charging via a supported wallbox charger and the Hyundai BlueLink API.  
It adjusts charging current in real-time based on **solar surplus**, **battery SOC**, and **time-of-day grid charging windows**.

## Features

- 🔋 **Solar surplus charging** – match EVSE current to excess PV generation.
- 🌙 **Off-peak grid charging** – automatically top-up during configured low-rate hours.
- 🎯 **Target SOC control** – syncs Hyundai AC target SOC to your desired limits.
- 💤 **Vehicle re-auth** – automatic BlueLink reauthentication and retry on failure.
- ⚡ **Dynamic amps** – real-time adjustment to soak up surplus, with ramping & anti-flap.
- 🛑 **Anti-thrash** – cooldown timers for start/stop and current changes.
- 📊 **Debug output** – prints PV, house load, battery SOC, EV SOC, headroom, and state each loop.

## Requirements

- Python 3.10+
- Hyundai BlueLink credentials (USA API supported)
- Supported wallbox charger (with API access)
- Optional: Solar/battery data source (e.g., Tesla Powerwall API)
- `config.yaml` with your settings

### Python packages
Install from `requirements.txt` (if included), or manually:

```bash
pip install requests click pyyaml

python opticharge.py -c config.yaml start

