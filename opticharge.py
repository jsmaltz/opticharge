import time
import yaml
import logging
import click
import requests
from wallbox import Wallbox
from bluelink import BlueLink

import logging
import http.client as http_client
import teslapy
import requests
import certifi
import time
import random

from hyundai_kia_connect_api import VehicleManager, const
from datetime import datetime, timedelta
from enum import Enum, auto

import json
# Turn on low-level HTTP debug logging
#http_client.HTTPConnection.debuglevel = 1
#logging.basicConfig(level=logging.DEBUG)
#logging.getLogger("urllib3").setLevel(logging.DEBUG)

#class TransientAPIError(Exception):
#    pass


# ----- Sensors -----

class TeslaSensor:
    def __init__(
        self,
        refresh_token: str,
        client_id: str = "ownerapi",
        audience: str = "https://owner-api.teslamotors.com",
        token_url: str = "https://auth.tesla.com/oauth2/v3/token",
        owner_api_url: str = "https://owner-api.teslamotors.com"
    ):
        self.refresh_token  = refresh_token
        self.client_id      = client_id
        self.audience       = audience
        self.token_url      = token_url
        self.owner_api_url  = owner_api_url
        self.access_token   = None
        self.session        = requests.Session()
        # force use of certifi's CA bundle
        self.session.verify = certifi.where()

        # cache these once discovered
        self._site_id       = None
        self._products_json = None

    def _refresh_access_token(self):
        payload = {
            "grant_type":    "refresh_token",
            "client_id":     self.client_id,
            "refresh_token": self.refresh_token,
            "audience":      self.audience
        }
        r = self.session.post(self.token_url, data=payload)
        r.raise_for_status()
        j = r.json()
        self.access_token  = j["access_token"]
        # Tesla may rotate the refresh token
        self.refresh_token = j.get("refresh_token", self.refresh_token)

    def _get_products(self):
        if self._products_json is None:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            url = f"{self.owner_api_url}/api/1/products"
            r = self.session.get(url, headers=headers)
            r.raise_for_status()
            self._products_json = r.json()["response"]
        return self._products_json

    def _get_site_id(self):
        if self._site_id is None:
            resp = self._get_products()
            # legacy shape?
            if isinstance(resp, dict) and "energy_sites" in resp:
                self._site_id = resp["energy_sites"][0]["id"]
            # new shape?
            elif isinstance(resp, list) and "energy_site_id" in resp[0]:
                self._site_id = resp[0]["energy_site_id"]
            else:
                raise RuntimeError(f"Could not find energy_site_id in {resp!r}")
        return self._site_id

    def get_house_power(self) -> dict:
        # 1) ensure we have a valid access token
        if not self.access_token:
            self._refresh_access_token()

        headers = {"Authorization": f"Bearer {self.access_token}"}
        site_id = self._get_site_id()

        # 2) live_status → solar & load (and maybe battery_level)
        live_url = f"{self.owner_api_url}/api/1/energy_sites/{site_id}/live_status"
        r = self.session.get(live_url, headers=headers)
        if r.status_code == 401:
            # expired token? retry once
            self._refresh_access_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            r = self.session.get(live_url, headers=headers)
        r.raise_for_status()
        live = r.json()["response"]
        
        if isinstance(live, str):
            try:
                live = json.loads(live)
                print("NOTE: Tesla live payload was JSON string; parsed to dict.")
            except Exception:
                live = {}
        if not isinstance(live, dict):
            live = {}
            
        #click.echo(live)
        #breakpoint()

        def _num(x, default=0.0):
            try:
                return float(x) if x is not None else default
            except Exception:
                return default

        # Then use:
        solar = _num(live.get("solar_power"), 0.0)
        house = _num(live.get("house_load"), 0.0)
        batt  = _num(live.get("battery_soc"), 0.0)

        # 3) battery_level fall-back if needed
        if batt is None:
            # try site_info
            info_url = f"{self.owner_api_url}/api/1/energy_sites/{site_id}/site_info"
            r2 = self.session.get(info_url, headers=headers)
            r2.raise_for_status()
            info = r2.json()["response"]
            batt = (
                info.get("battery", {}).get("percent")
                or info.get("battery_level")
                or info.get("percentage_charged")
            )
        return {
            "solar_power": solar,
            "house_load" : house,
            "battery_soc": batt,
        }


