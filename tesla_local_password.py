#!/usr/bin/env python3
"""Set the Gateway's local customer password while connected to its TEG network."""

import argparse
from pathlib import Path

import requests
import urllib3
import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--gateway-url", default="https://192.168.91.1")
    parser.add_argument("--old-password", required=True)
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    new_password = str(config.get("tesla_gateway_password", ""))
    if not new_password:
        raise SystemExit("tesla_gateway_password is empty in the config")

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.post(
        args.gateway_url.rstrip("/") + "/api/password/change",
        json={
            "old_password": args.old_password,
            "new_password": new_password,
            "toggled_pw": True,
        },
        headers={
            "Origin": args.gateway_url.rstrip("/"),
            "Referer": args.gateway_url.rstrip("/") + "/",
        },
        verify=False,
        timeout=10,
    )
    if response.status_code != 200:
        raise SystemExit(f"Password change failed: HTTP {response.status_code}: {response.text[:300]}")
    print("Gateway local password changed successfully.")


if __name__ == "__main__":
    main()
