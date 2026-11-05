#!/usr/bin/env python3
"""Generate the deterministic M1 pilot sets from the group ID configuration."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.id_ranges import DEFAULT_CONFIG_PATH, load_config
from src.pilots import build_pilot_set, build_smoke_set, write_pilot_file

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = DEFAULT_CONFIG_PATH.parent


def main() -> int:
    members = list(load_config().values())
    smoke = build_smoke_set(members)
    pilot = build_pilot_set(members)

    smoke_path = CONFIG_DIR / "pilot_10.json"
    pilot_path = CONFIG_DIR / "pilot_500.json"
    write_pilot_file(smoke_path, smoke)
    write_pilot_file(pilot_path, pilot)

    print(f"Wrote {len(smoke):,} smoke IDs -> {smoke_path.relative_to(ROOT)}")
    print(f"Wrote {len(pilot):,} pilot IDs -> {pilot_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
