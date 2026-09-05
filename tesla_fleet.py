"""Tesla Fleet API client for Powerwall/solar readings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import certifi
import requests


DEFAULT_API_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
DEFAULT_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"


class TeslaFleetError(RuntimeError):
    """A Fleet API response was valid HTTP but unusable by OptiCharge."""


class TeslaSensor:
    def __init__(
        self,
        refresh_token: str,
        client_id: str,
        api_base_url: str = DEFAULT_API_BASE_URL,
        token_url: str = DEFAULT_TOKEN_URL,
        token_cache: str | os.PathLike[str] | None = None,
        site_id: int | str | None = None,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ):
        if not client_id or client_id == "ownerapi":
            raise ValueError("tesla_client_id must be a Tesla Fleet API application client ID")

        self.client_id = client_id
        self.api_base_url = api_base_url.rstrip("/")
        self.token_url = token_url
        self.token_cache = Path(token_cache) if token_cache else None
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.verify = certifi.where()

        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self._site_id = str(site_id) if site_id is not None else None
        self._products_json: Any = None
        self._load_token_cache()

    def _load_token_cache(self) -> None:
        if not self.token_cache or not self.token_cache.exists():
            return
        try:
            cached = json.loads(self.token_cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TeslaFleetError(f"Could not read Tesla token cache {self.token_cache}: {exc}") from exc
        if cached.get("client_id") not in (None, self.client_id):
            raise TeslaFleetError("Tesla token cache belongs to a different client_id")
        self.refresh_token = cached.get("refresh_token") or self.refresh_token
        self.access_token = cached.get("access_token") or None

    def _save_tokens(self, payload: dict[str, Any]) -> None:
        if not self.token_cache:
            return
        data = {
            "client_id": self.client_id,
            "access_token": payload.get("access_token", self.access_token),
            "refresh_token": payload.get("refresh_token", self.refresh_token),
        }
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_cache.with_suffix(self.token_cache.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self.token_cache)

    @staticmethod
    def _response_message(response: requests.Response) -> str:
        body = response.text[:500]
        return f"{response.status_code} {response.reason} from {response.url}: {body}"

    def _refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise TeslaFleetError("No Fleet API refresh token; run tesla_auth.py authorize")
        response = self.session.post(
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise TeslaFleetError(f"Tesla token refresh failed: {self._response_message(response)}")
        payload = response.json()
        try:
            self.access_token = payload["access_token"]
        except KeyError as exc:
            raise TeslaFleetError("Tesla token response did not contain access_token") from exc
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        self._save_tokens(payload)

    def _get(self, path: str) -> dict[str, Any]:
        if not self.access_token:
            self._refresh_access_token()
        url = f"{self.api_base_url}{path}"
        response = self.session.get(
            url,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=self.timeout,
        )
        if response.status_code == 401:
            self._refresh_access_token()
            response = self.session.get(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=self.timeout,
            )
        if response.status_code == 421:
            raise TeslaFleetError(
                "Tesla account is in a different Fleet API region; change tesla_api_base_url. "
                + self._response_message(response)
            )
        if response.status_code != 200:
            raise TeslaFleetError(self._response_message(response))
        payload = response.json()
        if not isinstance(payload, dict):
            raise TeslaFleetError(f"Tesla returned a {type(payload).__name__}, expected an object")
        return payload

    def _get_products(self) -> Any:
        if self._products_json is None:
            payload = self._get("/api/1/products")
            if "response" not in payload:
                raise TeslaFleetError("Tesla products response is missing 'response'")
            self._products_json = payload["response"]
        return self._products_json

    def _get_site_id(self) -> str:
        if self._site_id is not None:
            return self._site_id
        products = self._get_products()
        candidates = products.get("energy_sites", []) if isinstance(products, dict) else products
        if not isinstance(candidates, list):
            candidates = []
        for product in candidates:
            if not isinstance(product, dict):
                continue
            site_id = product.get("energy_site_id") or product.get("id")
            if site_id is not None:
                self._site_id = str(site_id)
                print(f"TESLA site_id resolved: {self._site_id}")
                return self._site_id
        raise TeslaFleetError("Could not find an energy_site_id in Tesla products")

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def get_house_power(self) -> dict[str, float]:
        site_id = self._get_site_id()
        payload = self._get(f"/api/1/energy_sites/{site_id}/live_status")
        live = payload.get("response", payload)
        if isinstance(live, str):
            try:
                live = json.loads(live)
            except json.JSONDecodeError:
                live = {}
        if not isinstance(live, dict):
            raise TeslaFleetError("Tesla live_status response is not an object")

        solar = self._number(live.get("solar_power"))
        house = self._number(live.get("load_power"))
        battery = self._number(live.get("percentage_charged"))
        if not battery:
            info_payload = self._get(f"/api/1/energy_sites/{site_id}/site_info")
            info = info_payload.get("response", info_payload)
            if isinstance(info, dict):
                battery_info = info.get("battery") or {}
                battery = self._number(
                    (battery_info.get("percent") if isinstance(battery_info, dict) else None)
                    or info.get("battery_level")
                    or info.get("percentage_charged")
                )

        print(f"TESLA parsed: solar={solar:.1f}W, load={house:.1f}W, batt={battery:.1f}%")
        return {"solar_power": solar, "house_load": house, "battery_soc": battery}
