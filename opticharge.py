import time
import yaml
import logging
import click
import requests
from wallbox import Wallbox
from bluelink import BlueLink

import logging
import http.client as http_client
import requests
import certifi
import time
import random

from hyundai_kia_connect_api import VehicleManager, const
from datetime import datetime, timedelta
from enum import Enum, auto

import json
from pathlib import Path
from tesla_fleet import DEFAULT_API_BASE_URL, DEFAULT_TOKEN_URL, TeslaSensor
from tesla_local import sensor_from_config as local_tesla_sensor_from_config
from solar_forecast import SolarForecast
# Turn on low-level HTTP debug logging
#http_client.HTTPConnection.debuglevel = 1
#logging.basicConfig(level=logging.DEBUG)
#logging.getLogger("urllib3").setLevel(logging.DEBUG)

#class TransientAPIError(Exception):
#    pass


# ----- Sensors -----

class LegacyTeslaSensor:
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
        try:
            r = self.session.post(self.token_url, data=payload)
        except requests.RequestException as e:
            print(f"TESLA token POST error: {e!r}")
            raise
        if r.status_code != 200:
            print(f"TESLA token status={r.status_code} body={r.text[:200]!r}")

        r.raise_for_status()
        j = r.json()
        self.access_token  = j["access_token"]
        # Tesla may rotate the refresh token
        self.refresh_token = j.get("refresh_token", self.refresh_token)

    def _get_products(self):
        if self._products_json is None:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            url = f"{self.owner_api_url}/api/1/products"
            try:
                r = self.session.get(url, headers=headers)
            except requests.RequestException as e:
                print(f"TESLA products GET error: {e!r}")
                raise
            if r.status_code != 200:
                print(f"TESLA products status={r.status_code} body={r.text[:200]!r}")

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
                print(f"TESLA products unexpected shape: {type(resp).__name__} → {str(resp)[:200]!r}")
                raise RuntimeError(f"Could not find energy_site_id in products payload")
            print(f"TESLA site_id resolved: {self._site_id}")

        return self._site_id

    def get_house_power(self) -> dict:
        # 1) ensure we have a valid access token
        if not self.access_token:
            self._refresh_access_token()

        headers = {"Authorization": f"Bearer {self.access_token}"}
        site_id = self._get_site_id()

        # 2) live_status → solar & load (and maybe battery_level)
        live_url = f"{self.owner_api_url}/api/1/energy_sites/{site_id}/live_status"
        try:
            r = self.session.get(live_url, headers=headers)
        except requests.RequestException as e:
            print(f"TESLA live_status GET error: {e!r}")
            raise
        if r.status_code == 401:
            # expired token? retry once
            self._refresh_access_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            print("TESLA live_status retry after token refresh…")
            r = self.session.get(live_url, headers=headers)
        if r.status_code != 200:
            print(f"TESLA live_status status={r.status_code} body={r.text[:200]!r}")
            
        r.raise_for_status()
        try:
            body = r.json()
        except json.JSONDecodeError as e:
            print(f"TESLA live_status JSON decode error: {e!r} body={r.text[:200]!r}")
            raise
        if not isinstance(body, dict) or "response" not in body:
            print(f"TESLA live_status unexpected payload: type={type(body).__name__} preview={str(body)[:200]!r}")
            live = {}
        else:
            live = body["response"]
        
        if isinstance(live, str):
            try:
                live = json.loads(live)
                print("NOTE: Tesla live payload was JSON string; parsed to dict.")
            except Exception:
                live = {}
        if not isinstance(live, dict):
            live = {}

                    # ---- DIAGNOSTIC: show the live payload shape
        try:
            print(f"TESLA live_status keys: {sorted(list(live.keys()))}")
        except Exception:
            pass

        # ---- Owner API standard keys
        def _num(x, default=0.0):
            try:
                return float(x) if x is not None else default
            except Exception:
                return default

        solar = _num(live.get("solar_power"), 0.0)
        house = _num(live.get("load_power"), 0.0)
        batt = _num(live.get("percentage_charged"))

        # Fallback ONLY if SOC missing or zero (some tenants report 0 here)
        if not batt:
            info_url = f"{self.owner_api_url}/api/1/energy_sites/{site_id}/site_info"
            r2 = self.session.get(info_url, headers=headers)
            if r2.status_code != 200:
                print(f"TESLA site_info status={r2.status_code} body={r2.text[:200]!r}")
            r2.raise_for_status()
            info_body = r2.json()
            info = info_body.get("response", info_body) if isinstance(info_body, dict) else {}
            batt = _num(
                info.get("battery", {}).get("percent")
                or info.get("battery_level")
                or info.get("percentage_charged"),
                0.0
            )

        # One concise line to verify actual numbers we’re using
        print(f"TESLA parsed: solar={solar:.1f}W, load={house:.1f}W, batt={batt:.1f}%")

        return {
            "solar_power": solar,
            "house_load":  house,
            "battery_soc": batt,
        }

