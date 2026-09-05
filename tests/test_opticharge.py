import unittest
from unittest.mock import patch

from opticharge import DisabledTeslaSensor, _build_tesla_sensor


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


if __name__ == "__main__":
    unittest.main()
