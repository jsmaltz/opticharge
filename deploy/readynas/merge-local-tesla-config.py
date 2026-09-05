#!/usr/bin/env python3
"""Merge local-Gateway settings into an existing private OptiCharge config."""

import argparse
import os
from pathlib import Path

import yaml


TESLA_LOCAL_KEYS = (
    "tesla_enabled",
    "tesla_data_source",
    "tesla_gateway_url",
    "tesla_gateway_username",
    "tesla_gateway_password",
    "tesla_gateway_email",
    "tesla_gateway_timeout",
    "tesla_gateway_verify_tls",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    parser.add_argument("source")
    args = parser.parse_args()

    target_path = Path(args.target)
    source_path = Path(args.source)
    target = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
    source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
    for key in TESLA_LOCAL_KEYS:
        if key in source:
            target[key] = source[key]

    temporary = target_path.with_suffix(target_path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(target, sort_keys=False), encoding="utf-8")
    os.chmod(temporary, target_path.stat().st_mode)
    os.replace(temporary, target_path)


if __name__ == "__main__":
    main()