class BlueLinkSensor:
    def __init__(
        self,
        cfg,
        username: str,
        password: str,
        pin: str,
        region_cfg: int | str,
        brand_cfg: int | str,
        vin: str):

        self._bluelink_fail_count = 0
        self._bluelink_cooloff_until = 0  # epoch seconds
        self._refresh_fail_count = 0
        self.cfg=cfg
        # normalize region and brand 
        regions = const.REGIONS
        if isinstance(region_cfg, int) and region_cfg in regions:
            region_id = region_cfg
        elif isinstance(region_cfg, str) and region_cfg in regions.values():
            region_id = next(k for k, v in regions.items() if v == region_cfg)
        else:
            raise ValueError(f"Unknown region '{region_cfg}'.")

        brands = const.BRANDS
        if isinstance(brand_cfg, int) and brand_cfg in brands:
            brand_id = brand_cfg
        elif isinstance(brand_cfg, str) and brand_cfg in brands.values():
            brand_id = next(k for k, v in brands.items() if v == brand_cfg)
        else:
            raise ValueError(f"Unknown brand '{brand_cfg}'.")
        self.username = username
        self.password = password
        self.region_id = region_id
        self.brand_id = brand_id
        self.pin = pin
        self.vin = vin

        self.authenticate()

    def _safe_vehicle_data(self, vehicle) -> dict:
        """
        Return vehicle.data as a dict. If it's a JSON string or anything else,
        parse or fall back to {} so .get(...) is always safe.
        """
        d = getattr(vehicle, "data", None)
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                d = {}
        if not isinstance(d, dict):
            d = {}
        return d

    def authenticate(self):
        self.vm = VehicleManager(
            region=self.region_id,
            brand=self.brand_id,
            username=self.username,
            password=self.password,
            pin=self.pin
        )
        self.vm.check_and_refresh_token()
        
        # Map your real VIN to the internal ID, trying both .VIN and .vin
        self.vehicle_id = None
        for internal_id, vehicle in self.vm.vehicles.items():
            vehicle_vin = getattr(vehicle, 'vin',
                           getattr(vehicle, 'VIN', None))
            if vehicle_vin == self.vin:
                self.vehicle_id = internal_id
                break
        if self.vehicle_id is None:
            available = [getattr(v, 'vin', getattr(v, 'VIN', None))
                         for v in self.vm.vehicles.values()]
            raise RuntimeError(f"VIN '{self.vin}' not found; available VINs: {available}")

    def _call_with_reauth(self, func: callable):
        """
        Run func(), with 401/429 handling, transient payload retries,
        and a circuit breaker that forces a full client re-init.
        """
        for attempt in range(3):
            try:
                return func()
            except requests.exceptions.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code == 401:
                    click.echo("BlueLink 401 ? reauth?")
                    self.authenticate()
                    continue
                if code == 429:
                    wait = 6 + attempt * 4
                    click.echo(f"BlueLink 429 ? backoff {wait}s?")
                    time.sleep(wait)
                    continue
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
                # SDK saw an unexpected/missing field; treat as transient
                self._bluelink_fail_count += 1
                click.echo(f"BlueLink payload error ({type(e).__name__}): {e} "
                      f"[fail#{self._bluelink_fail_count}]")

                # short cooloff and try a normal reauth on first two hits
                if self._bluelink_fail_count < self.cfg['bluelink_refresh_fail_count']:
                    time.sleep(2 + attempt)
                    try:
                        self.authenticate()
                    except Exception:
                        pass
                    continue

                # circuit breaker: full client re-init on 3rd consecutive failure
                self._full_reinit_bluelink()
                # if we?re cooling off, don?t hammer
                if time.time() < getattr(self, "_bluelink_cooloff_until", 0):
                    time.sleep(max(0, self._bluelink_cooloff_until - time.time()))
                # after re-init, one more try this loop:
                try:
                    return func()
                finally:
                    # reset fail counter after a re-init path
                    self._bluelink_fail_count = 0
            except Exception as e:
                click.echo(f"BlueLink unexpected error: {e!r}. Retrying once?")
                time.sleep(2)
                continue

        # Final attempt (let exceptions bubble if it still fails)
        return func()

    def _full_reinit_bluelink(self):
        """Hard reset BlueLink by rebuilding VehicleManager exactly like authenticate()."""
        click.echo("BlueLink: performing FULL client re-init?")
        from hyundai_kia_connect_api import VehicleManager

        # Recreate VehicleManager exactly as in authenticate()
        vm = VehicleManager(
            region=self.region_id,
            brand=self.brand_id,
            username=self.username,
            password=self.password,
            pin=self.pin
        )
        vm.check_and_refresh_token()

        # Map VIN to internal id (support both .vin and .VIN)
        vehicle_id = None
        for internal_id, vehicle in vm.vehicles.items():
            vehicle_vin = getattr(vehicle, "vin", getattr(vehicle, "VIN", None))
            if vehicle_vin == self.vin:
                vehicle_id = internal_id
                break
        if vehicle_id is None:
            available = [getattr(v, "vin", getattr(v, "VIN", None)) for v in vm.vehicles.values()]
            raise RuntimeError(f"VIN '{self.vin}' not found after re-init; available VINs: {available}")

        # Atomically swap in the fresh manager + id
        self.vm = vm
        self.vehicle_id = vehicle_id

        # Clear breaker / cooldown state
        self._bluelink_fail_count = 0
        self._bluelink_cooloff_until = 0
        setattr(self, "_skip_next_refresh_until", 0)

        click.echo("BlueLink: FULL re-init complete.")

    def _call_with_reauth(self, func: callable):
        """
        Run func(), with 401/429 handling, transient payload retries,
        and a circuit breaker that forces a full client re-init.
        """
        for attempt in range(3):
            try:
                return func()
            except requests.exceptions.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code == 401:
                    click.echo("BlueLink 401 ? reauth?")
                    self.authenticate()
                    continue
                if code == 429:
                    wait = 6 + attempt * 4
                    click.echo(f"BlueLink 429 ? backoff {wait}s?")
                    time.sleep(wait)
                    continue
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
                self._bluelink_fail_count += 1
                click.echo(f"BlueLink payload error ({type(e).__name__}): {e} [fail#{self._bluelink_fail_count}]")

                if self._bluelink_fail_count >= 1:
                    click.echo("??  BlueLink circuit-breaker TRIPPED ? full re-init")
                    self._full_reinit_bluelink()
                    self._bluelink_fail_count = 0
                    continue

                time.sleep(2)
                try:
                    self.authenticate()
                except Exception:
                    pass
                continue

                # circuit breaker: full client re-init on 3rd consecutive failure
                click.echo("??  BlueLink circuit-breaker TRIPPED ? full re-init")
                self._full_reinit_bluelink()
                # if we're cooling off, don't hammer
                if time.time() < getattr(self, "_bluelink_cooloff_until", 0):
                    time.sleep(max(0, self._bluelink_cooloff_until - time.time()))
                # after re-init, one more try this loop:
                try:
                    return func()
                finally:
                    # reset fail counter after a re-init path
                    self._bluelink_fail_count = 0
            except Exception as e:
                click.echo(f"BlueLink unexpected error: {e!r}. Retrying once?")
                time.sleep(2)
                continue

        # Final attempt (let exceptions bubble if it still fails)
        return func()
            
    def start_charge(self) -> dict:
        """Tell the Hyundai Ioniq to begin charging immediately."""
        def _do():
            token_obj = self.vm.api.login(self.username, self.password)
            vehicle   = self.vm.get_vehicle(self.vehicle_id)
            return self.vm.api.start_charge(token_obj, vehicle)
        return self._call_with_reauth(_do)


    def stop_charge(self) -> dict:
        """Tell the Hyundai Ioniq to stop charging immediately."""
        def _do():
            token_obj = self.vm.api.login(self.username, self.password)
            vehicle   = self.vm.get_vehicle(self.vehicle_id)
            return self.vm.api.stop_charge(token_obj, vehicle)
        return self._call_with_reauth(_do)

    def get_vehicle_status(self) -> dict:
        def _do():
            now = time.time()
            can_refresh = now >= getattr(self, "_skip_next_refresh_until", 0)
            if can_refresh:
                try:
                    self.vm.update_vehicle_with_cached_state(self.vehicle_id)
                    self._refresh_fail_count = 0
                except Exception as e:
                    self._refresh_fail_count += 1
                    click.echo(f"refresh failed: {e}. fail#{self._refresh_fail_count}. Cooling off 30s.")
                    self._skip_next_refresh_until = now + 30
                    if self._refresh_fail_count >= self.cfg['bluelink_refresh_fail_count']:
                        click.echo("BlueLink refresh failures reached threshold ? full re-init")
                        self._full_reinit_bluelink()
                        self._refresh_fail_count = 0
                        self.vm.update_vehicle_with_cached_state(self.vehicle_id)

            car = self.vm.get_vehicle(self.vehicle_id)

            target_ac = getattr(car, "ev_charge_limits_ac", None)
            target_dc = getattr(car, "ev_charge_limits_dc", None)
            charging_power_kW = getattr(car, "ev_charging_power", None)

            #per-plug target list (robust against vehicle.data being a JSON string)
            vd = self._safe_vehicle_data(car)
            rci = vd.get("vehicleStatus", {}).get("evStatus", {}).get("reservChargeInfos", {})
            for item in rci.get("targetSOClist", []) or []:
                if item.get("plugType") == 0:
                    target_ac = item.get("targetSOClevel", target_ac)
                elif item.get("plugType") == 1:
                    target_dc = item.get("targetSOClevel", target_dc)
            return {
                "plugged_in": bool(getattr(car, "ev_battery_is_plugged_in", False)),
                "charging":   bool(getattr(car, "ev_battery_is_charging", False)),
                "soc":        getattr(car, "ev_battery_percentage", None),
                "target_ac":  target_ac,
                "target_dc":  target_dc,
                "charging_power_kW": charging_power_kW
            }
        return self._call_with_reauth(_do)

    def get_ac_target_soc(self) -> int | None:
        """
        Return the AC charging target SOC percentage (or None if unavailable).
        """
        def _do():
            # refresh cached state, then read
            self.vm.update_vehicle_with_cached_state(self.vehicle_id)
            vehicle = self.vm.get_vehicle(self.vehicle_id)

            vd = self._safe_vehicle_data(vehicle)
            try:
                target_soc_list = vd["vehicleStatus"]["evStatus"]["reservChargeInfos"]["targetSOClist"]
                return next(item["targetSOClevel"] for item in target_soc_list if item.get("plugType") == 0)
            except Exception:
                # Fallback to top-level shortcut if the detailed list isn't present
                return getattr(vehicle, "ev_charge_limits_ac", None)

        return self._call_with_reauth(_do)

    def set_ac_target_soc(self, soc_level: int) -> dict:
        """
        Set the AC charging target SOC percentage, preserving the current DC limit.
        """
        if not (50 <= soc_level <= 100):
            raise ValueError("SOC level must be between 50 and 100")

        def _do():
            token_obj = self.vm.api.login(self.username, self.password)
            vehicle = self.vm.get_vehicle(self.vehicle_id)

            # Read current DC target from vehicle data (robust to JSON string)
            vd = self._safe_vehicle_data(vehicle)
            target_soc_list = vd["vehicleStatus"]["evStatus"]["reservChargeInfos"]["targetSOClist"]
            dc_limit = next(item["targetSOClevel"] for item in target_soc_list if item.get("plugType") == 1)

            # set_charge_limits(api_token, vehicle_obj, ac_limit, dc_limit)
            return self.vm.api.set_charge_limits(token_obj, vehicle, soc_level, dc_limit)

        return self._call_with_reauth(_do)

