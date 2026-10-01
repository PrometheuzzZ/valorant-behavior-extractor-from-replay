"""Export numeric AbilitiesAndBuffs FastArray updates with their original bits.

The measured grammar is a support bit, four signed little-endian i32 header
words, deleted item IDs, and changed item IDs followed by packed handle/width
property streams WITHOUT a per-item checksum bit. This recovers boundaries;
it does not identify abilities, effects, player actions, or property meanings.

The parser exports that window on two routes (see ROUTES). Every record names
its route and its stream (`population`: fields or checkpoint_fields).
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from atomic_io import sha256_file as sha, staged_output
from wire_bits import Bits as _Bits, WireError, fastarray_header, iter_selected, text

SCHEMA_VERSION = 2
#: Every route's rows carry handle 1; another handle rejects as `route_identity`.
ROUTE_HANDLE = 1
#: route -> the exact (group_path, field_name) pair, not a cross product. Both
#: are the handle-1 payload of an AbilitiesAndBuffs ClassNetCache stream from
#: the FastArray support bit (crates/vrfkit/src/sink/stream.rs: `cnc_h1` from
#: emit_brute_forced_cnc_rpcs, `chained_cnc_h1` from on_rep_layout_tail).
ROUTES = {
    "cnc_h1": ("AbilitiesAndBuffsComponent", "_cnc_h1"),
    "chained_cnc_h1": ("/Script/ShooterGame.AresAbilitySystemComponent",
                       "__vrfkit_chained_cnc_h1__"),
}
ROUTE_BY_IDENTITY = {identity: route for route, identity in ROUTES.items()}
STREAMS = ("fields", "checkpoint_fields")


def _builds(*versions: str) -> frozenset[str]:
    return frozenset(f"++Ares-Core+release-{v}" for v in versions)


#: (route, stream) -> builds whose windows were all walked exactly, in
#: agreement with an independent reader: measured, not supported. Another build
#: rejects as `unvalidated_build`, an empty set as `unvalidated_*_route`, and
#: the exit is nonzero. cnc_h1 has no 12.10/12.11 or checkpoint windows; its
#: 13.00 entry rests on six windows (docs/GAS_AND_PATCHVOLUME_INVESTIGATION.md).
_LEGACY = ("11.06", "11.07", "11.08", "11.09", "11.10", "11.11", "12.00", "12.01",
           "12.02", "12.03", "12.04", "12.05", "12.06", "12.07", "12.08", "12.09")
ACCEPTED_BUILDS = {
    ("cnc_h1", "fields"): _builds(
        *_LEGACY, "13.00", "13.01", "13.02", "13.04", "13.05", "13.06"),
    ("cnc_h1", "checkpoint_fields"): frozenset(),
    ("chained_cnc_h1", "fields"): _builds(
        *_LEGACY, "12.10", "12.11", "13.00", "13.01", "13.02", "13.04", "13.05", "13.06"),
    ("chained_cnc_h1", "checkpoint_fields"): _builds(
        *_LEGACY, "12.10", "12.11", "13.00", "13.01", "13.02", "13.04", "13.05", "13.06"),
}
#: Per-observation totals, reported overall and for every route and stream.
METRICS = ("rows", "structural_exact", "rejected", "deleted_items", "changed_items", "raw_fields")
COLUMNS = ["time_ms", "packet_id", "channel_index", "actor_net_guid",
           "object_net_guid", "group_path", "handle", "field_name",
           "compatible_checksum", "bit_count", "raw_bits"]


class Bits(_Bits):
    TRUNCATED = "truncated_scalar"
    OVERRUN = "field_overrun"


def decode(raw: bytes, bit_count: int) -> dict:
    """Fully consume the measured no-checksum-item variant, or raise.

    IDs and replication keys are signed i32 wire values, not actor GUIDs, and
    need no monotonicity: a delta can span updates missing from this stream.
    """
    reader = Bits(raw, bit_count)
    array_key, base_key, deleted_ids, changed = fastarray_header(reader)
    entries = []
    for _ in range(changed):
        item_id = reader.i32()
        fields = []
        while True:
            encoded = reader.packed()
            if encoded == 0:
                break
            width = reader.packed()
            fields.append({"handle": encoded - 1, "bit_offset": reader.pos,
                           "bit_count": width})
            reader.take(width)
        entries.append({"item_id": item_id, "fields": fields})
    if reader.pos != reader.end:
        raise WireError("unconsumed_suffix")
    return {"supports_delta_struct_serialization": True,
            "array_replication_key": array_key, "base_replication_key": base_key,
            "num_deletes": len(deleted_ids), "num_changed": changed,
            "deleted_item_ids": deleted_ids, "changed_items": entries,
            "consumed_bits": reader.pos}


def route_mask(groups: pa.Array, names: pa.Array) -> pa.Array:
    """True where a row's (group_path, field_name) is exactly one route's pair."""
    pairs = [pc.fill_null(pc.and_(pc.equal(groups, group), pc.equal(names, name)), False)
             for group, name in ROUTES.values()]
    mask = pairs[0]
    for pair in pairs[1:]:
        mask = pc.or_(mask, pair)
    return mask


def selected_rows(path: Path, checkpoint: bool):
    columns = [*COLUMNS, *(["checkpoint_index", "checkpoint_id"] if checkpoint else [])]
    return iter_selected(path, columns, lambda b: route_mask(text(b, "group_path"), text(b, "field_name")))


def unselected_route_name_rows(path: Path) -> int:
    """Rows carrying a route's field name under a group no route pairs it
    with: counted, neither selected nor decoded (0 on every measured export)."""
    route_names = pa.array(sorted({name for _, name in ROUTES.values()}))
    total = 0
    for batch in pq.ParquetFile(path).iter_batches(columns=["group_path", "field_name"],
                                                   batch_size=65536, use_threads=False):
        groups = pc.cast(batch.column("group_path"), pa.string())
        names = pc.cast(batch.column("field_name"), pa.string())
        named = pc.fill_null(pc.is_in(names, value_set=route_names), False)
        unselected = pc.and_(named, pc.invert(route_mask(groups, names)))
        total += pc.sum(pc.cast(unselected, pa.int64())).as_py() or 0
    return total


def observation(row: dict, ordinal: int, population: str, build: str) -> dict:
    route = ROUTE_BY_IDENTITY.get((row["group_path"], row["field_name"]))
    result = {"population": population, "route": route, "physical_row_ordinal": ordinal,
              "identity": {k: v for k, v in row.items() if k != "raw_bits"},
              "raw_bits_hex": row["raw_bits"].hex() if row["raw_bits"] is not None else None}
    try:
        if route is None or population not in STREAMS:
            raise WireError("route_identity")
        accepted = ACCEPTED_BUILDS[(route, population)]
        if not accepted:
            raise WireError("unvalidated_checkpoint_route" if population == "checkpoint_fields"
                            else "unvalidated_main_route")
        if build not in accepted:
            raise WireError("unvalidated_build")
        if row["handle"] != ROUTE_HANDLE:
            raise WireError("route_identity")
        result["structure"] = decode(row["raw_bits"], row["bit_count"])
        result["status"] = "structural_exact"
    except WireError as error:
        result["structure"] = None
        result["status"] = str(error)
    return result


def empty_counts() -> Counter:
    """Every count key, zero-valued, so an absent route prints as 0, not as nothing."""
    keys = [*METRICS, *(f"{stream}_rows" for stream in STREAMS)]
    keys += [f"{route}.{stream}.{metric}" for route in ROUTES for stream in STREAMS for metric in METRICS]
    keys += [f"unselected_route_name.{stream}.rows" for stream in STREAMS]
    return Counter({key: 0 for key in keys})


def tally(counts: Counter, prefix: str, record: dict) -> None:
    counts[prefix + "rows"] += 1
    structure = record["structure"]
    if structure is None:
        counts[prefix + "rejected"] += 1
        return
    counts[prefix + "structural_exact"] += 1
    counts[prefix + "deleted_items"] += structure["num_deletes"]
    counts[prefix + "changed_items"] += structure["num_changed"]
    counts[prefix + "raw_fields"] += sum(len(item["fields"]) for item in structure["changed_items"])


def extract(export_dir: Path, out_dir: Path) -> dict:
    def write(stage: Path) -> dict:
        build = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))["replay_build"]
        counts, reasons = empty_counts(), Counter()
        output = stage / "observations.ndjson"
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            for stream in STREAMS:
                path = export_dir / f"{stream}.parquet"
                counts[f"unselected_route_name.{stream}.rows"] += unselected_route_name_rows(path)
                for ordinal, row in selected_rows(path, stream == "checkpoint_fields"):
                    record = observation(row, ordinal, stream, build)
                    counts[stream + "_rows"] += 1
                    tally(counts, "", record)
                    tally(counts, f"{record['route']}.{stream}.", record)
                    if record["structure"] is None:
                        reasons[record["status"]] += 1
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        return {"schema_version": SCHEMA_VERSION, "replay_build": build,
                "routes": {route: {"group_path": group, "field_name": name, "handle": ROUTE_HANDLE}
                           for route, (group, name) in ROUTES.items()},
                "counts": dict(counts), "rejection_reasons": dict(reasons),
                "extractor_sha256": sha(Path(__file__)), "observations_sha256": sha(output),
                "wire_bits_sha256": sha(Path(__file__).with_name("wire_bits.py")),
                "scope": "Numeric FastArray boundaries; no field names, gameplay meanings, casts, or player attribution."}
    return staged_output(export_dir, out_dir, ("manifest.json", *(f"{s}.parquet" for s in STREAMS)),
                         write, prefix=".fastarray-")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True, help="New directory outside the export")
    args = parser.parse_args()
    try:
        receipt = extract(args.export_dir, args.out_dir)
    except (OSError, ValueError, KeyError, pa.ArrowException) as error:
        print(f"fastarray: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt["counts"], sort_keys=True))
    return 1 if receipt["counts"]["rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
