# OptiCharge – Solar & Grid-Aware EV Charging Controller

OptiCharge is a Python script to intelligently control EV charging via a supported wallbox charger and the Hyundai BlueLink API.  
It adjusts charging current in real-time based on **solar surplus**, **battery SOC**, and **time-of-day grid charging windows**.

The intention is to use all available solar, minimizing amount sent
back to grid.

## Features

- **Solar surplus charging** – match EVSE current to excess PV generation.
- **Off-peak grid charging** – automatically top-up during configured low-rate hours.
- **Target SOC control** – syncs Hyundai AC target SOC to your desired limits.
- **Vehicle re-auth** – automatic BlueLink reauthentication and retry on failure.
- **Dynamic charging current ** – real-time adjustment to soak up surplus.
- **Anti-thrash** – cooldown timers for start/stop and current changes.

## Requirements

- Python 3.10+
- Hyundai BlueLink credentials (USA API supported)
- Supported wallbox charger (with API access)
- Optional: Solar/battery data source (e.g., Tesla Powerwall API)
- `config.yaml` with your settings

Usage:

python opticharge.py -c config.yaml start