class WallboxCharger:
    def __init__(self, username: str, password: str):
        self.client = Wallbox(username, password)
        self.charger_id = None  # defer until we can reliably fetch
        # Try once, but never crash if Wallbox isn't ready yet
        try:
            self.client.authenticate()
            ids = self.client.getChargersList()
            if ids:
                self.charger_id = ids[0]
                click.echo(f"DEBUG: Available charger IDs: {ids}")
            else:
                click.echo("WARNING: No Wallbox chargers visible yet; will retry on first use.")
        except Exception as e:
            # Defer to first use; _ensure_session() will retry with backoff
            click.echo(f"Wallbox init: deferring auth/list due to temporary error: {e!r}")

    def _ensure_session(self):
        """
        Ensure we are authenticated and have a charger_id.
        Called lazily by public methods so init failures don't kill the process.
        """
        # Authenticate if token missing/expired
        try:
            # authenticate() is idempotent in wallbox lib; call once here
            self.client.authenticate()
        except Exception as e:
            # One short backoff + retry to ride out transient startup/network issues
            click.echo(f"Wallbox auth failed, retrying shortly: {e!r}")
            time.sleep(3)
            self.client.authenticate()

        # Ensure we have a charger id
        if not self.charger_id:
            ids = self.client.getChargersList()
            if not ids:
                raise RuntimeError("No Wallbox chargers available after auth retry")
            self.charger_id = ids[0]
            click.echo(f"DEBUG: Available charger IDs: {ids}")

    def _call_with_reauth(self, func, *args, **kwargs):
        """
        Wrap a Wallbox API call with lightweight 401/429/connection retry.
        """
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                click.echo("Wallbox session unauthorized. Re-authenticating...")
                self._ensure_session()
                return func(*args, **kwargs)
            if status == 429:
                click.echo("Wallbox rate limit (429). Backing off 10s and retrying?")
                time.sleep(10)
                return func(*args, **kwargs)
            raise
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            click.echo(f"Wallbox network error: {e!r}. Backing off 3s and retrying?")
            time.sleep(3)
            self._ensure_session()
            return func(*args, **kwargs)

    def set_current(self, amps: int):
        """
        Set the maximum charging current (in amps) on the Pulsar Plus.
        """
        self._ensure_session()
        return self._call_with_reauth(self.client.setMaxChargingCurrent, self.charger_id, amps)

    def get_status(self) -> dict:
        self._ensure_session()
        raw_status = self._call_with_reauth(self.client.getChargerStatus, self.charger_id)

        # --- harden against string / non-dict payloads ---
        if isinstance(raw_status, str):
            try:
                import json as _json
                raw_status = _json.loads(raw_status)
            except Exception:
                raw_status = {}
        if not isinstance(raw_status, dict):
            raw_status = {}

        cfg = raw_status.get('config_data', {}) or {}
        current = cfg.get('max_charging_current')
        sid     = raw_status.get('status_id')

        # Treat only known "disconnected" codes as not connected.
        connected       = sid not in {0, 163}
        charging_codes  = {193, 194, 195}
        charging        = sid in charging_codes

        return {
            'current':   current,
            'status_id': sid,
            'connected': connected,
            'charging':  charging
        }