class BlueLinkSensor:
    """BlueLink adapter with proactive token refresh and stale-data fallback."""

    def __init__(
        self,
        cfg,
        username: str,
        password: str,
        pin: str,
        region_cfg: int | str,
        brand_cfg: int | str,
        vin: str,
    ):
        self.cfg = cfg
        self._status_failure_count = 0
        self._using_cached_status = False
        self._last_good_status = None
        self._last_good_status_at = 0.0
        self._reinit_failure_threshold = max(
            1, int(cfg.get("bluelink_reinit_fail_count", 3))
        )

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
        data = getattr(vehicle, "data", None)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {}
        return data if isinstance(data, dict) else {}

    def _vehicle_id_for(self, manager):
        for internal_id, vehicle in manager.vehicles.items():
            vehicle_vin = getattr(vehicle, "vin", getattr(vehicle, "VIN", None))
            if vehicle_vin == self.vin:
                return internal_id
        available = [
            getattr(vehicle, "vin", getattr(vehicle, "VIN", None))
            for vehicle in manager.vehicles.values()
        ]
        raise RuntimeError(f"VIN '{self.vin}' not found; available VINs: {available}")

    def authenticate(self):
        manager = VehicleManager(
            region=self.region_id,
            brand=self.brand_id,
            username=self.username,
            password=self.password,
            pin=self.pin,
        )
        manager.check_and_refresh_token()
        vehicle_id = self._vehicle_id_for(manager)
        self.vm = manager
        self.vehicle_id = vehicle_id

    def _full_reinit_bluelink(self):
        click.echo("BlueLink: rebuilding client after repeated refresh failures")
        self.authenticate()

    def _call_with_reauth(self, func: callable):
        """Run one API operation with token preflight and bounded retries."""
        last_error = None
        for attempt in range(3):
            try:
                self.vm.check_and_refresh_token()
                return func()
            except requests.exceptions.HTTPError as exc:
                last_error = exc
                status = getattr(exc.response, "status_code", None)
                if status in (401, 403):
                    click.echo("BlueLink session rejected; authenticating again")
                    self.authenticate()
                    continue
                if status == 429 or status in (500, 502, 503, 504):
                    delay = 6 + attempt * 4
                    click.echo(
                        f"BlueLink transient HTTP {status}; retrying in {delay}s"
                    )
                    time.sleep(delay)
                    continue
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
                delay = 2 + attempt * 2
                click.echo(f"BlueLink network error; retrying in {delay}s")
                time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("BlueLink operation failed without an error")

    def _status_from_vehicle(self) -> dict:
        car = self.vm.get_vehicle(self.vehicle_id)
        target_ac = getattr(car, "ev_charge_limits_ac", None)
        target_dc = getattr(car, "ev_charge_limits_dc", None)
        data = self._safe_vehicle_data(car)
        charge_info = (
            data.get("vehicleStatus", {})
            .get("evStatus", {})
            .get("reservChargeInfos", {})
        )
        for item in charge_info.get("targetSOClist", []) or []:
            if item.get("plugType") == 0:
                target_ac = item.get("targetSOClevel", target_ac)
            elif item.get("plugType") == 1:
                target_dc = item.get("targetSOClevel", target_dc)
        return {
            "plugged_in": bool(getattr(car, "ev_battery_is_plugged_in", False)),
            "charging": bool(getattr(car, "ev_battery_is_charging", False)),
            "soc": getattr(car, "ev_battery_percentage", None),
            "target_ac": target_ac,
            "target_dc": target_dc,
            "charging_power_kW": getattr(car, "ev_charging_power", None),
            "data_stale": False,
            "data_age_seconds": 0,
        }

    def _refresh_status(self) -> dict:
        def _do():
            self.vm.update_vehicle_with_cached_state(self.vehicle_id)
            return self._status_from_vehicle()

        return self._call_with_reauth(_do)

    def _record_fresh_status(self, status, recovery_message=None):
        if self._using_cached_status:
            click.echo(recovery_message or "BlueLink live data recovered")
        self._status_failure_count = 0
        self._using_cached_status = False
        self._last_good_status = dict(status)
        self._last_good_status_at = time.time()
        return status

    def _cached_status(self, error):
        if self._last_good_status is None:
            raise error
        if not self._using_cached_status:
            click.echo(
                f"BlueLink refresh unavailable ({type(error).__name__}: {error}); "
                "using last-known-good data"
            )
        self._using_cached_status = True
        status = dict(self._last_good_status)
        status["data_stale"] = True
        status["data_age_seconds"] = max(
            0, int(time.time() - self._last_good_status_at)
        )
        return status

    def get_vehicle_status(self) -> dict:
        try:
            return self._record_fresh_status(self._refresh_status())
        except Exception as exc:
            self._status_failure_count += 1
            if self._status_failure_count >= self._reinit_failure_threshold:
                try:
                    self._full_reinit_bluelink()
                    status = self._refresh_status()
                    return self._record_fresh_status(
                        status, "BlueLink live data recovered after client rebuild"
                    )
                except Exception as reinit_exc:
                    exc = reinit_exc
            return self._cached_status(exc)

    def get_ac_target_soc(self) -> int | None:
        """Read the AC target from the snapshot already fetched this cycle."""
        vehicle = self.vm.get_vehicle(self.vehicle_id)
        data = self._safe_vehicle_data(vehicle)
        target_soc_list = (
            data.get("vehicleStatus", {})
            .get("evStatus", {})
            .get("reservChargeInfos", {})
            .get("targetSOClist", [])
        )
        for item in target_soc_list or []:
            if item.get("plugType") == 0:
                return item.get("targetSOClevel")
        return getattr(vehicle, "ev_charge_limits_ac", None)

    def start_charge(self) -> dict:
        """Tell the Hyundai Ioniq to begin charging immediately."""
        return self._call_with_reauth(
            lambda: self.vm.api.start_charge(
                self.vm.token, self.vm.get_vehicle(self.vehicle_id)
            )
        )

    def stop_charge(self) -> dict:
        """Tell the Hyundai Ioniq to stop charging immediately."""
        return self._call_with_reauth(
            lambda: self.vm.api.stop_charge(
                self.vm.token, self.vm.get_vehicle(self.vehicle_id)
            )
        )

    def set_ac_target_soc(self, soc_level: int) -> dict:
        """Set AC charge limit while preserving the cached DC limit."""
        if not (50 <= soc_level <= 100):
            raise ValueError("SOC level must be between 50 and 100")

        def _do():
            vehicle = self.vm.get_vehicle(self.vehicle_id)
            data = self._safe_vehicle_data(vehicle)
            target_soc_list = (
                data.get("vehicleStatus", {})
                .get("evStatus", {})
                .get("reservChargeInfos", {})
                .get("targetSOClist", [])
            )
            dc_limit = next(
                (
                    item.get("targetSOClevel")
                    for item in target_soc_list or []
                    if item.get("plugType") == 1
                ),
                getattr(vehicle, "ev_charge_limits_dc", None),
            )
            if dc_limit is None:
                raise ValueError("BlueLink DC charge limit is unavailable")
            return self.vm.api.set_charge_limits(
                self.vm.token, vehicle, soc_level, dc_limit
            )

        return self._call_with_reauth(_do)

