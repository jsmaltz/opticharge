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

from hyundai_kia_connect_api import VehicleManager, const
from datetime import datetime, timedelta

# Turn on low-level HTTP debug logging
#http_client.HTTPConnection.debuglevel = 1
#logging.basicConfig(level=logging.DEBUG)
#logging.getLogger("urllib3").setLevel(logging.DEBUG)

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
        #print(live)
        #breakpoint()
        solar = live.get("solar_power", 0)
        #solar = 4000
        load  = live.get("load_power", 0)
        batt  = live.get("percentage_charged", None)

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
            "house_load" : load,
            "battery_soc": batt,
        }


class BlueLinkSensor:
    def __init__(
        self,
        username: str,
        password: str,
        pin: str,
        region_cfg: int | str,
        brand_cfg: int | str,
        vin: str
    ):
        # normalize region and brand as before…
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
            raise RuntimeError(f"VIN '{vin}' not found; available VINs: {available}")
        
    def _call_with_reauth(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                print("Bluelink session expired. Re-authenticating...")
                self.authenticate()
                return func(*args, **kwargs)
            elif e.response.status_code == 429:
                print("Bluelink rate limit hit (429). Sleeping and retrying once...")
                time.sleep(10)  # backoff to avoid hammering
                return func(*args, **kwargs)
            else:
                raise
            
    def start_charge(self) -> dict:
        """
        Tell the Hyundai Ioniq to begin charging immediately via OEM /control/reservecharge.
        """
        # 1) Login directly against the SPA API and get the token object
        token_obj = self.vm.api.login(self.username, self.password)  

        # 2) Grab the Vehicle so we can build headers
        vehicle = self.vm.get_vehicle(self.vehicle_id)

        result = self.vm.api.start_charge(token_obj, vehicle)

        print(result)
        
        return result

    def stop_charge(self) -> dict:
        """
        Tell the Hyundai Ioniq to stop charging immediately.
        """
        # 1) Re-login to get a fresh Token object
        token_obj = self.vm.api.login(self.username, self.password)  

        # 2) Get the Vehicle instance
        vehicle = self.vm.get_vehicle(self.vehicle_id)

        # 3) Call the built-in helper; it wraps the POST to /control/reservecharge with reservChargeSet=0
        result = self.vm.api.stop_charge(token_obj, vehicle)

        return result

    def get_vehicle_status(self) -> dict:
        # Refresh cached state
        self.vm.update_vehicle_with_cached_state(self.vehicle_id)
        car = self.vm.get_vehicle(self.vehicle_id)
        # These attrs exist on the Vehicle object:
        plugged = getattr(car, 'ev_battery_is_plugged_in', False)
        charging = getattr(car, 'ev_battery_is_charging', False)
        soc = getattr(car, 'ev_battery_percentage', None)

        return {
            'plugged_in': plugged,
            'charging':   charging,
            'soc':        soc
        }

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

        print(f"DEBUG: setMaxChargingCurrent returned: {result}")
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

    bluelink = BlueLinkSensor(
        username=cfg['bluelink_user'],
        password=cfg['bluelink_pass'],
        pin=cfg['bluelink_pin'],
        region_cfg=cfg['bluelink_region'],
        brand_cfg=cfg['bluelink_brand'],
        vin=cfg['vehicle_vin']
    )
    charger = WallboxCharger(
        username=cfg['wallbox_user'],
        password=cfg['wallbox_pass']
    )
    engine = DecisionEngine(cfg)
    interval = cfg.get('poll_interval', 300)

    click.echo('Starting controller loop...')
    while True:
        date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        print(date_str)
        readings = tesla.get_house_power()
        ev_status = bluelink.get_vehicle_status()
        charger_status = charger.get_status()
        print(readings)
        print(charger_status)

        if ev_status['plugged_in']:
            ev_charge_level = ev_status['soc']
            print(f'ev plugged in, with charge level {ev_charge_level}')
            # Determine effective target SOC based on headroom and Powerwall state
            headroom = readings['solar_power'] - readings['house_load']
            batt_soc = readings.get('battery_soc', 0)
            print(f'Powerwall charge level {batt_soc}')
            target_soc = cfg['ev_target_soc']
            if headroom > engine.hysteresis and batt_soc >= 99:
                target_soc = 100
            # Time-based override after 11 PM or start of off-peak
            now = datetime.now()
            if now.hour >= cfg['grid_charge_start_hour'] and ev_charge_level < cfg['ev_target_soc']:
                # calculate hours until next 6 AM or similar end of off-peak
                next_six = now.replace(hour=cfg['grid_charge_end_hour'], minute=0, second=0, microsecond=0)
                if now.hour >= cfg['grid_charge_end_hour']:
                    next_six += timedelta(days=1)
                hours_left = (next_six - now).total_seconds() / 3600
                # estimate amps (fallback to max if capacity unknown)
                if 'ev_battery_capacity_kwh' in cfg:
                    needed_kwh = (cfg['ev_target_soc'] - ev_charge_level) / 100 * cfg['ev_battery_capacity_kwh']
                    amps = int(needed_kwh * 1000 / (cfg['voltage'] * hours_left))
                    amps = max(cfg['min_amps'], min(cfg['max_amps'], amps))
                else:
                    amps = cfg['max_amps']
                charger.set_current(amps)
                click.echo(f"Set timed charger to {amps} A to reach {cfg['ev_target_soc']}% by 6 AM (in {hours_left:.1f}h)")
                bluelink.start_charge()

            elif ev_charge_level < target_soc:
                # Normal headroom-based charging
                amps, do_charge = engine.compute_amps(readings)
                if do_charge:
                    charger.set_current(amps)
                    click.echo(f"Set charger to {amps} A (headroom {headroom} W)")
                    bluelink.start_charge()
                else:
                    print('Not a good time to charge yet')
                    if ev_status['charging']==True:
                        print('Setting charger to default current.')
                        charger.set_current(cfg['default_amps'])
                        print('Stopping charging.')
                        bluelink.stop_charge()
                        breakpoint()
            else:
                click.echo("Target SOC reached; throttling to minimum")
                bluelink.stop_charge()
        else:
            click.echo("Vehicle not plugged in; skipping")

        time.sleep(interval)

if __name__ == '__main__':
    cli()
