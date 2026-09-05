import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tesla_fleet import TeslaFleetError, TeslaSensor


def response(status, payload, url="https://example.test/api"):
    result = Mock()
    result.status_code = status
    result.reason = "OK" if status == 200 else "Error"
    result.url = url
    result.json.return_value = payload
    result.text = json.dumps(payload)
    return result


class TeslaSensorTests(unittest.TestCase):
    def make_sensor(self, session, **kwargs):
        return TeslaSensor(
            refresh_token="refresh-1",
            client_id="client-1",
            api_base_url="https://example.test",
            session=session,
            **kwargs,
        )

    def test_reads_fleet_energy_site(self):
        session = Mock()
        session.post.return_value = response(
            200, {"access_token": "access-1", "refresh_token": "refresh-2"}
        )
        session.get.side_effect = [
            response(200, {"response": [{"energy_site_id": 1234}]}),
            response(
                200,
                {
                    "response": {
                        "solar_power": 6500,
                        "load_power": 3100,
                        "percentage_charged": 72.5,
                    }
                },
            ),
        ]

        readings = self.make_sensor(session).get_house_power()

        self.assertEqual(
            readings,
            {"solar_power": 6500.0, "house_load": 3100.0, "battery_soc": 72.5},
        )
        self.assertEqual(session.get.call_args_list[0].args[0], "https://example.test/api/1/products")
        self.assertEqual(
            session.get.call_args_list[1].args[0],
            "https://example.test/api/1/energy_sites/1234/live_status",
        )

    def test_refreshes_once_after_401(self):
        session = Mock()
        session.post.side_effect = [
            response(200, {"access_token": "expired", "refresh_token": "refresh-2"}),
            response(200, {"access_token": "fresh", "refresh_token": "refresh-3"}),
        ]
        session.get.side_effect = [
            response(401, {"error": "expired"}),
            response(200, {"response": [{"energy_site_id": 1234}]}),
        ]

        sensor = self.make_sensor(session)
        self.assertEqual(sensor._get_site_id(), "1234")
        self.assertEqual(session.post.call_count, 2)

    def test_persists_rotated_refresh_token(self):
        session = Mock()
        session.post.return_value = response(
            200, {"access_token": "access-2", "refresh_token": "refresh-2"}
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "tokens.json"
            sensor = self.make_sensor(session, token_cache=cache)
            sensor._refresh_access_token()
            saved = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "refresh-2")

    def test_reports_wrong_region(self):
        session = Mock()
        session.post.return_value = response(200, {"access_token": "access-1"})
        session.get.return_value = response(421, {"error": "wrong region"})
        with self.assertRaisesRegex(TeslaFleetError, "different Fleet API region"):
            self.make_sensor(session)._get_products()


if __name__ == "__main__":
    unittest.main()
