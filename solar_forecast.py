"""Solar-production forecasts and conservative overnight charging plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests


OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class OvernightPlan:
    classification: str
    forecast_kwh: float | None
    forecast_date: str | None
    ev_target_soc: int
    powerwall_floor_soc: float
    source: str


class SolarForecast:
    """Fetch Open-Meteo GTI data and turn it into an overnight policy.

    Results are cached to keep the weather service out of the five-minute control
    loop.  If fetching fails, a recent successful result can still be used;
    otherwise the configured conservative defaults are returned.
    """

    def __init__(self, config: dict[str, Any], session=None):
        self.cfg = config
        self.enabled = bool(config.get("solar_forecast_enabled", False))
        self.session = session or requests.Session()
        self._cached_plan: OvernightPlan | None = None
        self._cached_at: datetime | None = None

    def _fallback(self, source: str = "fallback") -> OvernightPlan:
        return OvernightPlan(
            classification="unavailable",
            forecast_kwh=None,
            forecast_date=None,
            ev_target_soc=int(
                self.cfg.get("forecast_ev_target_default", self.cfg["ev_target_soc"])
            ),
            powerwall_floor_soc=float(
                self.cfg.get("grid_powerwall_floor_soc", 50)
            ),
            source=source,
        )

    def _validate(self) -> None:
        required = ("latitude", "longitude", "panel_capacity_kw")
        missing = [name for name in required if self.cfg.get(name) is None]
        if missing:
            raise ValueError(
                "solar forecast requires config values: " + ", ".join(missing)
            )

    def _classify(self, forecast_kwh: float, forecast_date: str) -> OvernightPlan:
        poor_kwh = float(self.cfg.get("solar_forecast_poor_kwh", 15))
        good_kwh = float(self.cfg.get("solar_forecast_good_kwh", 35))
        if poor_kwh >= good_kwh:
            raise ValueError("solar_forecast_poor_kwh must be below solar_forecast_good_kwh")

        if forecast_kwh < poor_kwh:
            classification = "poor"
        elif forecast_kwh >= good_kwh:
            classification = "good"
        else:
            classification = "average"

        return OvernightPlan(
            classification=classification,
            forecast_kwh=forecast_kwh,
            forecast_date=forecast_date,
            ev_target_soc=int(
                self.cfg.get(
                    f"forecast_ev_target_{classification}",
                    self.cfg["ev_target_soc"],
                )
            ),
            powerwall_floor_soc=float(
                self.cfg.get(
                    f"forecast_powerwall_floor_{classification}",
                    self.cfg.get("grid_powerwall_floor_soc", 50),
                )
            ),
            source="open-meteo",
        )

    def _fetch(self, now: datetime) -> OvernightPlan:
        self._validate()
        response = self.session.get(
            self.cfg.get("solar_forecast_url", OPEN_METEO_FORECAST_URL),
            params={
                "latitude": float(self.cfg["latitude"]),
                "longitude": float(self.cfg["longitude"]),
                "hourly": "global_tilted_irradiance",
                "tilt": float(self.cfg.get("panel_tilt", 30)),
                "azimuth": float(self.cfg.get("panel_azimuth", 0)),
                "timezone": self.cfg.get("solar_forecast_timezone", "auto"),
                "forecast_days": 3,
            },
            timeout=float(self.cfg.get("solar_forecast_timeout", 10)),
        )
        response.raise_for_status()
        body = response.json()
        hourly = body.get("hourly") or {}
        times = hourly.get("time") or []
        irradiance = hourly.get("global_tilted_irradiance") or []
        if not times or len(times) != len(irradiance):
            raise ValueError("Open-Meteo response did not contain aligned hourly GTI data")

        # Before noon, the useful solar period is later today. After noon, plan
        # against tomorrow. This keeps a 00:00-06:00 grid window tied to the
        # daylight that follows it, while a late-evening window uses tomorrow.
        forecast_date = (
            now.date() if now.hour < 12 else (now + timedelta(days=1)).date()
        ).isoformat()
        gti_wh_m2 = sum(
            max(0.0, float(value or 0.0))
            for timestamp, value in zip(times, irradiance)
            if str(timestamp).startswith(forecast_date)
        )
        if not any(str(timestamp).startswith(forecast_date) for timestamp in times):
            raise ValueError(
                f"Open-Meteo response did not contain forecast date {forecast_date}"
            )

        capacity_kw = float(self.cfg["panel_capacity_kw"])
        performance_ratio = float(self.cfg.get("solar_forecast_performance_ratio", 0.80))
        forecast_kwh = gti_wh_m2 / 1000.0 * capacity_kw * performance_ratio
        return self._classify(round(forecast_kwh, 2), forecast_date)

    def get_overnight_plan(self, now: datetime | None = None) -> OvernightPlan:
        now = now or datetime.now()
        if not self.enabled:
            return self._fallback("disabled")

        refresh_after = timedelta(
            hours=float(self.cfg.get("solar_forecast_refresh_hours", 1))
        )
        if (
            self._cached_plan is not None
            and self._cached_at is not None
            and now - self._cached_at < refresh_after
        ):
            return self._cached_plan

        try:
            plan = self._fetch(now)
        except Exception:
            stale_after = timedelta(
                hours=float(self.cfg.get("solar_forecast_stale_hours", 6))
            )
            if (
                self._cached_plan is not None
                and self._cached_at is not None
                and now - self._cached_at < stale_after
            ):
                return OvernightPlan(
                    classification=self._cached_plan.classification,
                    forecast_kwh=self._cached_plan.forecast_kwh,
                    forecast_date=self._cached_plan.forecast_date,
                    ev_target_soc=self._cached_plan.ev_target_soc,
                    powerwall_floor_soc=self._cached_plan.powerwall_floor_soc,
                    source="stale-cache",
                )
            raise

        self._cached_plan = plan
        self._cached_at = now
        return plan
