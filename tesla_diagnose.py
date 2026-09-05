#!/usr/bin/env python3
"""Minimal, read-only Tesla Fleet API authorization diagnostic."""

import argparse
import base64
import json
import os
import time
from pathlib import Path

import certifi
import requests
import yaml


DEFAULT_API_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
DEFAULT_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"


def jwt_claims(token):
    """Decode JWT claims for diagnostics only; this does not verify the signature."""
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        return json.loads(base64.urlsafe_b64decode(encoded))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def token_path(config_path, config):
    path = Path(config.get("tesla_token_cache", ".tesla-tokens.json"))
    return path if path.is_absolute() else config_path.parent / path


def save_tokens(path, client_id, old_tokens, payload):
    updated = {
        "client_id": client_id,
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", old_tokens.get("refresh_token")),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
    return updated


def refresh_if_needed(session, config, path, tokens):
    claims = jwt_claims(tokens.get("access_token", ""))
    if claims.get("exp", 0) > time.time() + 60:
        return tokens, False

    refresh_token = tokens.get("refresh_token")
    client_id = config.get("tesla_client_id")
    if not refresh_token or not client_id:
        raise RuntimeError("Missing refresh token or tesla_client_id")

    response = session.post(
        config.get("tesla_token_url", DEFAULT_TOKEN_URL),
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        timeout=float(config.get("tesla_timeout", 20)),
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Token refresh failed: HTTP {response.status_code}, "
            f"x-txid={response.headers.get('x-txid', 'not supplied')}"
        )
    return save_tokens(path, client_id, tokens, response.json()), True


def get(session, base_url, path, access_token, timeout):
    response = session.get(
        base_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cache_path = token_path(config_path, config)
    tokens = json.loads(cache_path.read_text(encoding="utf-8"))
    client_id = config.get("tesla_client_id")
    if not client_id:
        raise SystemExit("FAIL: tesla_client_id is missing")

    session = requests.Session()
    session.verify = certifi.where()

    try:
        tokens, refreshed = refresh_if_needed(session, config, cache_path, tokens)
    except (OSError, KeyError, RuntimeError) as exc:
        raise SystemExit(f"FAIL: {exc}") from exc

    claims = jwt_claims(tokens.get("access_token", ""))
    scopes = claims.get("scp", [])
    if isinstance(scopes, str):
        scopes = scopes.split()

    print("Token checks")
    print(f"  refreshed: {refreshed}")
    print(f"  cache client matches config: {tokens.get('client_id') == client_id}")
    print(f"  authorized party matches config: {claims.get('azp') == client_id}")
    print(f"  account type: {claims.get('account_type', 'unknown')}")
    print(f"  energy_device_data scope: {'energy_device_data' in scopes}")
    print(f"  expires in seconds: {max(0, int(claims.get('exp', 0) - time.time()))}")

    base_url = config.get("tesla_api_base_url", DEFAULT_API_BASE_URL)
    timeout = float(config.get("tesla_timeout", 20))
    access_token = tokens["access_token"]

    region_response, region_payload = get(
        session, base_url, "/api/1/users/region", access_token, timeout
    )
    region = region_payload.get("response") if isinstance(region_payload, dict) else None
    if isinstance(region, dict):
        region = region.get("region")
    print("Region check")
    print(f"  HTTP status: {region_response.status_code}")
    print(f"  account region: {region or 'unknown'}")
    print(f"  x-txid: {region_response.headers.get('x-txid', 'not supplied')}")

    products_response, products_payload = get(
        session, base_url, "/api/1/products", access_token, timeout
    )
    products = products_payload.get("response") if isinstance(products_payload, dict) else None
    count = len(products) if isinstance(products, list) else None
    print("Products check")
    print(f"  URL: {products_response.url}")
    print(f"  HTTP status: {products_response.status_code}")
    print(f"  product count: {count if count is not None else 'unknown'}")
    print(f"  x-txid: {products_response.headers.get('x-txid', 'not supplied')}")

    if products_response.status_code != 200:
        raise SystemExit("RESULT: Fleet API rejected the products request")
    if count:
        print("RESULT: authorization works and Tesla returned products")
        return
    if claims.get("account_type") != "person":
        raise SystemExit("RESULT: empty list; this does not appear to be a person token")
    if "energy_device_data" not in scopes:
        raise SystemExit("RESULT: empty list; reauthorize with energy_device_data")
    if claims.get("azp") != client_id:
        raise SystemExit("RESULT: empty list; token belongs to a different Client ID")
    raise SystemExit(
        "RESULT: token type, Client ID, scope, and request are valid; "
        "the empty list is likely a Tesla-side product entitlement issue"
    )


if __name__ == "__main__":
    main()
