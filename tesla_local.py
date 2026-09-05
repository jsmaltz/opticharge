"""Read-only client for a Tesla Powerwall Gateway on the local network."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import requests
import urllib3
import yaml


class TeslaLocalError(RuntimeError):
    """The local Gateway could not provide usable energy readings."""


class TeslaLocalSensor:
    def __init__(
        self,
        gateway_url: str,
        password: str,
        email: str = "",
        username: str = "customer",
        timeout: float = 10.0,
        verify_tls: bool = False,
        session: requests.Session | None = None,
    ):
        if not gateway_url:
            raise ValueError("tesla_gateway_url is required")
        if not password:
            raise ValueError("tesla_gateway_password is required")

        self.gateway_url = gateway_url.rstrip("/")
        self.password = password
        self.email = email
        self.username = username
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.session = session or requests.Session()
        self.session.verify = verify_tls
        self._authenticated = False
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @staticmethod
    def _response_message(response: requests.Response) -> str:
        return f"HTTP {response.status_code} from {response.url}: {response.text[:300]}"

    def _login(self) -> None:
        response = self.session.post(
            f"{self.gateway_url}/api/login/Basic",
            json={
                "username": self.username,
                "password": self.password,
                "email": self.email,
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise TeslaLocalError(f"Gateway login failed: {self._response_message(response)}")
        self._authenticated = True

    def _get(self, path: str) -> Any:
        if not self._authenticated:
            self._login()
        response = self.session.get(f"{self.gateway_url}{path}", timeout=self.timeout)
        if response.status_code in (401, 403):
            self._authenticated = False
            self._login()
            response = self.session.get(f"{self.gateway_url}{path}", timeout=self.timeout)
        if response.status_code != 200:
            raise TeslaLocalError(self._response_message(response))
        try:
            return response.json()
        except ValueError as exc:
            raise TeslaLocalError(f"Gateway returned invalid JSON for {path}") from exc

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def get_house_power(self) -> dict[str, float]:
        meters = self._get("/api/meters/aggregates")
        soe = self._get("/api/system_status/soe")
        if not isinstance(meters, dict) or not isinstance(soe, dict):
            raise TeslaLocalError("Gateway energy response had an unexpected format")

        solar = self._number((meters.get("solar") or {}).get("instant_power"))
        house = self._number((meters.get("load") or {}).get("instant_power"))
        battery = self._number(soe.get("percentage"))
        print(f"TESLA local: solar={solar:.1f}W, load={house:.1f}W, batt={battery:.1f}%")
        return {"solar_power": solar, "house_load": house, "battery_soc": battery}


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def sensor_from_config(config: dict[str, Any]) -> TeslaLocalSensor:
    return TeslaLocalSensor(
        gateway_url=str(config.get("tesla_gateway_url", "")),
        password=str(config.get("tesla_gateway_password", "")),
        email=str(config.get("tesla_gateway_email", "")),
        username=str(config.get("tesla_gateway_username", "customer")),
        timeout=float(config.get("tesla_gateway_timeout", 10)),
        verify_tls=_bool(config.get("tesla_gateway_verify_tls"), False),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Test read-only local Tesla Gateway access")
    parser.add_argument("-c", "--config", default="config.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    print(json.dumps(sensor_from_config(config).get_house_power(), indent=2))


if __name__ == "__main__":
    main()
