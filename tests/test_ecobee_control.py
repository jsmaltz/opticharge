import unittest
from datetime import datetime
from unittest.mock import Mock

from ecobee_control import EcobeeController, HomeAssistantClient, decide_aux_heat


BASE_CONFIG = {
    "ecobee_gas_rate_per_therm": 2.0,
    "ecobee_heat_pump_cop": 3.0,
    "ecobee_furnace_efficiency": 0.95,
    "ecobee_cost_margin_fraction": 0.10,
    "ecobee_battery_limited_soc": 55,
    "ecobee_battery_hysteresis_soc": 5,
    "ecobee_solar_surplus_threshold_w": 500,
    "ecobee_solar_hysteresis_w": 500,
}


class DecisionTests(unittest.TestCase):
    def test_uses_gas_when_all_priority_conditions_are_true(self):
        decision = decide_aux_heat(
            BASE_CONFIG,
            electricity_rate=0.60,
            battery_soc=40,
            ev_soc=50,
            ev_target=80,
            solar_surplus_w=-1000,
            heat_call=True,
            current_aux_heat=False,
        )

        self.assertTrue(decision.desired_aux_heat)
        self.assertLess(
            decision.gas_heat_cost_per_kwh,
            decision.electric_heat_cost_per_kwh,
        )

    def test_prefers_heat_pump_when_ev_is_charged(self):
        decision = decide_aux_heat(
            BASE_CONFIG,
            electricity_rate=0.60,
            battery_soc=40,
            ev_soc=80,
            ev_target=80,
            solar_surplus_w=-1000,
            heat_call=True,
            current_aux_heat=False,
        )

        self.assertFalse(decision.desired_aux_heat)
        self.assertFalse(decision.ev_needs_charge)

    def test_does_not_switch_without_a_heat_call(self):
        decision = decide_aux_heat(
            BASE_CONFIG,
            electricity_rate=0.60,
            battery_soc=40,
            ev_soc=50,
            ev_target=80,
            solar_surplus_w=-1000,
            heat_call=False,
            current_aux_heat=False,
        )

        self.assertFalse(decision.desired_aux_heat)

    def test_existing_aux_mode_is_held_between_heat_calls(self):
        decision = decide_aux_heat(
            BASE_CONFIG,
            electricity_rate=0.60,
            battery_soc=58,
            ev_soc=50,
            ev_target=80,
            solar_surplus_w=750,
            heat_call=False,
            current_aux_heat=True,
        )

        self.assertTrue(decision.desired_aux_heat)

    def test_missing_rates_never_changes_mode(self):
        decision = decide_aux_heat(
            {},
            electricity_rate=None,
            battery_soc=40,
            ev_soc=50,
            ev_target=80,
            solar_surplus_w=-1000,
            heat_call=True,
            current_aux_heat=False,
        )

        self.assertIsNone(decision.desired_aux_heat)


class ClientTests(unittest.TestCase):
    def test_switch_service_uses_expected_home_assistant_endpoint(self):
        session = Mock()
        session.headers = {}
        response = Mock()
        response.json.return_value = []
        session.request.return_value = response
        client = HomeAssistantClient("http://ha:8123/", "secret", session=session)

        client.set_switch("switch.aux", True)

        session.request.assert_called_once_with(
            "POST",
            "http://ha:8123/api/services/switch/turn_on",
            timeout=10.0,
            json={"entity_id": "switch.aux"},
        )


class ControllerTests(unittest.TestCase):
    def test_ev2a_selects_winter_partial_peak_rate(self):
        cfg = {
            "ecobee_climate_entity_id": "climate.hallway",
            "ecobee_aux_heat_entity_id": "switch.aux",
            "ecobee_electric_rate_plan": "EV2-A",
            "ecobee_ev2a_winter_partial_peak_rate_per_kwh": 0.39428,
        }
        controller = EcobeeController(cfg, Mock())

        rate, period = controller._electricity_rate(
            False, datetime(2026, 11, 22, 21, 30)
        )

        self.assertEqual(rate, 0.39428)
        self.assertEqual(period, "winter_partial_peak")

    def test_ev2a_selects_summer_offpeak_rate(self):
        cfg = {
            "ecobee_climate_entity_id": "climate.hallway",
            "ecobee_aux_heat_entity_id": "switch.aux",
            "ecobee_electric_rate_plan": "EV2-A",
            "ecobee_ev2a_summer_offpeak_rate_per_kwh": 0.22558,
        }
        controller = EcobeeController(cfg, Mock())

        rate, period = controller._electricity_rate(
            False, datetime(2026, 7, 1, 14, 30)
        )

        self.assertEqual(rate, 0.22558)
        self.assertEqual(period, "summer_offpeak")

    def test_dry_run_reports_action_without_calling_service(self):
        client = Mock()
        client.get_state.side_effect = [
            {"attributes": {"hvac_action": "heating"}},
            {"state": "off", "last_changed": "2020-01-01T00:00:00+00:00"},
        ]
        cfg = dict(
            BASE_CONFIG,
            ecobee_climate_entity_id="climate.hallway",
            ecobee_aux_heat_entity_id="switch.aux",
            ecobee_dry_run=True,
            ecobee_electricity_peak_rate_per_kwh=0.60,
            ecobee_min_dwell_minutes=30,
        )
        controller = EcobeeController(cfg, client)

        result = controller.tick(
            readings={"solar_power": 0, "house_load": 1000, "battery_soc": 40},
            ev_status={"soc": 50},
            ev_target=80,
            in_grid_window=False,
        )

        self.assertEqual(result["action"], "turn_on")
        client.set_switch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
