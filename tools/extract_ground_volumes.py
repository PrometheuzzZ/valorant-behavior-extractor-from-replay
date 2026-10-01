"""Decode ground-area volume cells (GroundVolumeComponent FragmentInfo items)
with the item schema each replay declares.

A `Patch_*` actor's GroundVolumeComponent replicates its cells as the
FastArray `FragmentInfo` (handle 0 of its ClassNetCache), which the parser
keeps raw on two routes (ROUTES). One record per changed item: polygon in
world coordinates, floor, ceiling, grid cell, state and the owner's class.
Evidence: docs/GROUND_VOLUMES.md. Wire grammar:

    window  := entry+                      -- until the window's last bit
    entry   := handle:SerializeInt(max(slots, 2)) width:packed body[width]
    body    := support:1(=1) array_key:i32 base_key:i32 deletes:i32 changed:i32
               deleted_id:i32 * deletes
               (item_id:i32 members) * changed
    members := (handle+1:packed width:packed payload[width])* 0:packed
    array   := count:packed (index+1:packed members)* 0:packed

`slots` is the ClassNetCache group's declared slot count. A `members` handle
maps to the (name, compatible_checksum) the same replay declares (handles move
between builds, identities do not); MEMBERS gives the type. A window that does
not close exactly is a counted rejection with its raw bits kept.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import struct
import sys

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from atomic_io import sha256_file as sha, staged_output
from wire_bits import Bits, WireError, fastarray_header, iter_selected, load_net_guids, text

SCHEMA_VERSION = 2
CLASS_GROUP = "/Script/DynamicVolume.GroundVolumeComponent"
CNC_GROUP = CLASS_GROUP + "_ClassNetCache"
UNRESOLVED_CNC = "__vrfkit_unresolved_class_net_cache_payload__"
REP_LAYOUT_TAIL = "__vrfkit_unparsed_rep_layout_tail__"
#: field_name -> window kind; both start at a ClassNetCache field header.
WINDOW_KINDS = {UNRESOLVED_CNC: "unresolved_cnc_payload", REP_LAYOUT_TAIL: "rep_layout_tail"}
#: route -> exported group_path. `PatchVolume` is the component's unresolved
#: subobject name; each window is checked against the declared class.
ROUTES = {"bare_patch_volume": "PatchVolume", "declared_class": CLASS_GROUP}
ROUTE_BY_GROUP = {group: route for route, group in ROUTES.items()}
STREAMS = ("fields", "checkpoint_fields")
INPUTS = ("manifest.json", "fields.parquet", "checkpoint_fields.parquet", "actors.parquet",
          "net_guids.parquet", "checkpoint_export_groups.parquet", "checkpoint_export_fields.parquet")
#: The CNC field that carries the items, by its declared identity.
FRAGMENT_INFO = ("FragmentInfo", 2225407835)


def _builds(*versions: str) -> frozenset[str]:
    return frozenset(f"++Ares-Core+release-{v}" for v in versions)


#: (route, stream) -> builds whose windows all decoded exactly and passed the
#: independent checks: measured, not supported. Another build rejects as
#: `unvalidated_build`, an empty set as `unvalidated_checkpoint_route`.
ACCEPTED_BUILDS = {
    ("bare_patch_volume", "fields"): _builds(
        "11.06", "11.07", "11.08", "11.09", "11.10", "11.11",
        "12.00", "12.01", "12.02", "12.03", "12.04", "12.05", "12.06", "12.07",
        "12.08", "12.09", "13.01", "13.02", "13.04", "13.05", "13.06"),
    ("bare_patch_volume", "checkpoint_fields"): frozenset(),
    ("declared_class", "fields"): _builds("12.09", "13.01", "13.02", "13.04", "13.05"),
    ("declared_class", "checkpoint_fields"): frozenset(),
}
COLUMNS = ["time_ms", "packet_id", "channel_index", "actor_net_guid",
           "object_net_guid", "group_path", "handle", "field_name",
           "compatible_checksum", "bit_count", "raw_bits"]
#: Larger arrays reject before any element is read (measured maximum: 8).
MAX_ARRAY_COUNT = 4096


@dataclass(frozen=True)
class Scalar:
    """A fixed-width member. `kind` selects the reading in `read_scalar`."""
    kind: str
    width: int


@dataclass(frozen=True)
class Array:
    """A dynamic array. `elements` maps each element member's declared
    identity to its type. An element with one member whose name is the
    array's own name is a plain value; otherwise it is a struct."""
    elements: dict