class WallboxCharger:

    class APIError(RuntimeError):
        """A Wallbox call failed after bounded recovery attempts."""

    class CircuitOpen(APIError):
        def __init__(self, retry_after: float):
            self.retry_after = max(0.0, retry_after)
            super().__init__(f"Wallbox circuit open for {self.retry_after:.0f}s")

    EVSE_STATUS_PLUGGED = {
        164, 165, 177, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189,
        193, 194, 195, 196, 209, 210
    }
    EVSE_STATUS_NOT_PLUGGED = {0, 161, 162, 163}


    def __init__(
        self,
        username: str,
        password: str,
        request_timeout: float = 10.0,
        jwt_token_drift: float = 120.0,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        circuit_breaker_failures: int = 3,
        circuit_breaker_seconds: float = 60.0,
    ):
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._max_retries = max(0, int(max_retries))
        self._retry_base_seconds = max(0.0, float(retry_base_seconds))
        self._circuit_breaker_failures = max(1, int(circuit_breaker_failures))
        self._circuit_breaker_seconds = max(1.0, float(circuit_breaker_seconds))
        self.client = Wallbox(
            username,
            password,
            requestGetTimeout=float(request_timeout),
            jwtTokenDrift=float(jwt_token_drift),
        )
        self.charger_id = None  # defer until we can reliably fetch
        # Try once, but never crash if Wallbox isn't ready yet
        try:
            self._authenticate()
            ids = self._call_with_reauth(self.client.getChargersList)
