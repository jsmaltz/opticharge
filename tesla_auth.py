"""One-time Tesla Fleet API authorization and connectivity test."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import certifi
import requests
import yaml

from tesla_fleet import DEFAULT_API_BASE_URL, DEFAULT_TOKEN_URL, TeslaSensor


AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
SCOPES = "openid offline_access energy_device_data"


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    config["_directory"] = str(path.resolve().parent)
    return config


def cache_path(config: dict) -> Path:
    configured = Path(config.get("tesla_token_cache", ".tesla-tokens.json"))
    if not configured.is_absolute():
        configured = Path(config["_directory"]) / configured
    return configured


def require(config: dict, name: str) -> str:
    value = config.get(name)
    if not value:
        raise SystemExit(f"Set {name} in the config file first")
    return str(value)


def save_tokens(path: Path, client_id: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "client_id": client_id,
                "access_token": payload["access_token"],
                "refresh_token": payload["refresh_token"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    temporary.replace(path)


def receive_callback(redirect_uri: str, state: str) -> str:
    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    is_local_http = (
        parsed_redirect.scheme == "http"
        and parsed_redirect.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if not is_local_http:
        callback = input("\nPaste the complete redirected URL here: ").strip()
        values = urllib.parse.parse_qs(urllib.parse.urlparse(callback).query)
    else:
        result: dict[str, str] = {}

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                request = urllib.parse.urlparse(self.path)
                if request.path != parsed_redirect.path:
                    self.send_error(404)
                    return
                values = urllib.parse.parse_qs(request.query)
                result["state"] = values.get("state", [""])[0]
                result["code"] = values.get("code", [""])[0]
                result["error"] = values.get("error", [""])[0]
                body = b"Tesla authorization received. You can close this tab."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:
                pass

        port = parsed_redirect.port or 80
        server = http.server.HTTPServer((parsed_redirect.hostname, port), CallbackHandler)
        server.timeout = 900
        print(f"Waiting up to 15 minutes for Tesla to return to {redirect_uri}")
        server.handle_request()
        server.server_close()
        if result.get("error"):
            raise SystemExit(f"Tesla authorization failed: {result['error']}")
        values = {"state": [result.get("state")], "code": [result.get("code")]}

    if values.get("state", [None])[0] != state:
        raise SystemExit("OAuth state did not match; authorization aborted")
    code = values.get("code", [None])[0]
    if not code:
        raise SystemExit("Callback did not contain an authorization code")
    return code


def authorize(config: dict) -> None:
    client_id = require(config, "tesla_client_id")
    client_secret = require(config, "tesla_client_secret")
    redirect_uri = require(config, "tesla_redirect_uri")
    audience = config.get("tesla_api_base_url", DEFAULT_API_BASE_URL)
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "prompt": "login",
            "prompt_missing_scopes": "true",
            "require_requested_scopes": "true",
        }
    )
    url = f"{AUTH_URL}?{query}"
    print("Open this URL and approve access:\n")
    print(url)
    webbrowser.open(url)
    code = receive_callback(redirect_uri, state)

    response = requests.post(
        config.get("tesla_token_url", DEFAULT_TOKEN_URL),
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "audience": audience,
            "redirect_uri": redirect_uri,
        },
        timeout=float(config.get("tesla_timeout", 20)),
        verify=certifi.where(),
    )
    if response.status_code != 200:
        raise SystemExit(f"Token exchange failed ({response.status_code}): {response.text[:500]}")
    payload = response.json()
    save_tokens(cache_path(config), client_id, payload)
    print(f"Saved rotating tokens to {cache_path(config)}")


def test_connection(config: dict) -> None:
    sensor = TeslaSensor(
        refresh_token=str(config.get("tesla_refresh_token", "")),
        client_id=require(config, "tesla_client_id"),
        api_base_url=config.get("tesla_api_base_url", DEFAULT_API_BASE_URL),
        token_url=config.get("tesla_token_url", DEFAULT_TOKEN_URL),
        token_cache=cache_path(config),
        site_id=config.get("tesla_site_id"),
        timeout=float(config.get("tesla_timeout", 20)),
    )
    print(json.dumps(sensor.get_house_power(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("authorize", "test"))
    parser.add_argument("-c", "--config", default="config.yaml")
    args = parser.parse_args()
    config = load_config(Path(args.config))
    if args.command == "authorize":
        authorize(config)
    else:
        test_connection(config)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