@dataclass(frozen=True)
class Untyped:
    """Declared and measured at one width, never typed: kept as raw bits."""
    width: int


#: Declared (name, compatible_checksum) -> type. Each checksum reproduces from
#: its struct chain as the C++ type read here (the tests recompute each),
#: except `Status` (an enum) and `Begin`/`End` (measured widths). `253` is
#: `ID : int32`. TJunctions is a TArray only ever sent empty (16 bits): Untyped.
MEMBERS = {
    ("253", 1175316786): Scalar("int32", 32),
    ("bIsActive", 518428974): Scalar("bool", 1),
    ("Status", 2380676387): Scalar("uint", 3),
    ("ExteriorSegments", 3326329067): Array({
        ("Begin", 3658211664): Scalar("uint", 8),
        ("End", 1988330146): Scalar("uint", 8)}),
    ("ConvexHullPoints", 3039966384): Array({
        ("ConvexHullPoints", 2749781999): Scalar("vector3d", 192)}),
    ("ConvexHullCeilings", 3975907906): Array({
        ("ConvexHullCeilings", 1547370894): Scalar("float32", 32)}),
    ("ConvexHullTravelDistances", 1031017464): Array({
        ("ConvexHullTravelDistances", 1566128181): Scalar("float32", 32)}),
    ("TJunctions", 1038854951): Untyped(16),
    ("X", 2123226522): Scalar("int32", 32),
    ("Y", 2134384775): Scalar("int32", 32),
    ("TravelDistance", 956522941): Scalar("float32", 32),
    ("Ceiling", 1959526051): Scalar("float32", 32),
    ("Floor", 3454040167): Scalar("float32", 32),
}
#: Identities that belong inside an array element, never directly in an item.
ELEMENT_IDENTITIES = frozenset(i for spec in MEMBERS.values() if isinstance(spec, Array)
                               for i in spec.elements)
#: Declared identity -> the game's member path, by the whole (name, checksum)
#: pair; receipt only.
RESOLVED_NAMES = {
    ("253", 1175316786): "ID",
    ("X", 2123226522): "GridPos.X",
    ("Y", 2134384775): "GridPos.Y",
}
#: EGroundVolumeFragmentStatus as the 13.06 executable names it (4 is the Count
#: sentinel). A checksum does not encode enum values, so only that build and
#: identity are named.
STATUS_NAMES_BUILD = "++Ares-Core+release-13.06"
STATUS_IDENTITY = ("Status", 2380676387)
STATUS_NAMES = {0: "AllInside", 1: "PartiallyOutside", 2: "PartiallyBlocked", 3: "Invalid"}
#: Every per-export counter, printed and written even when zero.
COUNTERS = (
    "rows", "rows_fields", "rows_checkpoint_fields",
    "rows_bare_patch_volume", "rows_declared_class",
    "rows_unresolved_cnc_payload", "rows_rep_layout_tail",
    "windows_exact", "rejected", "entries", "deleted_items", "changed_items",
    "items_complete", "items_partial", "array_elements", "hulls",
    "untyped_members", "untyped_nonzero_members",
    "status_named", "status_unnamed_declaration", "status_unnamed_value",
    "owner_class_resolved", "owner_class_missing", "owner_class_ambiguous",
    "object_outer_is_actor", "object_outer_not_actor", "object_guid_unresolved",
)