#            ids = self.client.getChargersList()
            if ids:
                self.charger_id = ids[0]
                click.echo(f"DEBUG: Available charger IDs: {ids}")
            else:
                click.echo("WARNING: No Wallbox chargers visible yet; will retry on first use.")
        except Exception as e:
            # Defer to first use; _ensure_session() will retry with backoff
            click.echo(f"Wallbox init: deferring auth/list due to temporary error: {e!r}")

    def _is_evse_plugged(self, status_id: int) -> bool:
        if status_id in self.EVSE_STATUS_NOT_PLUGGED:
            return False
        if status_id in self.EVSE_STATUS_PLUGGED:
            return True
        # Unknown/new code: be conservative (treat as NOT plugged)
        return False

    def _retry_delay(self, attempt: int, retry_after=None) -> float:
        if retry_after is not None:
            try:
                return max(0.0, min(float(retry_after), 60.0))
            except (TypeError, ValueError):
                pass
        base = self._retry_base_seconds * (2 ** attempt)
        return min(30.0, base + random.uniform(0.0, min(0.5, base / 4.0)))

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_breaker_failures:
            self._circuit_open_until = time.time() + self._circuit_breaker_seconds
            click.echo(
                f"Wallbox circuit opened for {self._circuit_breaker_seconds:.0f}s "
                f"after {self._consecutive_failures} failed calls"
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _force_fresh_authentication(self) -> None:
        """Discard JWT state so wallbox.authenticate() cannot return early."""
        for attribute, value in (
            ("jwtToken", ""),
            ("jwtRefreshToken", ""),
            ("jwtTokenTtl", 0),
            ("jwtRefreshTokenTtl", 0),
        ):
            if hasattr(self.client, attribute):
                setattr(self.client, attribute, value)
        headers = getattr(self.client, "headers", None)
        if isinstance(headers, dict):
            headers.pop("Authorization", None)
        self._authenticate()

    def _authenticate(self) -> None:
        for attempt in range(self._max_retries + 1):
            try:
                self.client.authenticate()
                return
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as caught:
                error = caught
                retryable = True
                status = None
            except requests.exceptions.HTTPError as caught:
                error = caught
                status = getattr(caught.response, "status_code", None)
                retryable = status == 429 or status in (500, 502, 503, 504)

            if not retryable or attempt >= self._max_retries:
                self._record_failure()
                raise self.APIError(
                    f"Wallbox authentication failed"
                    + (f" with HTTP {status}" if status else "")
                ) from error

            retry_after = None
            if status == 429 and getattr(error, "response", None) is not None:
                retry_after = error.response.headers.get("Retry-After")
            delay = self._retry_delay(attempt, retry_after)
            click.echo(f"Wallbox authentication retry in {delay:.1f}s")
            time.sleep(delay)

    def _ensure_session(self):
        """
        Ensure we are authenticated and have a charger_id.
        Called lazily by public methods so init failures don't kill the process.
        """
        now = time.time()
        if self._circuit_open_until > now:
            raise self.CircuitOpen(self._circuit_open_until - now)

        # Authenticate if token missing/expired
        self._authenticate()

        # Ensure we have a charger id
        if not self.charger_id:
            ids = self._call_with_reauth(self.client.getChargersList)
            if not ids:
                raise RuntimeError("No Wallbox chargers available after auth retry")
            self.charger_id = ids[0]
            click.echo(f"DEBUG: Available charger IDs: {ids}")

    def _call_with_reauth(self, func, *args, **kwargs):
        now = time.time()
        if self._circuit_open_until > now:
            raise self.CircuitOpen(self._circuit_open_until - now)

        forced_auth = False
        attempt = 0
        while True:
            try:
                result = func(*args, **kwargs)
                self._record_success()
                return result
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as caught:
                error = caught
                status = None
                retryable = True
            except requests.exceptions.HTTPError as caught:
                error = caught
                status = getattr(caught.response, "status_code", None)
                if status in (401, 403) and not forced_auth:
                    click.echo("Wallbox session unauthorized; forcing fresh authentication")
                    self._force_fresh_authentication()
                    forced_auth = True
                    continue
                retryable = status == 429 or status in (500, 502, 503, 504)

            if not retryable or attempt >= self._max_retries:
                self._record_failure()
                operation = getattr(func, "__name__", "API call")
                raise self.APIError(
                    f"Wallbox {operation} failed"
                    + (f" with HTTP {status}" if status else "")
                ) from error

            retry_after = None
            if status == 429 and getattr(error, "response", None) is not None:
                retry_after = error.response.headers.get("Retry-After")
            delay = self._retry_delay(attempt, retry_after)
            click.echo(
                f"Wallbox transient"
                + (f" HTTP {status}" if status else " network error")
                + f"; retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            attempt += 1

    def set_current(self, amps: int):
        """
        Set the maximum charging current (in amps) on the Pulsar Plus.
        """
        self._ensure_session()
        return self._call_with_reauth(self.client.setMaxChargingCurrent, self.charger_id, amps)

    def pause_charging(self):
        """Suspend the EVSE so a delayed vehicle-side start cannot draw power."""
        self._ensure_session()
        return self._call_with_reauth(self.client.pauseChargingSession, self.charger_id)

    def resume_charging(self):
        """Enable the EVSE immediately before requesting a vehicle-side start."""
        self._ensure_session()
        return self._call_with_reauth(self.client.resumeChargingSession, self.charger_id)

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


class DisabledTeslaSensor:
    """Energy sensor used when Tesla integration is explicitly disabled."""

    def get_house_power(self):
        return {
            "solar_power": 0.0,
            "house_load": 0.0,
            "battery_soc": None,
        }


def _command_charging_start(charger, bluelink, ev_status, desired_target):
    """Issue one coordinated EVSE/vehicle start request."""
    charger.resume_charging()
    ev_soc = ev_status.get("soc") or 0
    try:
        ac_soc = bluelink.get_ac_target_soc()
    except Exception:
        ac_soc = None

    bump_target = max(desired_target or ev_soc, ev_soc + 1)
    if (ac_soc is None) or (ac_soc < bump_target):
        try:
            bluelink.set_ac_target_soc(min(100, bump_target))
        except Exception as exc:
            click.echo(f"set_ac_target_soc failed: {exc}")

    bluelink.start_charge()


def _command_charging_stop(charger, bluelink, default_amps):
    """Fail closed by suspending both charging paths regardless of telemetry."""
    errors = []
    for operation in (
        charger.pause_charging,
        lambda: charger.set_current(default_amps),
        bluelink.stop_charge,
    ):
        try:
            operation()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


STOP_CHARGE_STATES = ("TARGET_REACHED", "WAIT_SOLAR", "WAIT_POWERWALL")


def _should_stop_charging(state, last_state, charging_evse, charging_bl):
    """Stop on entry to a safe state, or immediately if charging reappears."""
    return state in STOP_CHARGE_STATES and (
        state != last_state or charging_evse or charging_bl
    )


def _evse_power_watts(charger_status, cfg) -> float:
    """Estimate active EVSE power from the Wallbox current setting."""
    try:
        if charger_status.get("charging"):
            amps = float(charger_status.get("current") or 0.0)
            return amps * float(cfg.get("voltage", 240.0)) * float(cfg.get("pf", 1.0))
    except (AttributeError, TypeError, ValueError):
        pass
    return 0.0


def _build_tesla_sensor(cfg):
    if not cfg.get("tesla_enabled", True):
        return DisabledTeslaSensor()

    if cfg.get("tesla_data_source", "fleet").lower() == "local":
        return local_tesla_sensor_from_config(cfg)

    token_cache = Path(cfg.get("tesla_token_cache", ".tesla-tokens.json"))
    if not token_cache.is_absolute():
        token_cache = Path(cfg["_config_directory"]) / token_cache
    return TeslaSensor(
        refresh_token=str(cfg.get("tesla_refresh_token", "")),
        client_id=cfg["tesla_client_id"],
        api_base_url=cfg.get("tesla_api_base_url", DEFAULT_API_BASE_URL),
        token_url=cfg.get("tesla_token_url", DEFAULT_TOKEN_URL),
        token_cache=token_cache,
        site_id=cfg.get("tesla_site_id"),
        timeout=float(cfg.get("tesla_timeout", 20)),
    )

# ----- CLI -----
@click.group()
@click.option('--config', '-c', default='config.yaml', help='Path to config file')
@click.pass_context
def cli(ctx, config):
    with open(config) as f:
        cfg = yaml.safe_load(f)
    cfg["_config_directory"] = str(Path(config).resolve().parent)
    logging.basicConfig(level=cfg.get('log_level', 'INFO'))
    ctx.obj = cfg

@cli.command()
@click.pass_context
def start(ctx):
    cfg = ctx.obj

     # Initialize sensors and controllers
    tesla_enabled = cfg.get("tesla_enabled", True)
    tesla = _build_tesla_sensor(cfg)
    if not tesla_enabled:
        click.echo("Tesla integration disabled; solar charging and battery gating are unavailable")

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
        password=cfg['wallbox_pass'],
        request_timeout=cfg.get('wallbox_request_timeout_s', 10),
        jwt_token_drift=cfg.get('wallbox_jwt_drift_s', 120),
        max_retries=cfg.get('wallbox_max_retries', 2),
        retry_base_seconds=cfg.get('wallbox_retry_base_s', 1),
        circuit_breaker_failures=cfg.get('wallbox_circuit_breaker_failures', 3),
        circuit_breaker_seconds=cfg.get('wallbox_circuit_breaker_s', 60),
    )
    engine = DecisionEngine(cfg)
    solar_forecast = SolarForecast(cfg)
    interval = cfg.get('poll_interval', 300)
    last_state = None
    last_cmd_ts = 0.0
    last_startstop_ts = 0.0
    MIN_CMD_INTERVAL = cfg.get("min_cmd_interval_s", 20)
    MIN_STARTSTOP_INTERVAL = cfg.get("min_startstop_interval_s", 60)
    
    click.echo('Starting controller loop...')

    target_soc = cfg['ev_target_soc']

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
            evse_power_kw = round(_evse_power_watts(charger_status, cfg) / 1000.0, 2)
            click.echo({
                "solar_power": readings["solar_power"],
                "house_load": readings["house_load"],
                "battery_soc": readings.get("battery_soc"),
                "ev_soc": ev_status.get("soc"),
                "ev_charging": ev_status.get("charging"),
                "ev_charging_power_kW": evse_power_kw,
                "ev_charging_power_source": "wallbox_estimate",
                "bluelink_ev_charging_power_kW": ev_status.get("charging_power_kW"),
                "bluelink_data_stale": ev_status.get("data_stale", False),
                "bluelink_data_age_seconds": ev_status.get("data_age_seconds", 0),
            })
            click.echo(f"Charger status: {charger_status}")

            # --- start-of-tick reset to avoid sticky state/reason across iterations ---
            state = None
            amps_wanted = None
            reason = ""
            
            #plugged_evse = bool(charger_status.get("connected"))
            plugged_evse = charger._is_evse_plugged(charger_status.get("status_id"))
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
                batt_soc = readings.get("battery_soc")
                now = datetime.now()
                in_grid_window = _in_window(now, cfg["grid_charge_start_hour"], cfg["grid_charge_end_hour"])
                try:
                    overnight_plan = solar_forecast.get_overnight_plan(now)
                except Exception as exc:
                    overnight_plan = solar_forecast._fallback("error")
                    click.echo(f"Solar forecast unavailable: {exc}")

                overnight_target = overnight_plan.ev_target_soc
                powerwall_floor = overnight_plan.powerwall_floor_soc
                powerwall_stop_margin = float(cfg.get("grid_powerwall_stop_margin_soc", 2))
                powerwall_stop_at = powerwall_floor + powerwall_stop_margin
                desired_target = overnight_target if in_grid_window else cfg["ev_target_soc"]
                surplus_target = cfg.get("ev_target_soc_solar_surplus", desired_target)
                battery_ready = (not tesla_enabled) or (
                    batt_soc is not None
                    and batt_soc >= cfg['battery_soc_full_threshold_high']
                )
                if headroom > engine.hysteresis and battery_ready:
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
                # Safety: no solar surplus outside grid window → force WAIT_SOLAR immediately
                if headroom <= 0 and not in_grid_window:
                    state = "WAIT_SOLAR"; amps_wanted = None; reason = "negative headroom (safety)"
                click.echo({"now": now.strftime("%F %T"), 
                       "grid_window": [cfg["grid_charge_start_hour"], cfg["grid_charge_end_hour"]], 
                       "in_grid_window": in_grid_window,
                       "forecast": overnight_plan.classification,
                       "forecast_kwh": overnight_plan.forecast_kwh,
                       "forecast_date": overnight_plan.forecast_date,
                       "overnight_ev_target": overnight_target,
                       "powerwall_floor": powerwall_floor,
                       "forecast_source": overnight_plan.source})

                if headroom <= 0 and not in_grid_window:
                    state = "WAIT_SOLAR"; amps_wanted = None; reason = "negative headroom (safety)"

                target_reached = ev_charge_level >= desired_target

                # decide amps for grid window if applicable
                grid_amps = None
                if in_grid_window and ev_charge_level < overnight_target:
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
                                needed_kwh = max(0.0, (overnight_target - ev_charge_level) / 100.0 * cfg["ev_battery_capacity_kwh"])
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
                elif (
                    tesla_enabled
                    and in_grid_window
                    and batt_soc is not None
                    and batt_soc <= powerwall_stop_at
                ):
                    state = "WAIT_POWERWALL"; amps_wanted = None
                    reason = (
                        f"PW SOC {batt_soc:.1f}% reached {powerwall_floor:.1f}% "
                        f"floor guard"
                    )
                elif in_grid_window and grid_amps:
                    state = "CHARGING_GRID"; amps_wanted = grid_amps; reason = "grid window"
                # To avoid thrash, if the Powerwall has fallen past the low level, stop charging the EV
                if state == "CHARGING_SOLAR" and tesla_enabled:
                    if batt_soc is not None and batt_soc < cfg.get("battery_soc_full_threshold_low"):
                        state = "WAIT_SOLAR"

                # Reconcile EVSE amps without issuing start/stop commands here. Session
                # control is deliberately centralized below so one tick can only issue
                # one coordinated start or stop request.
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
                        amps_wanted = desired_a
                    else:
                        amps_wanted = cur_a

                elif state is None:
                    # Use EVSE-excluded house load for all surplus math
                    # Use PW SOC + effective headroom to allow solar charging
                    batt_soc = readings.get("battery_soc")
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

                        battery_ready = (not tesla_enabled) or (
                            batt_soc is not None and batt_soc >= batt_full_thr
                        )
                        if battery_ready and (headroom > getattr(engine, "hysteresis", 0)):
                            # set amps to soak up the actual surplus
                            amps_wanted = engine.compute_amps(readings_eff)[0]
                            state = "CHARGING_SOLAR"; reason = "solar surplus"
                        else:
                            state = "WAIT_SOLAR"; amps_wanted = None
                            if tesla_enabled and (batt_soc is None or batt_soc < batt_full_thr):
                                batt_label = "unknown" if batt_soc is None else f"{batt_soc:.1f}%"
                                reason = f"PW SOC {batt_label} < {batt_full_thr}%"
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


                # 4) Apply all EVSE/vehicle actions in one place.
                now_ts = time.time()
                # Safety: never keep CHARGING_GRID outside its window
                if state == "CHARGING_GRID" and not in_grid_window:
                    state = "WAIT_SOLAR"; amps_wanted = None; reason = "outside grid window (safety)"

                state_changed = state != last_state
                if state in ("CHARGING_GRID", "CHARGING_SOLAR"):
                    if amps_wanted is None:
                        # final belt & suspenders
                        amps_wanted = engine.compute_amps(readings)[0]
                    current_a = int(charger_status.get("current") or cfg.get("default_amps", 6))
                    if amps_wanted != current_a and amps_wanted != last_set_current:
                        charger.set_current(amps_wanted)
                        last_set_current = amps_wanted
                        last_cmd_ts = now_ts

                    should_start = (
                        not charging_actual
                        and (
                            state_changed
                            or (now_ts - last_startstop_ts) > MIN_STARTSTOP_INTERVAL
                        )
                    )
                    if should_start:
                        if plugged_evse:
                            _command_charging_start(
                                charger, bluelink, ev_status, desired_target
                            )
                            click.echo("Starting charging")
                            last_startstop_ts = now_ts
                        else:
                            click.echo("Skip start_charge: EVSE not connected")

                elif state in STOP_CHARGE_STATES:
                    # Act on entry even if both APIs currently say idle. This clears
                    # a previously accepted start that the vehicle may execute later.
                    should_stop = _should_stop_charging(
                        state, last_state, charging_evse, charging_bl
                    )
                    if should_stop:
                        _command_charging_stop(
                            charger, bluelink, cfg["default_amps"]
                        )
                        click.echo("Charging suspended")
                        last_startstop_ts = now_ts
                        last_cmd_ts = now_ts
                        last_set_current = cfg["default_amps"]

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
            if isinstance(e, WallboxCharger.CircuitOpen):
                backoff = min(max(1.0, e.retry_after), max_backoff)
            else:
                backoff = min(2 * consec_errors, max_backoff)
            # --- DIAGNOSTIC: show exact line and inputs causing the crash ---
            import traceback
            exc_type = type(e).__name__
            print(f"Tick error ({exc_type}): {e}. Backing off {backoff:.0f}s, then continuing.")
            # Expected Wallbox outages are already classified and do not need a
            # duplicated traceback. Preserve full diagnostics for other errors.
            if not isinstance(e, WallboxCharger.APIError):
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
