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

        # 2) live_status ? solar, load, battery
        live_url = f"{self.owner_api_url}/api/1/energy_sites/{site_id}/live_status"
        r = self.session.get(live_url, headers=headers)
        if r.status_code == 401:
            self._refresh_access_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            r = self.session.get(live_url, headers=headers)
        r.raise_for_status()
        live = r.json()["response"]

        print(live.keys())
        solar = live.get("solar_power", -1)
        load  = live.get("load_power", -1)
        grid  = live.get("grid_power", -1)
        generator_power = live.get("generator_power", -1)
        
        # ? battery charge/discharge power (kW)
        batt_power = live.get("battery_power", 0)  # + = discharging, - = charging

        # battery SOC
        batt_soc = live.get("percentage_charged", None)

        if batt_soc is None:
            # fallback to site_info
            info_url = f"{self.owner_api_url}/api/1/energy_sites/{site_id}/site_info"
            r2 = self.session.get(info_url, headers=headers)
            r2.raise_for_status()
            info = r2.json()["response"]
            batt_soc = (
                info.get("battery", {}).get("percent")
                or info.get("battery_level")
                or info.get("percentage_charged")
            )

        return {
            "solar_power": solar,
            "house_load": load,
            "battery_soc": batt_soc,
            "battery_power": batt_power,
            "grid_power": batt_power,
            "generator_power": generator_power,             
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
                    print("BlueLink 401 ? reauth?")
                    self.authenticate()
                    continue
                if code == 429:
                    wait = 6 + attempt * 4
                    print(f"BlueLink 429 ? backoff {wait}s?")
                    time.sleep(wait)
                    continue
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
                # SDK saw an unexpected/missing field; treat as transient
                self._bluelink_fail_count += 1
                print(f"BlueLink payload error ({type(e).__name__}): {e} "
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
                print(f"BlueLink unexpected error: {e!r}. Retrying once?")
                time.sleep(2)
                continue

        # Final attempt (let exceptions bubble if it still fails)
        return func()

    def _full_reinit_bluelink(self):
        """Hard reset BlueLink by rebuilding VehicleManager exactly like authenticate()."""
        print("BlueLink: performing FULL client re-init?")
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

        print("BlueLink: FULL re-init complete.")

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
                    print("BlueLink 401 ? reauth?")
                    self.authenticate()
                    continue
                if code == 429:
                    wait = 6 + attempt * 4
                    print(f"BlueLink 429 ? backoff {wait}s?")
                    time.sleep(wait)
                    continue
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
                self._bluelink_fail_count += 1
                print(f"BlueLink payload error ({type(e).__name__}): {e} [fail#{self._bluelink_fail_count}]")

                if self._bluelink_fail_count >= 1:
                    print("??  BlueLink circuit-breaker TRIPPED ? full re-init")
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
                print("??  BlueLink circuit-breaker TRIPPED ? full re-init")
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
                print(f"BlueLink unexpected error: {e!r}. Retrying once?")
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
                    print(f"refresh failed: {e}. fail#{self._refresh_fail_count}. Cooling off 30s.")
                    self._skip_next_refresh_until = now + 30
                    if self._refresh_fail_count >= self.cfg['bluelink_refresh_fail_count']:
                        print("BlueLink refresh failures reached threshold ? full re-init")
                        self._full_reinit_bluelink()
                        self._refresh_fail_count = 0
                        self.vm.update_vehicle_with_cached_state(self.vehicle_id)

            car = self.vm.get_vehicle(self.vehicle_id)

            target_ac = getattr(car, "ev_charge_limits_ac", None)
            target_dc = getattr(car, "ev_charge_limits_dc", None)
            charging_power_kW = getattr(car, "ev_charging_power", None)
            
            # try per-plug list if present
            try:
                rci = (car.data or {}).get("vehicleStatus", {}).get("evStatus", {}).get("reservChargeInfos", {}) or {}
                tslist = rci.get("targetSOClist", []) or []
                for item in tslist:
                    if item.get("plugType") == 0:
                        target_ac = item.get("targetSOClevel", target_ac)
                    elif item.get("plugType") == 1:
                        target_dc = item.get("targetSOClevel", target_dc)
            except Exception:
                pass

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

            try:
                target_soc_list = vehicle.data["vehicleStatus"]["evStatus"]["reservChargeInfos"]["targetSOClist"]
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

            # Read current DC target from vehicle data
            target_soc_list = vehicle.data["vehicleStatus"]["evStatus"]["reservChargeInfos"]["targetSOClist"]
            dc_limit = next(item["targetSOClevel"] for item in target_soc_list if item["plugType"] == 1)

            # set_charge_limits(api_token, vehicle_obj, ac_limit, dc_limit)
            return self.vm.api.set_charge_limits(token_obj, vehicle, soc_level, dc_limit)

        return self._call_with_reauth(_do)

class WallboxCharger:
    def __init__(self, username: str, password: str):
        self.client = Wallbox(username, password)
        self.client.authenticate()
        # print the chargers we see
        ids = self.client.getChargersList()
        print("DEBUG: Available charger IDs:", ids)
        self.charger_id = ids[0]

    def _call_with_reauth(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                print("Wallbox session expired. Re-authenticating...")
                self.client.authenticate()
                return func(*args, **kwargs)
            elif e.response.status_code == 429:
                print("Wallbox rate limit hit (429). Sleeping and retrying once...")
                time.sleep(10)  # backoff to avoid hammering
                return func(*args, **kwargs)
            else:
                raise

    def set_current(self, amps: int):
        """
        Set the maximum charging current (in amps) on the Pulsar Plus.
        """
        # This will send the HTTP request to change the current
        result = self._call_with_reauth(self.client.setMaxChargingCurrent, self.charger_id, amps)

        #print(f"DEBUG: setMaxChargingCurrent returned: {result}")
        return result
    
    def get_status(self) -> dict:
        raw_status = self._call_with_reauth(self.client.getChargerStatus, self.charger_id)
        cfg = raw_status.get('config_data', {})
        current = cfg.get('max_charging_current')
        sid     = raw_status.get('status_id')

        connected     = sid not in {0, 163}
        charging_codes = {193, 194, 195}
        charging      = sid in charging_codes

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

    click.echo('Starting measurement loop...')

    last_set_current = None
    state = None

    consec_errors = 0
    max_backoff = 60  # seconds

    poll_s = int(cfg.get("poll_interval", 300))      # seconds between checks
    jitter_s = float(cfg.get("poll_jitter_s", 0.5))   # optional random spread

    poll_s = 20
    
    # start immediately
    next_tick = time.monotonic()

    while True:
        date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        print(date_str)
        readings = tesla.get_house_power()
        
        print(readings)
            
        # normal sleep to next tick (your poll_interval + jitter)
        next_tick += poll_s
        time.sleep(max(0, next_tick - time.monotonic()) + random.uniform(0, jitter_s))
        
if __name__ == '__main__':
    cli()
