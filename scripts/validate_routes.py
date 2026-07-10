#!/usr/bin/env python3
"""Smoke-test get_drive_time against the known baseline routes from the
maps-mcp build spec. Run this after deployment (and after setting
GOOGLE_MAPS_API_KEY) to sanity-check that the API key/routing works and
that results are in the right ballpark.

Usage:
    GOOGLE_MAPS_API_KEY=... python scripts/validate_routes.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from maps_mcp.core import compute_drive_time  # noqa: E402

HOME_ADDRESS = "4054 W Boheny Dr, Bloomington, IN"

# (destination, expected duration) from the build spec's validation baselines.
BASELINES = [
    ("Elkhart, IN", "~3.5h"),
    ("Fort Wayne, IN", "~3h"),
    ("Jasper, IN", "~1h 15m"),
    ("Erlanger, KY", "~2h 15m"),
    ("Indianapolis, IN", "~1h"),
    ("Kid Angles Daycare, Bloomington, IN", "~12m"),
]


def main() -> int:
    exit_code = 0
    for destination, expected in BASELINES:
        try:
            result = compute_drive_time(HOME_ADDRESS, destination, "now")
        except Exception as exc:  # noqa: BLE001 - smoke test, want to see any failure
            print(f"[FAIL] {HOME_ADDRESS} -> {destination}: {exc}")
            exit_code = 1
            continue
        print(
            f"{HOME_ADDRESS} -> {destination}: got {result['duration']} "
            f"(expected {expected}), traffic_adjusted={result['traffic_adjusted']}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