class DecisionEngine:
    def __init__(self, config):
        self.cfg = config
        self.hysteresis = config.get('hysteresis_watts', 500)

    def compute_amps(self, readings):
        solar = readings['solar_power']
        load = readings['house_load']
        headroom = max(0, solar - load)
        do_charge = headroom > self.hysteresis
        amps = int(headroom / self.cfg['voltage'] // self.cfg['step_size'] * self.cfg['step_size'])
        return max(self.cfg['min_amps'], min(self.cfg['max_amps'], amps)), do_charge

# ----- CLI -----
@click.group()
@click.option('--config', '-c', default='config.yaml', help='Path to config file')
@click.pass_context
def cli(ctx, config):
    with open(config) as f:
        cfg = yaml.safe_load(f)
    logging.basicConfig(level=cfg.get('log_level', 'INFO'))
    ctx.obj = cfg

@cli.command()
@click.pass_context
def start(ctx):
    cfg = ctx.obj

     # Initialize sensors and controllers
    tesla = TeslaSensor(
    refresh_token=cfg["tesla_refresh_token"],
    client_id=cfg.get("tesla_client_id", "ownerapi")
    )

    bluelink = BlueLinkSensor(
        cfg,
        username=cfg['bluelink_user'],
        password=cfg['bluelink_pass'],
        pin=cfg['bluelink_pin'],
        region_cfg=cfg['bluelink_region'],
        brand_cfg=cfg['bluelink_brand'],
        vin=cfg['vehicle_vin']
    )

#    click.echo("BL obj:", bluelink.__class__.__name__, "from", getattr(bluelink.__class__, "__module__", "?"))
#    click.echo("Has get_vehicle_status?", hasattr(bluelink, "get_vehicle_status"))
#    if not hasattr(bluelink, "get_vehicle_status"):
#        click.echo("Methods:", [m for m in dir(bluelink) if m.startswith("get_") or m.startswith("Get_")])
        
    charger = WallboxCharger(
        username=cfg['wallbox_user'],
        password=cfg['wallbox_pass']
    )
    engine = DecisionEngine(cfg)
    interval = cfg.get('poll_interval', 300)
    last_state = None
    last_cmd_ts = 0.0
    MIN_CMD_INTERVAL = cfg.get("min_cmd_interval_s", 20)
    MIN_STARTSTOP_INTERVAL = cfg.get("min_startstop_interval_s", 60)
    
    click.echo('Starting controller loop...')

    target_soc = cfg['ev_target_soc']

    def _evse_power_watts(charger_status, cfg) -> float:
        try:
            if charger_status.get("charging"):
                amps = float(charger_status.get("current") or 0.0)
                return amps * float(cfg.get("voltage", 240.0)) * float(cfg.get("pf", 1.0))
        except Exception:
            pass
        return 0.0

    def _in_window(now, start_hour, end_hour):
        """Return True if now is within [start_hour → end_hour) with midnight wrap."""
        if start_hour <= end_hour:
            return start_hour <= now.hour < end_hour
        # window wraps past midnight
        return now.hour >= start_hour or now.hour < end_hour

    last_set_current = None
    state = None

    consec_errors = 0
    max_backoff = 60  # seconds

    poll_s = int(cfg.get("poll_interval", 300))      # seconds between checks
    jitter_s = float(cfg.get("poll_jitter_s", 0.5))   # optional random spread

    # start immediately
    next_tick = time.monotonic()

    while True:
        try:
            date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
            click.echo(date_str)
            readings = tesla.get_house_power()
            ev_status = bluelink.get_vehicle_status()
            charger_status = charger.get_status()
            state = None; amps_wanted = None; reason = ""
            # --- type hardening (avoid 'str'.get crashes) ---
            if isinstance(ev_status, str):
                try: ev_status = json.loads(ev_status)
                except Exception: ev_status = {}
            if not isinstance(ev_status, dict):
                ev_status = {}

            if isinstance(charger_status, str):
                try: charger_status = json.loads(charger_status)
                except Exception: charger_status = {}
            if not isinstance(charger_status, dict):
                charger_status = {}
            click.echo({
                "solar_power": readings["solar_power"],
                "house_load": readings["house_load"],
                "battery_soc": readings.get("battery_soc"),
                "ev_soc": ev_status.get("soc"),
                "ev_charging": ev_status.get("charging"),
                "ev_charging_power_kW": ev_status.get("charging_power_kW")
            })
            click.echo(f"Charger status: {charger_status}")

            # --- start-of-tick reset to avoid sticky state/reason across iterations ---
            state = None
            amps_wanted = None
            reason = ""
            
            plugged_evse = bool(charger_status.get("connected"))
            plugged_bl   = bool(ev_status.get("plugged_in"))
            plugged      = plugged_evse   # EVSE pilot is the ground truth

            charging_evse = bool(charger_status.get("charging"))
            charging_bl   = bool(ev_status.get("charging"))
            charging_actual = charging_evse

            if plugged:
# 1) Determine desired target SOC (bump to surplus target on strong solar)
                evse_w = _evse_power_watts(charger_status, cfg)
                base_house = max(0.0, readings["house_load"] - evse_w)  # house load excluding EVSE
                headroom   = readings["solar_power"] - base_house

                click.echo({"eff_headroom": round(headroom,1), "evse_w": round(evse_w,1), "base_house": round(base_house,1)})

                neg_headroom = headroom <= 0
                batt_soc = readings.get("battery_soc", 0)
                desired_target = cfg["ev_target_soc"]
                surplus_target = cfg.get("ev_target_soc_solar_surplus", desired_target)
                if headroom > engine.hysteresis and batt_soc >= cfg['battery_soc_full_threshold_high']:
                    desired_target = surplus_target

                # 2) Ensure AC target SOC matches desired_target (apply once per mismatch)
                try:
                    ac_soc_value = bluelink.get_ac_target_soc()
                except Exception:
                    ac_soc_value = None
                if ac_soc_value != desired_target and (time.time() - last_cmd_ts) > MIN_CMD_INTERVAL:
                    bluelink.set_ac_target_soc(desired_target)
                    last_cmd_ts = time.time()

                # 3) Classify state: TARGET_REACHED, CHARGING_GRID, CHARGING_SOLAR, WAIT_SOLAR, UNPLUGGED
                ev_charge_level = ev_status.get("soc") or 0
                now = datetime.now()
                in_grid_window = _in_window(now, cfg["grid_charge_start_hour"], cfg["grid_charge_end_hour"])

                # Safety: no solar surplus outside grid window → force WAIT_SOLAR immediately
                if headroom <= 0 and not in_grid_window:
                    state = "WAIT_SOLAR"; amps_wanted = None; reason = "negative headroom (safety)"
                click.echo({"now": now.strftime("%F %T"), 
                       "grid_window": [cfg["grid_charge_start_hour"], cfg["grid_charge_end_hour"]], 
                       "in_grid_window": in_grid_window})

                if headroom <= 0 and not in_grid_window:
                    state = "WAIT_SOLAR"; amps_wanted = None; reason = "negative headroom (safety)"

                target_reached = ev_charge_level >= desired_target

                # decide amps for grid window if applicable
                grid_amps = None
                if in_grid_window and ev_charge_level < cfg["ev_target_soc"]:
                    if cfg.get("grid_charge_fast", False):
                        grid_amps = cfg["grid_charge_fast_amps"]
                    else:
                        # compute the minimum amps to reach target by end of window (fallback to max if unknown)
                        try:
                            # end-of-window absolute datetime
                            end = now.replace(hour=cfg["grid_charge_end_hour"], minute=0, second=0, microsecond=0)
                            if not _in_window(now, cfg["grid_charge_end_hour"], cfg["grid_charge_start_hour"]):
                                # ensure end is “next” window end if we’re already past it today
                                if (cfg["grid_charge_start_hour"] > cfg["grid_charge_end_hour"] and now.hour >= cfg["grid_charge_end_hour"]) \
                                   or (cfg["grid_charge_start_hour"] <= cfg["grid_charge_end_hour"] and now.hour >= cfg["grid_charge_end_hour"]):
                                    end += timedelta(days=1)
                            hours_left = max(0.1, (end - now).total_seconds() / 3600.0)  # prevent div/0
                            if "ev_battery_capacity_kwh" in cfg:
                                needed_kwh = max(0.0, (cfg["ev_target_soc"] - ev_charge_level) / 100.0 * cfg["ev_battery_capacity_kwh"])
                                amps = int(needed_kwh * 1000.0 / (cfg["voltage"] * hours_left))
                                grid_amps = max(cfg["min_amps"], min(cfg["max_amps"], amps))
                            else:
                                grid_amps = cfg["max_amps"]
                        except Exception:
                            grid_amps = cfg["max_amps"]

                # state resolution
                
                if not plugged:
                    state = "UNPLUGGED"; amps_wanted = None; reason = "car not plugged"
                elif target_reached:
                    state = "TARGET_REACHED"; amps_wanted = None; reason = "target met"
                elif in_grid_window and grid_amps:
                    state = "CHARGING_GRID"; amps_wanted = grid_amps; reason = "grid window"
                # --- Maintenance: reconcile EVSE amps even without a state transition ---

                # To avoid thrash, if the Powerwall has fallen past the low level, stop charging the EV
                if state == "CHARGING_SOLAR":
                    if readings.get("battery_soc") < cfg.get("battery_soc_full_threshold_low"):
                        state = "WAIT_SOLAR"
                    
                if state in ("CHARGING_GRID", "CHARGING_SOLAR"):
                    # ensure readings_eff exists for any fallback compute_amps
                    readings_eff = dict(readings); readings_eff["house_load"] = base_house
                    # decide target amps for this state
                    desired_a = amps_wanted
                    if desired_a is None:
                        # belt & suspenders fallback
                        desired_a = engine.compute_amps(readings_eff)[0]

                    # current reading from EVSE
                    cur_a  = int(charger_status.get("current") or cfg.get("default_amps", 6))

                    # smoothing / quantization
                    step   = int(cfg.get("amp_step", 1))
                    ramp   = int(cfg.get("ramp_limit_amps", 6))
                    min_dA = int(cfg.get("min_delta_amps", 1))
                    max_a  = int(cfg.get("max_amps", 40))
                    min_a  = int(cfg.get("min_amps", 6))

                    desired_a = max(min_a, min(max_a, desired_a))
                    # quantize to step
                    desired_a = (desired_a // step) * step

                    # only act if changed meaningfully and not already what we set last time
                    if (abs(desired_a - cur_a) >= min_dA) and (desired_a != last_set_current):
                        # ramp to avoid big jumps
                        if desired_a > cur_a:
                            desired_a = min(cur_a + ramp, desired_a)
                        else:
                            desired_a = max(cur_a - ramp, desired_a)

                        charger.set_current(desired_a)
                        last_set_current = desired_a
                        last_cmd_ts = time.time()

                    # ensure charge session is running
                    if not charging_actual:
                        if plugged_evse:
                            try:
                                ev_soc = ev_status.get("soc") or 0
                                ac_soc = bluelink.get_ac_target_soc()
                            except Exception:
                                ac_soc = None

                            bump_target = max(desired_target or ev_soc, ev_soc + 1)
                            if (ac_soc is None) or (ac_soc < bump_target):
                                try:
                                    bluelink.set_ac_target_soc(min(100, bump_target))
                                except Exception as e:
                                    click.echo(f"set_ac_target_soc failed: {e}")

                            bluelink.start_charge()
                            click.echo("Starting charging")
                            last_cmd_ts = time.time()
                        else:
                            click.echo("Skip start_charge: EVSE not connected")

                else:
                    # Use EVSE-excluded house load for all surplus math
                    # Use PW SOC + effective headroom to allow solar charging
                    batt_soc = readings.get("battery_soc", 0)
                    batt_full_thr = int(cfg.get("battery_soc_full_threshold_high", 99))

                    
                    # effective headroom already computed earlier as:
                    #   evse_w    = _evse_power_watts(charger_status, cfg)
                    #   base_house = max(0.0, readings["house_load"] - evse_w)
                    #   headroom   = readings["solar_power"] - base_house
                    if neg_headroom and not in_grid_window:
                        state = "WAIT_SOLAR"; amps_wanted = None
                        reason = "negative headroom"
                    else:
                        # still feed engine with EVSE-excluded house load (for any other internal calcs)
                        readings_eff = dict(readings); readings_eff["house_load"] = base_house
                        amps_calc, do_charge = engine.compute_amps(readings_eff)

                        if (batt_soc >= batt_full_thr) and (headroom > getattr(engine, "hysteresis", 0)):
                            # set amps to soak up the actual surplus
                            amps_wanted = engine.compute_amps(readings)[0]
                            state = "CHARGING_SOLAR"; reason = "solar surplus"
                        else:
                            state = "WAIT_SOLAR"; amps_wanted = None
                            if batt_soc < batt_full_thr:
                                reason = f"PW SOC {batt_soc:.1f}% < {batt_full_thr}%"
                            else:
                                reason = "waiting for solar"                # Never keep CHARGING_GRID outside its window
                if state == "CHARGING_GRID" and not in_grid_window:
                    state = "WAIT_SOLAR"; amps_wanted = None; reason = "outside grid window (safety)"



                click.echo(f"STATE={state} ({reason})")

                # Structured tick forensics
                click.echo({"now": now.strftime("%F %T"),
                       "state": state, "reason": reason,
                       "in_grid_window": in_grid_window,
                       "plugged_evse": plugged_evse, "plugged_bl": plugged_bl,
                       "charging_evse": charging_evse, "charging_bl": charging_bl})


                # 4) Act only on transitions or after cooldown to avoid thrash
                now_ts = time.time()
                should_stop_now = (state in ("TARGET_REACHED", "WAIT_SOLAR")) and charging_actual and not in_grid_window
                # Safety: never keep CHARGING_GRID outside its window
                if state == "CHARGING_GRID" and not in_grid_window:
                    state = "WAIT_SOLAR"; amps_wanted = None; reason = "outside grid window (safety)"

                should_act = (state != last_state) or ((now_ts - last_cmd_ts) > MIN_STARTSTOP_INTERVAL) or should_stop_now

                if should_act:
                    if state in ("CHARGING_GRID", "CHARGING_SOLAR"):
                        if amps_wanted is None:
                            # final belt & suspenders
                            amps_wanted = engine.compute_amps(readings)[0]
                        charger.set_current(amps_wanted)
                        if not charging_actual:
                            if plugged_evse:
                                try:
                                    ev_soc = ev_status.get("soc") or 0
                                    ac_soc = bluelink.get_ac_target_soc()
                                except Exception:
                                    ac_soc = None

                                bump_target = max(desired_target or ev_soc, ev_soc + 1)
                                if (ac_soc is None) or (ac_soc < bump_target):
                                    try:
                                        bluelink.set_ac_target_soc(min(100, bump_target))
                                    except Exception as e:
                                        click.echo(f"set_ac_target_soc failed: {e}")

                                bluelink.start_charge()
                                click.echo("Starting charging")
                                last_cmd_ts = time.time()
                            else:
                                click.echo("Skip start_charge: EVSE not connected")

                    elif state in ("TARGET_REACHED", "WAIT_SOLAR"):
                        if ev_status.get("charging"):
                            charger.set_current(cfg["default_amps"])
                            bluelink.stop_charge()
                            click.echo(f"Stopping charging")
                            last_cmd_ts = now_ts

                    # UNPLUGGED or anything else → no action
                    last_state = state

            else:
                click.echo("Vehicle not plugged in; skipping")
            consec_errors = 0

        except KeyboardInterrupt:
            click.echo("Exiting on Ctrl+C"); break
                
        #except Exception as e:
        #    consec_errors += 1
        #    backoff = min(2 * consec_errors, max_backoff)
        #    click.echo(f"Tick error ({type(e).__name__}): {e}. Backing off {backoff}s, then continuing.")
        #    time.sleep(backoff)

        except Exception as e:
            consec_errors += 1
            backoff = min(2 * consec_errors, max_backoff)
            # --- DIAGNOSTIC: show exact line and inputs causing the crash ---
            import traceback
            exc_type = type(e).__name__
            print(f"Tick error ({exc_type}): {e!r}. Backing off 2s, then continuing.")
            # Full stacktrace with file & line numbers
            traceback.print_exc()

            # Dump the types and short previews of the loop inputs
            def _short(x, n=400):
                try:
                    s = repr(x)
                    return s if len(s) <= n else s[:n] + "...[trunc]"
                except Exception:
                    return f"<unrepr-able {type(x).__name__}>"

            try:
                print("DEBUG types:",
                      "readings=", type(readings).__name__,
                      "ev_status=", type(ev_status).__name__,
                      "charger_status=", type(charger_status).__name__)
            except NameError:
                pass

            try:
                print("DEBUG previews:",
                      "readings=", _short(readings),
                      "ev_status=", _short(ev_status),
                      "charger_status=", _short(charger_status))
            except NameError:
                pass

            # keep existing behavior
            time.sleep(backoff)
            continue

            
        # normal sleep to next tick (your poll_interval + jitter)
        next_tick += poll_s
        time.sleep(max(0, next_tick - time.monotonic()) + random.uniform(0, jitter_s))
        
if __name__ == '__main__':
    cli()