@dataclass(frozen=True)
class ReplaySchema:
    """One replay's declarations for the component and its ClassNetCache."""
    members: dict        # handle -> (name, checksum), class group
    cnc: dict            # handle -> (name, checksum), ClassNetCache group
    cnc_slots: int | None
    error: str | None    # a window-independent reason every window rejects with

    def item_identities(self) -> frozenset:
        return frozenset(i for i in self.members.values() if i in MEMBERS)


def resolved_names(schema: ReplaySchema) -> dict:
    """Declared name -> game member path, for each RESOLVED_NAMES identity
    this replay declares with exactly that checksum."""
    return {identity[0]: RESOLVED_NAMES[identity]
            for _, identity in sorted(schema.members.items()) if identity in RESOLVED_NAMES}


def status_label(build: str, identity, value: int) -> tuple:
    """(enumerator name or None, the counter it is tallied under)."""
    if build != STATUS_NAMES_BUILD or identity != STATUS_IDENTITY:
        return None, "status_unnamed_declaration"
    name = STATUS_NAMES.get(value)
    return name, "status_named" if name is not None else "status_unnamed_value"


def read_scalar(bits: Bits, spec: Scalar):
    if spec.kind == "bool":
        return bool(bits.read(1))
    if spec.kind == "uint":
        return bits.read(spec.width)
    if spec.kind == "int32":
        return bits.i32()
    if spec.kind == "float32":
        value = struct.unpack("<f", bits.read(32).to_bytes(4, "little"))[0]
        if not math.isfinite(value):
            raise WireError("nonfinite_value")
        return value
    if spec.kind == "vector3d":
        value = [struct.unpack("<d", bits.read(64).to_bytes(8, "little"))[0] for _ in range(3)]
        if not all(math.isfinite(v) for v in value):
            raise WireError("nonfinite_value")
        return value
    raise WireError("unknown_kind")  # a MEMBERS entry with no reader


def member_stream(bits: Bits, schema: ReplaySchema, allowed: dict, counts: Counter) -> dict:
    """Read members until the 0 handle. Returns {identity: decoded value}."""
    out = {}
    while True:
        encoded = bits.packed()
        if encoded == 0:
            return out
        identity = schema.members.get(encoded - 1)
        if identity is None:
            raise WireError("undeclared_member_handle")
        if identity not in allowed:
            known = identity in MEMBERS or identity in ELEMENT_IDENTITIES
            raise WireError("misplaced_member" if known else "unrecognized_member")
        if identity in out:
            raise WireError("duplicate_member")
        width = bits.packed()
        payload = bits.take(width)
        spec = allowed[identity]
        if isinstance(spec, Array):
            out[identity] = read_array(payload, schema, identity, spec, counts)
        elif isinstance(spec, Untyped):
            if width != spec.width:
                raise WireError("member_width_mismatch")
            raw = payload.read(width)
            counts["untyped_members"] += 1
            counts["untyped_nonzero_members"] += raw != 0
            out[identity] = {"untyped_bits": width, "value_hex": format(raw, "x")}
        else:
            if width != spec.width:
                raise WireError("member_width_mismatch")
            out[identity] = read_scalar(payload, spec)
        if payload.remaining():
            raise WireError("unconsumed_member")


def read_array(bits: Bits, schema: ReplaySchema, identity, spec: Array, counts: Counter) -> list:
    count = bits.packed()
    if count > MAX_ARRAY_COUNT:
        raise WireError("array_count_bounds")
    elements = []
    while True:
        encoded = bits.packed()
        if encoded == 0:
            break
        index = encoded - 1
        if index >= count:
            raise WireError("array_index_bounds")
        # Every measured array is sent whole, in order: index i is element i.
        if index != len(elements):
            raise WireError("array_index_order")
        members = member_stream(bits, schema, spec.elements, counts)
        if set(members) != set(spec.elements):
            raise WireError("partial_element")
        if len(spec.elements) == 1 and next(iter(spec.elements))[0] == identity[0]:
            elements.append(next(iter(members.values())))
        else:
            elements.append({name: value for (name, _), value in members.items()})
    if len(elements) != count:
        raise WireError("partial_array")
    if bits.remaining():
        raise WireError("unconsumed_array")
    counts["array_elements"] += count
    return elements


