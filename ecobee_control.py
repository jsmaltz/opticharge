"""Home Assistant-backed Ecobee fuel-selection control."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any

import requests


THERM_KWH = 29.3001


class HomeAssistantError(RuntimeError):
    """Raised when the Home Assistant API cannot be used safely."""


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status is not None else ""
            raise HomeAssistantError(f"Home Assistant request failed{suffix}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HomeAssistantError("Home Assistant returned invalid JSON") from exc
        if not isinstance(payload, (dict, list)):
            raise HomeAssistantError("Home Assistant returned an unexpected payload")
        return payload

    def get_state(self, entity_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/api/states/{entity_id}")
        if not isinstance(payload, dict):
            raise HomeAssistantError(f"Invalid state for {entity_id}")
        return payload

    def set_switch(self, entity_id: str, enabled: bool) -> None:
        action = "turn_on" if enabled else "turn_off"
        self._request(
            "POST",
            f"/api/services/switch/{action}",
            json={"entity_id": entity_id},
        )


@dataclass(frozen=True)
class HeatingDecision:
    desired_aux_heat: bool | None
    reason: str
    electric_heat_cost_per_kwh: float | None = None
    gas_heat_cost_per_kwh: float | None = None
    electricity_rate_per_kwh: float | None = None
    heat_call: bool = False
    battery_limited: bool = False
    ev_needs_charge: bool = False
    solar_scarce: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_aux_heat(
    cfg: dict[str, Any],
    *,
    electricity_rate: float | None,
    battery_soc: float | None,
    ev_soc: float | None,
    ev_target: float,
    solar_surplus_w: float,
    heat_call: bool,
    current_aux_heat: bool,
) -> HeatingDecision:
    gas_rate = cfg.get("ecobee_gas_rate_per_therm")
    if electricity_rate is None or gas_rate is None:
        return HeatingDecision(None, "energy rates are not configured", heat_call=heat_call)

    try:
        electricity_rate = float(electricity_rate)
        gas_rate = float(gas_rate)
        cop = float(cfg.get("ecobee_heat_pump_cop", 3.0))
        furnace_efficiency = float(cfg.get("ecobee_furnace_efficiency", 0.95))
    except (TypeError, ValueError):
        return HeatingDecision(None, "invalid energy cost configuration", heat_call=heat_call)
    if min(electricity_rate, gas_rate, cop, furnace_efficiency) <= 0:
        return HeatingDecision(None, "invalid energy cost configuration", heat_call=heat_call)

    electric_cost = electricity_rate / cop
    gas_cost = gas_rate / (THERM_KWH * furnace_efficiency)
    margin = max(0.0, float(cfg.get("ecobee_cost_margin_fraction", 0.10)))
    gas_is_cheaper = electric_cost > gas_cost * (1.0 + margin)

    battery_limit = float(cfg.get("ecobee_battery_limited_soc", 55.0))
    battery_hysteresis = max(
        0.0, float(cfg.get("ecobee_battery_hysteresis_soc", 5.0))
    )
    effective_battery_limit = battery_limit + (
        battery_hysteresis if current_aux_heat else 0.0
    )
    battery_limited = battery_soc is not None and float(battery_soc) <= effective_battery_limit

    ev_needs_charge = ev_soc is not None and float(ev_soc) < float(ev_target)
    solar_limit = float(cfg.get("ecobee_solar_surplus_threshold_w", 500.0))
    solar_hysteresis = max(
        0.0, float(cfg.get("ecobee_solar_hysteresis_w", 500.0))
    )
    effective_solar_limit = solar_limit + (solar_hysteresis if current_aux_heat else 0.0)
    solar_scarce = float(solar_surplus_w) <= effective_solar_limit

    # Once gas-only mode is active, keep it selected between heat calls while
    # the economic and energy-priority conditions remain true. This avoids
    # changing HVAC modes at the end of every individual furnace cycle.
    heating_relevant = heat_call or current_aux_heat
    use_aux = all(
        (
            gas_is_cheaper,
            battery_limited,
            ev_needs_charge,
            solar_scarce,
            heating_relevant,
        )
    )
    facts = {
        "gas cheaper": gas_is_cheaper,
        "battery limited": battery_limited,
        "EV below target": ev_needs_charge,
        "solar scarce": solar_scarce,
        "heat called": heating_relevant,
    }
    reason = ", ".join(name for name, value in facts.items() if not value)
    if use_aux:
        reason = "gas heat is cheaper and electricity is reserved for battery/EV"
    elif not reason:
        reason = "heat-pump mode preferred"
    return HeatingDecision(
        use_aux,
        reason,
        electric_heat_cost_per_kwh=round(electric_cost, 4),
        gas_heat_cost_per_kwh=round(gas_cost, 4),
        electricity_rate_per_kwh=round(electricity_rate, 5),
        heat_call=heat_call,
        battery_limited=battery_limited,
        ev_needs_charge=ev_needs_charge,
        solar_scarce=solar_scarce,
    )


class EcobeeController:
    def __init__(self, cfg: dict[str, Any], client: HomeAssistantClient) -> None:
        self.cfg = cfg
        self.client = client
        self.climate_entity = str(cfg["ecobee_climate_entity_id"])
        self.aux_entity = str(cfg["ecobee_aux_heat_entity_id"])
        self.dry_run = bool(cfg.get("ecobee_dry_run", True))
        self.min_dwell_s = max(0.0, float(cfg.get("ecobee_min_dwell_minutes", 30))) * 60.0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "EcobeeController":
        token_path = Path(cfg.get("home_assistant_token_file", ".home-assistant-token"))
        if not token_path.is_absolute():
            token_path = Path(cfg["_config_directory"]) / token_path
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise HomeAssistantError(f"Home Assistant token file is empty: {token_path}")
        client = HomeAssistantClient(
            str(cfg["home_assistant_url"]),
            token,
            timeout=float(cfg.get("home_assistant_timeout_s", 10)),
        )
        return cls(cfg, client)

    @staticmethod
    def _seconds_since(last_changed: str | None) -> float:
        if not last_changed:
            return float("inf")
        try:
            changed = datetime.fromisoformat(last_changed.replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - changed).total_seconds())
        except (TypeError, ValueError):
            return float("inf")

    def _electricity_rate(
        self, in_grid_window: bool, now: datetime | None = None
    ) -> tuple[float | None, str]:
        now = now or datetime.now().astimezone()
        if str(self.cfg.get("ecobee_electric_rate_plan", "")).upper() in {
            "EV2-A",
            "EV2A",
        }:
            hour = now.hour
            if 16 <= hour < 21:
                period = "peak"
            elif 15 <= hour < 16 or 21 <= hour < 24:
                period = "partial_peak"
            else:
                period = "offpeak"
            season = "summer" if 6 <= now.month <= 9 else "winter"
            value = self.cfg.get(f"ecobee_ev2a_{season}_{period}_rate_per_kwh")
            return value, f"{season}_{period}"
        key = (
            "ecobee_electricity_offpeak_rate_per_kwh"
            if in_grid_window
            else "ecobee_electricity_peak_rate_per_kwh"
        )
        period = "offpeak" if in_grid_window else "peak"
        return (
            self.cfg.get(key, self.cfg.get("ecobee_electricity_rate_per_kwh")),
            period,
        )

    def tick(
        self,
        *,
        readings: dict[str, Any],
        ev_status: dict[str, Any],
        ev_target: float,
        in_grid_window: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        climate = self.client.get_state(self.climate_entity)
        aux = self.client.get_state(self.aux_entity)
        heat_call = climate.get("attributes", {}).get("hvac_action") == "heating"
        current_aux = aux.get("state") == "on"
        solar_surplus = float(readings.get("solar_power") or 0.0) - float(
            readings.get("house_load") or 0.0
        )
        electricity_rate, rate_period = self._electricity_rate(in_grid_window, now)
        decision = decide_aux_heat(
            self.cfg,
            electricity_rate=electricity_rate,
            battery_soc=readings.get("battery_soc"),
            ev_soc=ev_status.get("soc"),
            ev_target=ev_target,
            solar_surplus_w=solar_surplus,
            heat_call=heat_call,
            current_aux_heat=current_aux,
        )
        result = decision.as_dict()
        result.update(
            {
                "current_aux_heat": current_aux,
                "dry_run": self.dry_run,
                "action": "none",
                "electric_rate_period": rate_period,
            }
        )
        desired = decision.desired_aux_heat
        if desired is None or desired == current_aux:
            return result
        seconds_since_change = self._seconds_since(aux.get("last_changed"))
        if seconds_since_change < self.min_dwell_s:
            result["reason"] += "; minimum dwell time active"
            return result
        result["action"] = "turn_on" if desired else "turn_off"
        if not self.dry_run:
            self.client.set_switch(self.aux_entity, desired)
        return result
