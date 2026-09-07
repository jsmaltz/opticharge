import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from opticharge import (
    DisabledTeslaSensor,
    BlueLinkSensor,
    WallboxCharger,
    _build_tesla_sensor,
    _command_charging_start,
    _command_charging_stop,
    _should_stop_charging,
)


class DisabledTeslaSensorTests(unittest.TestCase):
    def test_returns_unavailable_energy_readings(self):
        self.assertEqual(
            DisabledTeslaSensor().get_house_power(),
            {
                "solar_power": 0.0,
                "house_load": 0.0,
                "battery_soc": None,
            },
        )

    @patch("opticharge.TeslaSensor")
    def test_disabled_mode_does_not_construct_tesla_client(self, tesla_sensor):
        sensor = _build_tesla_sensor({"tesla_enabled": False})

        self.assertIsInstance(sensor, DisabledTeslaSensor)
        tesla_sensor.assert_not_called()


def bare_bluelink(reinit_failures=3):
    sensor = object.__new__(BlueLinkSensor)
    sensor.cfg = {}
    sensor.username = "user"
    sensor.password = "password"
    sensor.pin = "1234"
    sensor.vin = "VIN"
    sensor.region_id = 3
    sensor.brand_id = 2
    sensor.vehicle_id = "vehicle-id"
    sensor._status_failure_count = 0
    sensor._using_cached_status = False
    sensor._last_good_status = None
    sensor._last_good_status_at = 0.0
    sensor._reinit_failure_threshold = reinit_failures
    sensor.vm = Mock()
    sensor.vm.token = object()
    return sensor


class BlueLinkRefreshTests(unittest.TestCase):
    def test_api_call_checks_token_before_operation(self):
        sensor = bare_bluelink()
        operation = Mock(return_value="ok")

        self.assertEqual(sensor._call_with_reauth(operation), "ok")

        sensor.vm.check_and_refresh_token.assert_called_once_with()
        operation.assert_called_once_with()

    def test_ac_target_uses_existing_snapshot_without_second_refresh(self):
        sensor = bare_bluelink()
        vehicle = SimpleNamespace(
            data={
                "vehicleStatus": {
                    "evStatus": {
                        "reservChargeInfos": {
                            "targetSOClist": [
                                {"plugType": 0, "targetSOClevel": 80}
                            ]
                        }
                    }
                }
            },
            ev_charge_limits_ac=70,
        )
        sensor.vm.get_vehicle.return_value = vehicle

        self.assertEqual(sensor.get_ac_target_soc(), 80)

        sensor.vm.update_vehicle_with_cached_state.assert_not_called()
        sensor.vm.check_and_refresh_token.assert_not_called()

    @patch("opticharge.time.time", return_value=1300.0)
    def test_partial_payload_returns_last_good_status_without_reinit(self, now):
        sensor = bare_bluelink(reinit_failures=3)
        sensor._last_good_status = {"soc": 61, "charging": False}
        sensor._last_good_status_at = 1000.0
        sensor._refresh_status = Mock(side_effect=KeyError("vehicleStatus"))
        sensor._full_reinit_bluelink = Mock()

        status = sensor.get_vehicle_status()

        self.assertEqual(status["soc"], 61)
        self.assertTrue(status["data_stale"])
        self.assertEqual(status["data_age_seconds"], 300)
        sensor._full_reinit_bluelink.assert_not_called()

    def test_client_rebuild_waits_for_three_failed_polls(self):
        sensor = bare_bluelink(reinit_failures=3)
        sensor._last_good_status = {"soc": 61}
        sensor._last_good_status_at = 0.0
        fresh = {"soc": 62, "data_stale": False, "data_age_seconds": 0}
        sensor._refresh_status = Mock(
            side_effect=[
                KeyError("vehicleStatus"),
                KeyError("vehicleStatus"),
                KeyError("vehicleStatus"),
                fresh,
            ]
        )
        sensor._full_reinit_bluelink = Mock()

        self.assertTrue(sensor.get_vehicle_status()["data_stale"])
        self.assertTrue(sensor.get_vehicle_status()["data_stale"])
        self.assertEqual(sensor.get_vehicle_status()["soc"], 62)

        sensor._full_reinit_bluelink.assert_called_once_with()
        self.assertEqual(sensor._status_failure_count, 0)
        self.assertFalse(sensor._using_cached_status)


def http_error(status, retry_after=None):
    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return requests.HTTPError(response=response)


def bare_charger(max_retries=2, breaker_failures=3):
    charger = object.__new__(WallboxCharger)
    charger.client = Mock()
    charger.client.jwtToken = "rejected-token"
    charger.client.jwtRefreshToken = "refresh-token"
    charger.client.jwtTokenTtl = 9999999999999
    charger.client.jwtRefreshTokenTtl = 9999999999999
    charger.client.headers = {"Authorization": "Bearer rejected-token"}
    charger._consecutive_failures = 0
    charger._circuit_open_until = 0.0
    charger._max_retries = max_retries
    charger._retry_base_seconds = 0.0
    charger._circuit_breaker_failures = breaker_failures
    charger._circuit_breaker_seconds = 60.0
    return charger