def decode_window(raw: bytes, bit_count: int, schema: ReplaySchema) -> tuple[list, list, Counter]:
    """Fully consume one window or raise WireError: (entries, items, counts),
    entry offsets relative to the window."""
    if schema.error:
        raise WireError(schema.error)
    bits = Bits(raw, bit_count)
    if bits.remaining() == 0:
        raise WireError("empty_window")
    counts = Counter()
    entries, items = [], []
    while bits.remaining():
        handle = bits.serialize_int(max(schema.cnc_slots, 2))
        field = schema.cnc.get(handle)
        if field is None:
            raise WireError("undeclared_cnc_handle")
        if field != FRAGMENT_INFO:
            raise WireError("unrecognized_cnc_field")
        width = bits.packed()
        offset = bits.pos
        body = bits.take(width)
        array_key, base_key, deleted, changed = fastarray_header(body)
        entry_index = len(entries)
        for item_index in range(changed):
            item_id = body.i32()
            members = member_stream(body, schema, MEMBERS, counts)
            items.append({"entry_index": entry_index, "item_index": item_index,
                          "array_replication_key": array_key,
                          "base_replication_key": base_key, "item_id": item_id,
                          "identities": list(members),
                          "fields": {name: value for (name, _), value in members.items()}})
        if body.remaining():
            raise WireError("unconsumed_entry")
        entries.append({"cnc_handle": handle, "cnc_field": field[0], "bit_offset": offset,
                        "bit_count": width, "array_replication_key": array_key,
                        "base_replication_key": base_key, "deleted_item_ids": deleted,
                        "changed_items": changed})
    return entries, items, counts


def load_schema(export_dir: Path, manifest: dict) -> ReplaySchema:
    """Union of the main-stream (manifest) and every checkpoint declaration of
    both groups. The slot count, which sets the ClassNetCache handle width, is
    declared only in checkpoints. A handle with two identities, conflicting
    slot counts or none reject every window: picking one would be a guess.
    """
    declared = {CLASS_GROUP: {}, CNC_GROUP: {}}
    conflicts = []

    def add(group, handle, identity):
        seen = declared[group].setdefault(int(handle), identity)
        if seen != identity:
            conflicts.append((group, handle))

    for group in manifest.get("net_field_export_groups", []):
        if group.get("path") in declared:
            for field in group["fields"]:
                add(group["path"], field["handle"], (field["name"], int(field["compatible_checksum"])))
    wanted, slots = {}, set()
    groups = pq.read_table(export_dir / "checkpoint_export_groups.parquet",
                           columns=["checkpoint_index", "path_name_index", "group_path", "declared_slots"])
    for row in groups.to_pylist():
        if row["group_path"] in declared:
            wanted[(row["checkpoint_index"], row["path_name_index"])] = row["group_path"]
            if row["group_path"] == CNC_GROUP:
                slots.add(row["declared_slots"])
    if wanted:
        fields = pq.read_table(export_dir / "checkpoint_export_fields.parquet",
                               columns=["checkpoint_index", "path_name_index", "handle",
                                        "compatible_checksum", "rendered_name"])
        indexes = sorted({key[1] for key in wanted})
        fields = fields.filter(pc.is_in(fields["path_name_index"], value_set=pa.array(
            indexes, type=fields.schema.field("path_name_index").type)))
        for row in fields.to_pylist():
            group = wanted.get((row["checkpoint_index"], row["path_name_index"]))
            if group is not None:
                add(group, row["handle"], (row["rendered_name"], int(row["compatible_checksum"])))
    error = None
    if conflicts:
        error = "declaration_conflict"
    elif len(slots) > 1:
        error = "cnc_slots_conflict"
    elif not slots:
        error = "cnc_slots_undeclared"
    return ReplaySchema(declared[CLASS_GROUP], declared[CNC_GROUP],
                        next(iter(slots)) if len(slots) == 1 else None, error)


