import unittest
from unittest.mock import Mock

from tesla_local import TeslaLocalError, TeslaLocalSensor


def response(status, payload, url="https://gateway.test/api"):
    result = Mock()
    result.status_code = status
    result.url = url
    result.text = str(payload)
    result.json.return_value = payload
    return result


class TeslaLocalSensorTests(unittest.TestCase):
    def make_sensor(self, session):
        return TeslaLocalSensor(
            gateway_url="https://gateway.test",
            password="secret",
            email="owner@example.test",
            session=session,
        )

    def test_reads_local_energy_data(self):
        session = Mock()
        session.post.return_value = response(200, {})
        session.get.side_effect = [
            response(200, {"solar": {"instant_power": 6500}, "load": {"instant_power": 3100}}),
            response(200, {"percentage": 72.5}),
        ]

        self.assertEqual(
            self.make_sensor(session).get_house_power(),
            {"solar_power": 6500.0, "house_load": 3100.0, "battery_soc": 72.5},
        )
        session.post.assert_called_once_with(
            "https://gateway.test/api/login/Basic",
            json={"username": "customer", "password": "secret", "email": "owner@example.test"},
            timeout=10.0,
        )

    def test_reauthenticates_after_forbidden(self):
        session = Mock()
        session.post.side_effect = [response(200, {}), response(200, {})]
        session.get.side_effect = [
            response(403, {}),
            response(200, {"solar": {}, "load": {}}),
            response(200, {"percentage": 50}),
        ]

        self.make_sensor(session).get_house_power()

        self.assertEqual(session.post.call_count, 2)

    def test_reports_bad_credentials(self):
        session = Mock()
        session.post.return_value = response(401, {"error": "bad credentials"})

        with self.assertRaisesRegex(TeslaLocalError, "Gateway login failed"):
            self.make_sensor(session).get_house_power()


if __name__ == "__main__":
    unittest.main()