class WallboxHardeningTests(unittest.TestCase):
    @patch("opticharge.Wallbox")
    def test_constructor_sets_timeout_and_proactive_token_drift(self, wallbox):
        client = wallbox.return_value
        client.getChargersList.return_value = [123]

        charger = WallboxCharger("user", "password")

        wallbox.assert_called_once_with(
            "user", "password", requestGetTimeout=10.0, jwtTokenDrift=120.0
        )
        self.assertEqual(charger.charger_id, 123)

    @patch("opticharge.time.sleep")
    def test_401_discards_rejected_token_and_performs_fresh_login(self, sleep):
        charger = bare_charger()
        operation = Mock(side_effect=[http_error(401), {"status_id": 194}])

        result = charger._call_with_reauth(operation)

        self.assertEqual(result, {"status_id": 194})
        self.assertEqual(operation.call_count, 2)
        charger.client.authenticate.assert_called_once_with()
        self.assertEqual(charger.client.jwtToken, "")
        self.assertEqual(charger.client.jwtRefreshToken, "")
        self.assertNotIn("Authorization", charger.client.headers)
        sleep.assert_not_called()

    @patch("opticharge.random.uniform", return_value=0.0)
    @patch("opticharge.time.sleep")
    def test_502_is_retried_with_bounded_backoff(self, sleep, uniform):
        charger = bare_charger(max_retries=2)
        operation = Mock(side_effect=[http_error(502), http_error(503), "ok"])

        self.assertEqual(charger._call_with_reauth(operation), "ok")

        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("opticharge.random.uniform", return_value=0.0)
    @patch("opticharge.time.sleep")
    def test_timeout_is_retried(self, sleep, uniform):
        charger = bare_charger(max_retries=1)
        operation = Mock(side_effect=[requests.Timeout("slow"), "ok"])

        self.assertEqual(charger._call_with_reauth(operation), "ok")

        self.assertEqual(operation.call_count, 2)
        sleep.assert_called_once_with(0.0)

    @patch("opticharge.time.sleep")
    def test_429_honors_retry_after(self, sleep):
        charger = bare_charger(max_retries=1)
        operation = Mock(side_effect=[http_error(429, retry_after=7), "ok"])

        self.assertEqual(charger._call_with_reauth(operation), "ok")

        sleep.assert_called_once_with(7.0)

    @patch("opticharge.time.time", return_value=1000.0)
    def test_circuit_opens_after_repeated_permanent_failures(self, now):
        charger = bare_charger(max_retries=0, breaker_failures=3)
        operation = Mock(side_effect=http_error(400))

        for _ in range(3):
            with self.assertRaises(WallboxCharger.APIError):
                charger._call_with_reauth(operation)

        with self.assertRaises(WallboxCharger.CircuitOpen):
            charger._call_with_reauth(operation)
        self.assertEqual(operation.call_count, 3)

    @patch("opticharge.time.time", return_value=1000.0)
    def test_open_circuit_blocks_authentication_attempt(self, now):
        charger = bare_charger()
        charger._circuit_open_until = 1030.0

        with self.assertRaises(WallboxCharger.CircuitOpen):
            charger._ensure_session()

        charger.client.authenticate.assert_not_called()

    def test_pause_and_resume_use_hardened_api_wrapper(self):
        charger = bare_charger()
        charger.charger_id = 123
        charger._ensure_session = Mock()
        charger._call_with_reauth = Mock(side_effect=["paused", "resumed"])

        self.assertEqual(charger.pause_charging(), "paused")
        self.assertEqual(charger.resume_charging(), "resumed")

        self.assertEqual(charger._ensure_session.call_count, 2)
        self.assertEqual(charger._call_with_reauth.call_count, 2)
        self.assertIs(
            charger._call_with_reauth.call_args_list[0].args[0],
            charger.client.pauseChargingSession,
        )
        self.assertIs(
            charger._call_with_reauth.call_args_list[1].args[0],
            charger.client.resumeChargingSession,
        )


class ChargingCommandTests(unittest.TestCase):
    def test_start_issues_one_coordinated_request(self):
        charger = Mock()
        bluelink = Mock()
        bluelink.get_ac_target_soc.return_value = 50

        _command_charging_start(charger, bluelink, {"soc": 59}, 60)

        charger.resume_charging.assert_called_once_with()
        bluelink.set_ac_target_soc.assert_called_once_with(60)
        bluelink.start_charge.assert_called_once_with()

    def test_stop_suspends_both_paths_even_when_telemetry_was_idle(self):
        charger = Mock()
        bluelink = Mock()

        self.assertTrue(
            _should_stop_charging("WAIT_POWERWALL", "CHARGING_GRID", False, False)
        )
        _command_charging_stop(charger, bluelink, 9)

        charger.pause_charging.assert_called_once_with()
        charger.set_current.assert_called_once_with(9)
        bluelink.stop_charge.assert_called_once_with()

    def test_unchanged_idle_stop_state_does_not_repeat_commands(self):
        self.assertFalse(
            _should_stop_charging("WAIT_POWERWALL", "WAIT_POWERWALL", False, False)
        )

    def test_charging_reappearing_in_stop_state_is_stopped_again(self):
        self.assertTrue(
            _should_stop_charging("WAIT_POWERWALL", "WAIT_POWERWALL", True, False)
        )

    def test_stop_attempts_vehicle_command_if_wallbox_pause_fails(self):
        charger = Mock()
        bluelink = Mock()
        charger.pause_charging.side_effect = RuntimeError("wallbox unavailable")

        with self.assertRaisesRegex(RuntimeError, "wallbox unavailable"):
            _command_charging_stop(charger, bluelink, 9)

        charger.set_current.assert_called_once_with(9)
        bluelink.stop_charge.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