def selected_rows(path: Path, checkpoint: bool):
    """(physical row ordinal, row) for every row on a route, any handle."""
    columns = [*COLUMNS, *(["checkpoint_index", "checkpoint_id"] if checkpoint else [])]
    return iter_selected(path, columns, lambda b: pc.and_(
        pc.is_in(text(b, "group_path"), value_set=pa.array(list(ROUTE_BY_GROUP))),
        pc.is_in(text(b, "field_name"), value_set=pa.array(list(WINDOW_KINDS)))))


def owner_classes(export_dir: Path) -> dict:
    """actor GUID -> set of class paths on its `open` events."""
    owners = {}
    table = pq.read_table(export_dir / "actors.parquet", columns=["actor_net_guid", "event", "class_path"])
    for row in table.to_pylist():
        if row["event"] == "open":
            owners.setdefault(row["actor_net_guid"], set()).add(row["class_path"])
    return owners


def window_record(row: dict, ordinal: int, population: str, build: str, schema: ReplaySchema):
    """(window record, decoded items, per-window counts)."""
    route = ROUTE_BY_GROUP[row["group_path"]]
    record = {"population": population, "physical_row_ordinal": ordinal, "route": route,
              "window_kind": WINDOW_KINDS[row["field_name"]],
              "identity": {k: v for k, v in row.items() if k != "raw_bits"},
              "raw_bits_hex": row["raw_bits"].hex() if row["raw_bits"] is not None else None}
    items, counts = [], Counter()
    try:
        accepted = ACCEPTED_BUILDS[(route, population)]
        if not accepted:
            raise WireError("unvalidated_checkpoint_route")
        if build not in accepted:
            raise WireError("unvalidated_build")
        # REP_LAYOUT_TAIL rows carry handle 0 and UNRESOLVED_CNC rows u32::MAX.
        if row["handle"] != (0 if row["field_name"] == REP_LAYOUT_TAIL else 0xFFFFFFFF):
            raise WireError("route_identity")
        entries, items, counts = decode_window(row["raw_bits"], row["bit_count"], schema)
        record["status"] = "decoded_exact"
        record["entries"] = entries
    except WireError as error:
        record["status"] = str(error)
        record["entries"] = None
    return record, items, counts


def item_record(window: dict, item: dict, owners: dict, objects: dict,
                declared_items: frozenset, counts: Counter, build: str) -> dict:
    identity = window["identity"]
    classes = owners.get(identity["actor_net_guid"], set())
    if len(classes) == 1 and None not in classes:
        owner, owner_status = next(iter(classes)), "resolved"
    else:
        owner, owner_status = None, ("missing" if not classes or classes == {None} else "ambiguous")
    counts[f"owner_class_{owner_status}"] += 1
    obj = objects.get(identity["object_net_guid"])
    if obj is None:
        counts["object_guid_unresolved"] += 1
    else:
        # The component should be a subobject of the actor whose channel
        # carries it; a different outer is surfaced, not repaired.
        counts["object_outer_is_actor" if obj[1] == identity["actor_net_guid"]
               else "object_outer_not_actor"] += 1
    fields = item["fields"]
    complete = set(item["identities"]) == declared_items
    counts["items_complete" if complete else "items_partial"] += 1
    status_name = None
    for member in item["identities"]:
        if member[0] == "Status":
            status_name, counter = status_label(build, member, fields["Status"])
            counts[counter] += 1
    hull = None
    if "ConvexHullPoints" in fields:
        counts["hulls"] += 1
        hull = {"points_xy": [[p[0], p[1]] for p in fields["ConvexHullPoints"]],
                "floor": fields.get("Floor"), "ceiling": fields.get("Ceiling")}
    return {"population": window["population"],
            "physical_row_ordinal": window["physical_row_ordinal"],
            "route": window["route"], "window_kind": window["window_kind"],
            "entry_index": item["entry_index"], "item_index": item["item_index"],
            **{k: identity[k] for k in ("time_ms", "packet_id", "channel_index",
                                        "actor_net_guid", "object_net_guid")},
            "object_path": obj[0] if obj else None,
            "object_outer_net_guid": obj[1] if obj else None,
            "owner_class_path": owner, "owner_class_status": owner_status,
            "array_replication_key": item["array_replication_key"],
            "base_replication_key": item["base_replication_key"],
            "item_id": item["item_id"], "complete": complete,
            "fields": fields, "status_name": status_name, "hull": hull}


