"""Independently inspect two observed ability DynamicArray wire payloads.

Usage: python tools/validate_ability_array_evidence.py EXPORT_DIR [...]
The input directories contain fields.parquet. Every matching parent is checked
for explicit array/element terminators, exact bit consumption, known member
handles, and the member widths the descriptors declare (pinned in ROUTES).
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from validate_type_evidence import Bits, fname

BLINDS = ("/Script/ShooterGame.BlindManagerComponent", "ActiveBlinds", 3853965310)
PATH = ("/Script/ShooterGame.PrecalculatedProjectileMovementComponent_ClassNetCache",
        "MulticastSetPath.NetworkedProjectilePath", 2930105559)
PATH_GROUP = "/Script/ShooterGame.PrecalculatedProjectileMovementComponent:MulticastSetPath"

#: route -> {member handle: (name, allowed widths, declared checksum)}
ROUTES = {
    BLINDS: {
        3: ("BlindId", (32,), 2836858544),
        4: ("EffectID", (64,), 3321413110),
        5: ("SourceID", (297,), 4130766059),
        6: ("bLocalEffect", (1,), 2802682995),
        7: ("bTransient", (1,), 815378154),
        8: ("InitialDuration", (32,), 1370668337),
        9: ("StartNetMovementTime", (32,), 2358118895),
        10: ("BlindConfig", (16,), 4121438116),
        11: ("CausingActor", (8, 16, 24), 2370661694),
    },
    PATH: {
        1: ("ElapsedSeconds", (32,), None),
        2: ("Location", (192,), None),
        3: ("Velocity", (192,), None),
    },
}

#: route -> (label, manifest group, {handle: (name, checksum)} it must declare)
DECLARATIONS = {
    BLINDS[1]: ("BlindManager", BLINDS[0], {2: BLINDS[1:], **{
        handle: (name, checksum) for handle, (name, _, checksum) in ROUTES[BLINDS].items()}}),
    PATH[1]: ("MulticastSetPath", PATH_GROUP, {0: ("NetworkedProjectilePath", PATH[2])}),
}


def member_value(handle: int, data: bytes, width: int, path_point: bool):
    """`(column, value)` of one member window, read independently."""
    bits = Bits(data, width)
    if path_point:
        result = ("value_f64", bits.ieee(32)) if handle == 1 else ("value_str", tuple(bits.ieee(64) for _ in range(3)))
    elif handle in (3, 4):
        result = ("value_i64", bits.bits(width))
    elif handle == 5:
        result = ("value_str", fname(bits))
    elif handle in (6, 7):
        result = ("value_bool", bits.bit())
    elif handle in (8, 9):
        result = ("value_f64", bits.ieee(32))
    else:
        result = ("value_i64", bits.int_packed())
    if bits.remaining():
        raise ValueError(f"member {handle} consumed {bits.pos} of {width} bits")
    numbers = result[1] if isinstance(result[1], tuple) else (result[1],)
    if any(isinstance(n, float) and not math.isfinite(n) for n in numbers):
        raise ValueError(f"member {handle} is non-finite")
    return result


def inspect(row: dict, spec: dict) -> tuple[int, Counter, dict]:
    bits = Bits(row["raw_bits"], row["bit_count"])
    capacity = bits.int_packed()
    if capacity > 4096:
        raise ValueError(f"array capacity {capacity} exceeds decoder limit")
    observations = Counter()
    children = {}
    path_point = row["field_name"].startswith("MulticastSetPath.")
    seen = 0
    while True:
        encoded_index = bits.int_packed()
        if encoded_index == 0:
            break
        if encoded_index - 1 >= capacity:
            raise ValueError(f"array index {encoded_index - 1} >= capacity {capacity}")
        seen += 1
        if seen > 4096:
            raise ValueError("too many array elements")
        fields = 0
        while True:
            encoded_handle = bits.int_packed()
            if encoded_handle == 0:
                break
            handle = encoded_handle - 1
            width = bits.int_packed()
            if handle not in spec:
                raise ValueError(f"unknown member handle {handle}")
            name, allowed, _checksum = spec[handle]
            if width not in allowed:
                raise ValueError(f"{name} width {width}, expected {allowed}")
            if width > bits.remaining():
                raise ValueError(f"{name} extends past parent")
            raw = bits.bits(width).to_bytes((width + 7) // 8, "little")
            child_name = f"{row['field_name']}[{encoded_index - 1}].{name}"
            if child_name in children:
                raise ValueError(f"duplicate child {child_name}")
            column, value = member_value(handle, raw, width, path_point)
            children[child_name] = (handle, width, raw, column, value)
            observations[(handle, width)] += 1
            fields += 1
            if fields > 128:
                raise ValueError("too many member fields")
    # An ActiveBlinds delta may update some members, or none plus one zero
    # trailer byte; a path point needs all three members and no trailer.
    if not path_point and seen == 0 and bits.remaining() == 8:
        if bits.int_packed() != 0:
            raise ValueError("nonzero empty-array trailer")
    if bits.remaining():
        raise ValueError(f"{bits.remaining()} unconsumed bits")
    if path_point and len(children) != seen * 3:
        raise ValueError(f"{len(children)} members for {seen} elements")
    return seen, observations, children


def check_declarations(export: Path, rows: Counter) -> None:
    groups = {
        group["path"]: group["fields"]
        for group in json.loads((export / "manifest.json").read_text(encoding="utf-8"))[
            "net_field_export_groups"
        ]
    }
    for route, (label, group, wanted) in DECLARATIONS.items():
        if not rows[route]:
            continue
        observed = {field["handle"]: (field["name"], field["compatible_checksum"]) for field in groups[group]}
        for handle, declaration in wanted.items():
            if observed.get(handle) != declaration:
                raise ValueError(f"{label} handle {handle}: {observed.get(handle)} != {declaration}")


def compare_children(parent: dict, expected: dict, emitted: list[dict]) -> None:
    if len(expected) != len(emitted):
        raise ValueError(f"{parent['field_name']}: {len(emitted)} emitted children, expected {len(expected)}")
    scope = ("time_ms", "packet_id", "channel_index", "actor_net_guid", "object_net_guid", "group_path")
    path_point = parent["field_name"].startswith("MulticastSetPath.")
    for (name, (handle, width, raw, column, value)), child in zip(expected.items(), emitted):
        if child["field_name"] != name:
            raise ValueError(f"child path {child['field_name']} != {name}")
        if any(child[key] != parent[key] for key in scope):
            raise ValueError(f"{name}: child context differs from parent")
        if child["handle"] != (0 if path_point else handle):
            raise ValueError(f"{name}: wrong exported handle {child['handle']}")
        if child["compatible_checksum"] is not None:
            raise ValueError(f"{name}: nested child has a top-level checksum")
        if child["bit_count"] != width or child["raw_bits"] != raw:
            raise ValueError(f"{name}: child raw window differs from parent")
        if value is None or child[column] is None:
            raise ValueError(f"{name}: null typed value")
        if column == "value_str" and isinstance(value, tuple):
            text = child[column]
            if not (text.startswith("(") and text.endswith(")")):
                raise ValueError(f"{name}: malformed vector {text!r}")
            actual = tuple(float(part) for part in text[1:-1].split(","))
            if actual != value:
                raise ValueError(f"{name}: vector {actual} != {value}")
        elif child[column] != value:
            raise ValueError(f"{name}: value {child[column]!r} != {value!r}")
        if any(child[key] is not None for key in ("value_i64", "value_f64", "value_bool", "value_str") if key != column):
            raise ValueError(f"{name}: multiple typed columns")


def relevant_rows(export: Path, compare_typed: bool):
    columns = ["group_path", "field_name", "compatible_checksum", "bit_count", "raw_bits"]
    if compare_typed:
        columns += [
            "time_ms", "packet_id", "channel_index", "actor_net_guid", "object_net_guid",
            "handle", "value_i64", "value_f64", "value_bool", "value_str",
        ]
    parents = [key[1] for key in ROUTES]
    for batch in pq.ParquetFile(export / "fields.parquet").iter_batches(batch_size=65536, columns=columns):
        names = pc.cast(batch["field_name"], pa.string())
        mask = pc.is_in(names, value_set=pa.array(parents))
        if compare_typed:
            for parent in parents:
                mask = pc.or_(mask, pc.starts_with(names, parent + "["))
        yield from batch.filter(pc.fill_null(mask, False)).to_pylist()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exports", nargs="+", type=Path)
    parser.add_argument("--require-routes", action="store_true", help="fail if either route has zero rows across all exports")
    parser.add_argument("--compare-typed", action="store_true", help="compare emitted child rows to an independent decode of parent raw bits")
    args = parser.parse_args(argv)
    failed = False
    total_rows = Counter()
    for export in args.exports:
        rows = Counter({key[1]: 0 for key in ROUTES})
        elements = Counter()
        members = Counter()
        typed = Counter({key[1]: 0 for key in ROUTES})
        pending = {key[1]: [] for key in ROUTES}
        for row in relevant_rows(export, args.compare_typed):
            if args.compare_typed:
                child_route = next(
                    (key[1] for key in ROUTES if row["group_path"] == key[0] and row["field_name"] is not None
                     and row["field_name"].startswith(key[1] + "[")),
                    None,
                )
                if child_route:
                    pending[child_route].append(row)
                    continue
            key = (row["group_path"], row["field_name"], row["compatible_checksum"])
            spec = ROUTES.get(key)
            if spec is None:
                continue
            try:
                count, observed, expected_children = inspect(row, spec)
                if args.compare_typed:
                    compare_children(row, expected_children, pending[key[1]])
                    typed[key[1]] += len(expected_children)
            except ValueError as exc:
                failed = True
                print(f"{export}: {key[1]} {row['bit_count']} bits: {exc}")
                pending[key[1]].clear()
                continue
            pending[key[1]].clear()
            rows[key[1]] += 1
            elements[key[1]] += count
            members.update({(key[1], handle, width): count for (handle, width), count in observed.items()})
        if args.compare_typed and any(pending.values()):
            failed = True
            print(f"{export}: orphan children: { {name: len(children) for name, children in pending.items()} }")
        try:
            check_declarations(export, rows)
        except (KeyError, ValueError) as exc:
            failed = True
            print(f"{export}: declaration mismatch: {exc}")
        total_rows.update(rows)
        print(f"{export}: rows={dict(rows)} elements={dict(elements)} typed_children={dict(typed)} members={dict(members)}")
    if args.require_routes and any(total_rows[key[1]] == 0 for key in ROUTES):
        failed = True
        print(f"missing observed route: {dict(total_rows)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
