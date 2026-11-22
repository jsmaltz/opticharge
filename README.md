# OptiCharge – Solar & Grid-Aware EV Charging Controller

OptiCharge is a Python script to intelligently control EV charging via a supported wallbox charger and the Hyundai BlueLink API.  
It adjusts charging current in real-time based on **solar surplus**, **battery SOC**, and **time-of-day grid charging windows**.

It can also integrate with an Ecobee thermostat through Home Assistant to
select gas auxiliary heat when delivered gas heat is cheaper, Powerwall energy
is limited, the EV is below target, solar is scarce, and heat is being called.
The thermostat controller defaults to dry-run mode and applies a minimum dwell
time before any mode change.

The intention is to use all available solar, minimizing amount sent
back to grid.

## Features

- **Solar surplus charging** – match EVSE current to excess PV generation.
- **Off-peak grid charging** – automatically top-up during configured low-rate hours.
- **Target SOC control** – syncs Hyundai AC target SOC to your desired limits.
- **Vehicle re-auth** – automatic BlueLink reauthentication and retry on failure.
- **Dynamic charging current ** – real-time adjustment to soak up surplus.
- **Anti-thrash** – cooldown timers for start/stop and current changes.
- **Wallbox recovery** – forced fresh login after rejected tokens, bounded
  transient retries, request timeouts, and a circuit breaker.
- **Solar forecasting** – optional Open-Meteo forecasts adjust the overnight EV
  target and Powerwall safety floor.

## Solar forecast and overnight Powerwall floor

OptiCharge can use Open-Meteo's hourly global tilted irradiance forecast to
classify tomorrow as a poor, average, or good solar day. The classification
selects an overnight EV target and Powerwall floor. Configure the site latitude,
longitude, array capacity, panel tilt, and panel azimuth in `config.yaml`, then
set `solar_forecast_enabled: true`. Open-Meteo defines south as 0 degrees, east
as -90, and west as +90.

The fixed `grid_powerwall_floor_soc` remains active when forecasting is disabled
or unavailable. A recent successful forecast is cached for up to
`solar_forecast_stale_hours`; after that, OptiCharge falls back to the default
EV target and fixed floor. The initial template never lets a favorable forecast
lower the Powerwall below 50%.

## Requirements

- Python 3.10+
- Hyundai BlueLink credentials (USA API supported)
- Supported wallbox charger (with API access)
- Optional: Solar/battery data source (e.g., Tesla Powerwall API)
- `config.yaml` with your settings

Usage:

python opticharge.py -c config.yaml start

## Tesla local Gateway setup

Local Gateway access is the preferred data source when OptiCharge runs on the
same network as a Powerwall. It avoids Tesla cloud availability and API
entitlement issues while providing the required solar power, house load, and
battery state-of-charge readings.

Set these values in the private `config.yaml`:

    tesla_enabled: true
    tesla_data_source: "local"
    tesla_gateway_url: "https://your-gateway-address"
    tesla_gateway_username: "customer"
    tesla_gateway_password: "your-custom-local-password"
    tesla_gateway_verify_tls: false

Test the readings without starting charger control:

    python tesla_local.py -c config.yaml

Recent Gateway firmware rejects the default five-character customer password.
To replace it, connect directly to the Gateway's `TEG-xxx` Wi-Fi network,
toggle the Powerwall switch when prompted, and run:

    python tesla_local_password.py -c config.yaml --old-password XXXXX

The password-change utility reads the new password from
`tesla_gateway_password`; it does not print it. The Gateway normally uses a
self-signed HTTPS certificate, so TLS verification defaults to disabled for
local access.

## Tesla Fleet API setup

The legacy Tesla Owner API is no longer supported. As an alternative to local
Gateway access, OptiCharge can use Tesla's official Fleet API by setting
`tesla_data_source: "fleet"`. It requests only `openid`, `offline_access`, and
`energy_device_data`.

Tesla integration can be bypassed with `tesla_enabled: false`. This makes no
Tesla API requests and does not read the Tesla token cache. Solar-surplus
charging is unavailable in this mode, while grid-window charging, EV target
SOC handling, Wallbox control, and BlueLink handling continue normally.

1. Create and register an application at https://developer.tesla.com/. Tesla's
   onboarding requires a public key hosted on the application's HTTPS domain
   and registration in each API region the app will use.
2. Enable the `energy_device_data` scope and configure an HTTPS redirect URI.
3. Copy `config.yaml_template` to `config.yaml` and set `tesla_client_id`,
   `tesla_client_secret`, and `tesla_redirect_uri`.
4. Authorize the Tesla account and save its rotating tokens:

       python tesla_auth.py -c config.yaml authorize

5. Test Powerwall/solar readings without starting charger control:

       python tesla_auth.py -c config.yaml test

For a credential-safe diagnostic of the token, account region, and `/products`
response (including Tesla's `x-txid`), run:

       python tesla_diagnose.py -c config.yaml

Tokens are stored in `.tesla-tokens.json`, which is ignored by Git. Fleet API
refresh tokens rotate; deploy this file with `config.yaml` and keep both files
private. The North America API URL is the default. Accounts in Europe, the
Middle East, or Africa must use the corresponding regional URL from Tesla's
Fleet API documentation.

## Home Assistant / Ecobee

Create a dedicated non-administrator Home Assistant user and a long-lived
access token for it. Store the token by itself in `.home-assistant-token` beside
`config.yaml`, with file mode `0600`; do not put the token in Git or directly in
the YAML file. Configure the Home Assistant URL and the Ecobee climate and
auxiliary-heat entity IDs using the `ecobee_*` settings in
`config.yaml_template`.

Keep `ecobee_dry_run: true` until the peak/off-peak electricity rates, gas rate,
heat-pump COP, and furnace efficiency have been verified in the logs. OptiCharge
will not make a thermostat decision when required rate values are missing.

The template includes PG&E EV2-A peak, partial-peak, and off-peak energy rates
effective June 1, 2026, plus the November 1, 2026 G-1 baseline gas rate. These
tariffs are seasonal and should be checked after PG&E rate revisions. The
Carrier 58MXA furnace efficiency setting is 92% AFUE; the heat-pump COP remains
an estimate until its model and performance data are configured.

Fedora:

service opticharge restart

Show log:

journalctl -u opticharge.service -f

ReadyNAS intranet log viewer:

http://10.0.0.50:8088/