def extract(export_dir: Path, out_dir: Path) -> dict:
    return staged_output(export_dir, out_dir, INPUTS, lambda stage: write(export_dir, stage),
                         prefix=".ground-volumes-")


def write(export_dir: Path, stage: Path) -> dict:
    """Write items and windows into `stage`; return the receipt."""
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    build = manifest["replay_build"]
    schema = load_schema(export_dir, manifest)
    owners, objects = owner_classes(export_dir), load_net_guids(export_dir, "path", "outer_net_guid")
    declared_items = schema.item_identities()
    counts = Counter({k: 0 for k in COUNTERS})
    reasons = Counter()
    with (stage / "windows.ndjson").open("w", encoding="utf-8", newline="\n") as windows, \
            (stage / "items.ndjson").open("w", encoding="utf-8", newline="\n") as items_out:
        for stream in STREAMS:
            for ordinal, row in selected_rows(export_dir / f"{stream}.parquet", stream != "fields"):
                record, items, window_counts = window_record(row, ordinal, stream, build, schema)
                counts["rows"] += 1
                counts[f"rows_{stream}"] += 1
                counts[f"rows_{record['route']}"] += 1
                counts[f"rows_{record['window_kind']}"] += 1
                if record["status"] == "decoded_exact":
                    counts["windows_exact"] += 1
                    counts.update(window_counts)
                    counts["entries"] += len(record["entries"])
                    counts["deleted_items"] += sum(len(e["deleted_item_ids"]) for e in record["entries"])
                    counts["changed_items"] += len(items)
                    for item in items:
                        out = item_record(record, item, owners, objects, declared_items, counts, build)
                        items_out.write(json.dumps(out, separators=(",", ":"), allow_nan=False) + "\n")
                else:
                    counts["rejected"] += 1
                    reasons[record["status"]] += 1
                windows.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
    return {
        "schema_version": SCHEMA_VERSION, "replay_build": build,
        "counts": dict(counts), "rejection_reasons": dict(reasons),
        "declarations": {
            "class_group": {str(h): list(i) for h, i in sorted(schema.members.items())},
            "cnc_group": {str(h): list(i) for h, i in sorted(schema.cnc.items())},
            "cnc_declared_slots": schema.cnc_slots, "schema_error": schema.error,
            "resolved_names": resolved_names(schema)},
        "extractor_sha256": sha(Path(__file__)),
        "wire_bits_sha256": sha(Path(__file__).with_name("wire_bits.py")),
        "windows_sha256": sha(stage / "windows.ndjson"),
        "items_sha256": sha(stage / "items.ndjson"),
        "scope": ("GroundVolumeComponent FragmentInfo cells decoded with the replay's own "
                  "declared names; resolved_names by exact (name, checksum); Status "
                  "enumerator names for the 13.06 declaration only; no ability or player "
                  "semantics."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True, help="New directory outside the export")
    args = parser.parse_args()
    try:
        receipt = extract(args.export_dir, args.out_dir)
    except (OSError, ValueError, KeyError, pa.ArrowException) as error:
        print(f"ground-volumes: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt["counts"], sort_keys=True))
    if receipt["rejection_reasons"]:
        print(json.dumps(receipt["rejection_reasons"], sort_keys=True), file=sys.stderr)
    return 1 if receipt["counts"]["rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
