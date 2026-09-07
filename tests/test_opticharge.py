import unittest
from unittest.mock import Mock, patch

import requests

from opticharge import DisabledTeslaSensor, WallboxCharger, _build_tesla_sensor


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


if __name__ == "__main__":
    unittest.main()
