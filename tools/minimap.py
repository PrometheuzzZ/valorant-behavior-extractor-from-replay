#!/usr/bin/env python3
"""Minimap projection of world positions, and a check that it fits an export.

The constants are valorant-api.com's `/v1/maps` (`xMultiplier`, `yMultiplier`,
`xScalarToAdd`, `yScalarToAdd`), supplied by the user and keyed by `mapUrl`,
which is the manifest's `level_names_and_times[0].name`. The axes cross:
`pos_y` drives u and `pos_x` drives v (docs/DATA.md, "Minimap projection").
Hidden actors sit in a park slot, filtered on both x and z: a falling player
also reaches z = -50000.

The command prints the share of live movement rows inside [0,1]^2 and the u/v
range, and exits 1 for a map without constants or a share below `MIN_INSIDE`.
Only the range shows a too-small scale, which containment passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

#: Parked rows: x -50,879..-49,091 and z -49,920..-49,785 (21,301 rows, 4 exports).
PARK_X, PARK_Z, PARK_RADIUS = -50000.0, -49900.0, 2000.0
#: Eleven of twelve maps put 100% of live rows inside, Abyss 99.68% (falls);
#: the uncrossed axes put 0.9% (Haven) and 3.1% (Fracture).
MIN_INSIDE = 0.99


def load_constants(maps_json: Path, map_url: str) -> dict:
    """The map's four constants; KeyError for a map the file lacks or leaves at 0."""
    document = json.loads(maps_json.read_text(encoding="utf-8"))
    for entry in document["data"] if isinstance(document, dict) else document:
        if entry.get("mapUrl") == map_url and entry.get("xMultiplier") and entry.get("yMultiplier"):
            return {key: float(entry[key])
                    for key in ("xMultiplier", "yMultiplier", "xScalarToAdd", "yScalarToAdd")}
    raise KeyError(f"no minimap constants for {map_url} in {maps_json}")


def project(pos_x, pos_y, constants: dict):
    """(u, v) in minimap units: u from pos_y, v from pos_x."""
    return (pos_y * constants["xMultiplier"] + constants["xScalarToAdd"],
            pos_x * constants["yMultiplier"] + constants["yScalarToAdd"])


def parked(pos_x, pos_z):
    """True where a row sits in the park slot, judged on both x and z."""
    return (abs(pos_x - PARK_X) <= PARK_RADIUS) & (abs(pos_z - PARK_Z) <= PARK_RADIUS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", type=Path, required=True, help="directory written by `vrfkit export`")
    ap.add_argument("--maps", type=Path, required=True, help="valorant-api.com /v1/maps JSON")
    args = ap.parse_args(argv)
    try:
        manifest = json.loads((args.export / "manifest.json").read_text(encoding="utf-8"))
        map_url = manifest["level_names_and_times"][0]["name"]
        constants = load_constants(args.maps, map_url)
        table = pq.read_table(args.export / "movement.parquet", columns=["pos_x", "pos_y", "pos_z"])
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    x, y, z = (table.column(name).to_numpy(zero_copy_only=False).astype(np.float64)
               for name in ("pos_x", "pos_y", "pos_z"))
    live = ~parked(x, z)
    u, v = project(x[live], y[live], constants)
    inside = (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)
    share = inside.mean() if inside.size else 0.0
    print(f"{map_url}: {json.dumps(constants, sort_keys=True)}")
    print(f"  movement rows: {len(x)}, parked: {int((~live).sum())}, live: {int(live.sum())}")
    print(f"  live rows inside [0,1]^2: {int(inside.sum())} ({share:.4%})")
    if inside.size:
        print(f"  u {u.min():.3f}..{u.max():.3f}, v {v.min():.3f}..{v.max():.3f}")
    if share < MIN_INSIDE:
        print(f"FAILED: {share:.4%} of live rows inside, below {MIN_INSIDE:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
