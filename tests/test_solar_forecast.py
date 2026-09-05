import unittest
from datetime import datetime, timedelta

import requests

from solar_forecast import SolarForecast


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return FakeResponse(self.body)


def config(**overrides):
    values = {
        "solar_forecast_enabled": True,
        "latitude": 40.0,
        "longitude": -120.0,
        "panel_capacity_kw": 10.0,
        "panel_tilt": 25,
        "panel_azimuth": 0,
        "solar_forecast_performance_ratio": 0.8,
        "solar_forecast_poor_kwh": 15,
        "solar_forecast_good_kwh": 35,
        "grid_powerwall_floor_soc": 50,
        "forecast_powerwall_floor_poor": 65,
        "forecast_powerwall_floor_average": 50,
        "forecast_powerwall_floor_good": 50,
        "forecast_ev_target_poor": 80,
        "forecast_ev_target_average": 70,
        "forecast_ev_target_good": 60,
        "ev_target_soc": 80,
    }
    values.update(overrides)
    return values


def hourly_body(day, irradiance):
    return {
        "hourly": {
            "time": [f"{day}T{hour:02d}:00" for hour in range(24)],
            "global_tilted_irradiance": [irradiance] * 24,
        }
    }


class SolarForecastTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 5, 22, 0)
        self.tomorrow = "2026-09-06"

    def test_good_forecast_selects_lower_ev_target_but_keeps_floor(self):
        # 24h * 200 W/m2 * 10 kW * 0.8 / 1000 = 38.4 kWh
        session = FakeSession(hourly_body(self.tomorrow, 200))
        plan = SolarForecast(config(), session).get_overnight_plan(self.now)

        self.assertEqual(plan.classification, "good")
        self.assertEqual(plan.forecast_kwh, 38.4)
        self.assertEqual(plan.forecast_date, self.tomorrow)
        self.assertEqual(plan.ev_target_soc, 60)
        self.assertEqual(plan.powerwall_floor_soc, 50)

    def test_poor_forecast_raises_floor_and_keeps_full_grid_target(self):
        session = FakeSession(hourly_body(self.tomorrow, 50))
        plan = SolarForecast(config(), session).get_overnight_plan(self.now)

        self.assertEqual(plan.classification, "poor")
        self.assertEqual(plan.ev_target_soc, 80)
        self.assertEqual(plan.powerwall_floor_soc, 65)

    def test_cache_avoids_repeated_requests(self):
        session = FakeSession(hourly_body(self.tomorrow, 150))
        forecast = SolarForecast(config(), session)
        first = forecast.get_overnight_plan(self.now)
        second = forecast.get_overnight_plan(self.now + timedelta(minutes=30))

        self.assertEqual(first, second)
        self.assertEqual(session.calls, 1)

    def test_recent_cache_is_used_during_transient_failure(self):
        session = FakeSession(hourly_body(self.tomorrow, 150))
        forecast = SolarForecast(config(solar_forecast_refresh_hours=1), session)
        forecast.get_overnight_plan(self.now)
        session.error = requests.ConnectionError("offline")

        plan = forecast.get_overnight_plan(self.now + timedelta(hours=2))

        self.assertEqual(plan.source, "stale-cache")
        self.assertEqual(plan.classification, "average")

    def test_disabled_forecast_returns_safe_defaults_without_network(self):
        session = FakeSession(error=AssertionError("network should not be used"))
        plan = SolarForecast(
            config(solar_forecast_enabled=False), session
        ).get_overnight_plan(self.now)

        self.assertEqual(plan.source, "disabled")
        self.assertEqual(plan.ev_target_soc, 80)
        self.assertEqual(plan.powerwall_floor_soc, 50)
        self.assertEqual(session.calls, 0)

    def test_after_midnight_uses_solar_forecast_for_same_day(self):
        after_midnight = datetime(2026, 9, 6, 1, 0)
        session = FakeSession(hourly_body("2026-09-06", 200))

        plan = SolarForecast(config(), session).get_overnight_plan(after_midnight)

        self.assertEqual(plan.forecast_date, "2026-09-06")


if __name__ == "__main__":
    unittest.main()
