"""GroundVolumeComponent FragmentInfo windows: synthetic wire fixtures only.

Every fixture is built bit by bit from the grammar in the tool's docstring, so
a test names the wire property it pins and fails when the decoder stops
enforcing it. No replay bytes or player data are used.
"""
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase, run_cli
import extract_ground_volumes as gv
from check_checksum_types import chain_checksum
from wire_fixtures import BitWriter

CNC_IDENTITY = ("FragmentInfo", 2225407835)
#: 13.02-shaped declaration: handle -> (name, compatible_checksum).
DECL = {
    1: ("bIsActive", 2967469237), 21: ("FinalCount", 2642881703),
    23: ("253", 1175316786), 24: ("bIsActive", 518428974), 25: ("Status", 2380676387),
    26: ("ExteriorSegments", 3326329067), 27: ("Begin", 3658211664), 28: ("End", 1988330146),
    30: ("ConvexHullPoints", 3039966384), 31: ("ConvexHullPoints", 2749781999),
    33: ("ConvexHullCeilings", 3975907906), 34: ("ConvexHullCeilings", 1547370894),
    36: ("ConvexHullTravelDistances", 1031017464), 37: ("ConvexHullTravelDistances", 1566128181),
    39: ("X", 2123226522), 40: ("Y", 2134384775), 41: ("TravelDistance", 956522941),
    42: ("Ceiling", 1959526051), 43: ("Floor", 3454040167),
}
#: The identities that sit directly in an item, and those inside elements.
ITEM_IDS = {"253": 1175316786, "bIsActive": 518428974, "Status": 2380676387,
            "ExteriorSegments": 3326329067, "ConvexHullPoints": 3039966384,
            "ConvexHullCeilings": 3975907906, "ConvexHullTravelDistances": 1031017464,
            "X": 2123226522, "Y": 2134384775, "TravelDistance": 956522941,
            "Ceiling": 1959526051, "Floor": 3454040167}
ELEMENT_IDS = {"Begin": 3658211664, "End": 1988330146, "ConvexHullPoints": 2749781999,
               "ConvexHullCeilings": 1547370894, "ConvexHullTravelDistances": 1566128181}
ITEM_ORDER = ("253", "bIsActive", "Status", "ExteriorSegments", "ConvexHullPoints",
              "ConvexHullCeilings", "ConvexHullTravelDistances", "X", "Y",
              "TravelDistance", "Ceiling", "Floor")
KIND = {"253": ("i32", 32), "bIsActive": ("uint", 1), "Status": ("uint", 3),
        "X": ("i32", 32), "Y": ("i32", 32), "TravelDistance": ("f32", 32),
        "Ceiling": ("f32", 32), "Floor": ("f32", 32), "Begin": ("uint", 8), "End": ("uint", 8),
        "ConvexHullPoints": ("v3d", 192), "ConvexHullCeilings": ("f32", 32),
        "ConvexHullTravelDistances": ("f32", 32), "TJunctions": ("uint", 16)}
POINTS = [[6915.6875681463225, 11655.582109527837, 300.0000305175781],
          [7075.687568105437, 11655.582109527837, 300.0],
          [7075.687568105437, 11815.582109527837, 99.99999237060547],
          [6915.6875681463225, 11815.582109486952, 99.99999237060547]]
CELL = {"253": 7, "bIsActive": True, "Status": 1,
        "ExteriorSegments": [{"Begin": 2, "End": 3}], "ConvexHullPoints": POINTS,
        "ConvexHullCeilings": [346.0000305175781, 332.459228515625, 146.0, 146.0],
        "ConvexHullTravelDistances": [1.0, 0.0, 1.0, 1.4142135381698608],
        "X": -1, "Y": 2, "TravelDistance": 0.8535534143447876,
        "Ceiling": 346.0000305175781, "Floor": 99.99999237060547}


#: The item struct's parent chain as the 13.06 executable names it; no export
#: declares these levels (nor GridPos), only the members below.
ITEM_CHAIN = [("FragmentInfo", "FGroundVolumeFragmentArray"), ("Items", "TArray"),
              ("Items", "FGroundVolumeFragment")]
#: Declared identity -> its path below FGroundVolumeFragment, one (name, C++
#: type) step per struct or array level; an array element repeats the name.
REPRODUCED = {
    ("253", 1175316786): [("ID", "int32")],
    ("bIsActive", 518428974): [("bIsActive", "bool")],
    ("ExteriorSegments", 3326329067): [("ExteriorSegments", "TArray")],
    ("ConvexHullPoints", 3039966384): [("ConvexHullPoints", "TArray")],
    ("ConvexHullPoints", 2749781999): [("ConvexHullPoints", "TArray"), ("ConvexHullPoints", "FVector")],
    ("ConvexHullCeilings", 3975907906): [("ConvexHullCeilings", "TArray")],
    ("ConvexHullCeilings", 1547370894): [("ConvexHullCeilings", "TArray"), ("ConvexHullCeilings", "float")],
    ("ConvexHullTravelDistances", 1031017464): [("ConvexHullTravelDistances", "TArray")],
    ("ConvexHullTravelDistances", 1566128181): [("ConvexHullTravelDistances", "TArray"),
                                                ("ConvexHullTravelDistances", "float")],
    ("TJunctions", 1038854951): [("TJunctions", "TArray")],
    ("X", 2123226522): [("GridPos", "FIntPoint"), ("X", "int32")],
    ("Y", 2134384775): [("GridPos", "FIntPoint"), ("Y", "int32")],
    ("TravelDistance", 956522941): [("TravelDistance", "float")],
    ("Ceiling", 1959526051): [("Ceiling", "float")],
    ("Floor", 3454040167): [("Floor", "float")],
}
#: Declared identities no spelling tried reproduces: an enum, and the two byte
#: members of the segment struct. Their types rest on widths and relations.
WIDTH_ONLY = {("Status", 2380676387), ("Begin", 3658211664), ("End", 1988330146)}
#: C++ type -> how the tool must read a member its checksum says has it.
READ_AS = {"int32": gv.Scalar("int32", 32), "bool": gv.Scalar("bool", 1),
           "float": gv.Scalar("float32", 32), "FVector": gv.Scalar("vector3d", 192)}
B1306, B1305 = "++Ares-Core+release-13.06", "++Ares-Core+release-13.05"
STATUS = ("Status", 2380676387)
STATUS_COUNTERS = ("status_named", "status_unnamed_declaration", "status_unnamed_value")


def scalar_bits(name, value, kind=None):
    kind, width = kind or KIND[name]
    out = BitWriter()
    if kind == "f32":
        return out.write(struct.unpack("<I", struct.pack("<f", value))[0], 32)
    if kind == "v3d":
        for part in value:
            out.write(struct.unpack("<Q", struct.pack("<d", part))[0], 64)
        return out
    return out.write(int(value) & ((1 << width) - 1), width)


def array_bits(elements, handles, single=None, indices=None, count=None):
    out = BitWriter().packed(len(elements) if count is None else count)
    for position, element in enumerate(elements):
        out.packed((position if indices is None else indices[position]) + 1)
        for name, value in ({single: element} if single else element).items():
            payload = scalar_bits(name, value)
            out.packed(handles[name] + 1).packed(len(payload)).extend(payload)
        out.packed(0)
    return out.packed(0)


def item_bits(values=None, decl=None, drop=(), override=None):
    """One changed item's member stream for a declaration (default DECL)."""
    decl = decl or DECL
    item_level = {name: h for h, (name, cs) in decl.items() if ITEM_IDS.get(name) == cs}
    element = {name: h for h, (name, cs) in decl.items() if ELEMENT_IDS.get(name) == cs}
    values = dict(CELL, **(values or {}))
    out = BitWriter()
    for name in ITEM_ORDER:
        if name in drop or name not in item_level:
            continue
        if override and name in override:
            payload = override[name]
        elif name == "ExteriorSegments":
            payload = array_bits(values[name], element)
        elif name.startswith("ConvexHull"):
            payload = array_bits(values[name], element, single=name)
        else:
            payload = scalar_bits(name, values[name])
        out.packed(item_level[name] + 1).packed(len(payload)).extend(payload)
    return out.packed(0)


def window(items, keys=(4, 0), deleted=(), handle_bits=1, cnc_handle=0, support=1,
           body_suffix=0):
    body = BitWriter().write(support, 1)
    for word in (*keys, len(deleted), len(items)):
        body.write(word & 0xFFFFFFFF, 32)
    for ident in deleted:
        body.write(ident & 0xFFFFFFFF, 32)
    for ident, stream in items:
        body.write(ident & 0xFFFFFFFF, 32).extend(stream)
    body.write(0, body_suffix)
    return BitWriter().write(cnc_handle, handle_bits).packed(len(body)).extend(body).to_bytes()


def schema(members=None, slots=2, cnc=None, error=None):
    return gv.ReplaySchema(DECL if members is None else members,
                           {0: CNC_IDENTITY} if cnc is None else cnc, slots, error)


def decode(raw_count, sch=None):
    raw, count = raw_count
    return gv.decode_window(raw, count, sch or schema())


class DecodeTests(unittest.TestCase):
    def assertRejects(self, reason, raw_count, sch=None):
        with self.assertRaises(gv.WireError) as caught:
            decode(raw_count, sch)
        self.assertEqual(str(caught.exception), reason)

    def test_item_members_decode_by_declared_identity(self):
        entries, items, counts = decode(window([(5, item_bits())], keys=(9, 4)))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fields"], CELL)
        self.assertEqual(list(items[0]["fields"]), list(ITEM_ORDER))
        self.assertEqual((items[0]["item_id"], items[0]["array_replication_key"],
                          items[0]["base_replication_key"]), (5, 9, 4))
        self.assertEqual(entries[0]["cnc_field"], "FragmentInfo")
        self.assertEqual(counts["array_elements"], 1 + 4 + 4 + 4)

    def test_same_names_under_other_handles_decode_identically(self):
        # 11.10-shaped: every member sits at a different handle. Only the
        # replay's declaration may map them; a fixed handle table cannot.
        shifted = {handle - 1: identity for handle, identity in DECL.items() if handle >= 23}
        _, items, _ = decode(window([(1, item_bits(decl=shifted))]), schema(shifted))
        self.assertEqual(items[0]["fields"], CELL)
        # The 13.02 stream read with the shifted declaration must not decode.
        with self.assertRaises(gv.WireError):
            decode(window([(1, item_bits())]), schema(shifted))

    def test_member_handle_is_encoded_minus_one(self):
        # Declare only handle 40 (Y); a stream carrying encoded 41 is handle 40.
        member = BitWriter().packed(41).packed(32).extend(scalar_bits("Y", -7)).packed(0)
        _, items, _ = decode(window([(1, member)]), schema({40: DECL[40], 41: DECL[41]}))
        self.assertEqual(items[0]["fields"], {"Y": -7})

    def test_support_bit_must_be_set(self):
        self.assertRejects("unsupported_support_bit", window([(1, item_bits())], support=0))

    def test_every_truncation_rejects(self):
        raw, count = window([(1, item_bits(drop=("ExteriorSegments",)))])
        for width in range(count):
            with self.subTest(width=width), self.assertRaises(gv.WireError):
                gv.decode_window(raw[:(width + 7) // 8], width, schema())

    def test_unconsumed_entry_bits_reject(self):
        self.assertRejects("unconsumed_entry", window([(1, item_bits())], body_suffix=1))

    def test_scalar_width_must_match_type(self):
        for bad in (31, 33):
            stream = item_bits(override={"X": BitWriter().write(3, bad)})
            with self.subTest(width=bad):
                self.assertRejects("member_width_mismatch", window([(1, stream)]))

    def test_floats_are_ieee754_and_vectors_three_doubles(self):
        values = {"Floor": 100.0, "Ceiling": -2.5,
                  "ConvexHullPoints": [[1.5, -2.25, 1e10]] * 3}
        _, items, _ = decode(window([(1, item_bits(values))]))
        fields = items[0]["fields"]
        self.assertEqual((fields["Floor"], fields["Ceiling"]), (100.0, -2.5))
        self.assertEqual(fields["ConvexHullPoints"], [[1.5, -2.25, 1e10]] * 3)
        self.assertIsInstance(fields["Floor"], float)

    def test_integers_are_signed_32_bit(self):
        _, items, _ = decode(window([(-3, item_bits({"X": -2, "Y": 2**31 - 1}))]))
        self.assertEqual((items[0]["item_id"], items[0]["fields"]["X"],
                          items[0]["fields"]["Y"]), (-3, -2, 2**31 - 1))

    def test_nonfinite_float_rejects(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertRejects("nonfinite_value", window([(1, item_bits({"Floor": value}))]))

    def test_array_elements_must_be_whole_and_in_order(self):
        handles = {"ConvexHullCeilings": 34}
        cases = {
            "array_index_order": array_bits([1.0, 2.0], handles, "ConvexHullCeilings", indices=[1, 0]),
            "array_index_bounds": array_bits([1.0], handles, "ConvexHullCeilings", indices=[3], count=2),
            "partial_array": array_bits([1.0], handles, "ConvexHullCeilings", count=2),
        }
        for reason, payload in cases.items():
            with self.subTest(reason=reason):
                stream = item_bits(override={"ConvexHullCeilings": payload})
                self.assertRejects(reason, window([(1, stream)]))

    def test_array_payload_must_close_exactly(self):
        payload = array_bits([1.0], {"ConvexHullCeilings": 34}, "ConvexHullCeilings").write(0, 8)
        self.assertRejects("unconsumed_array", window([(1, item_bits(override={"ConvexHullCeilings": payload}))]))

    def test_element_missing_a_member_rejects(self):
        payload = array_bits([{"Begin": 1}], {"Begin": 27, "End": 28})
        self.assertRejects("partial_element", window([(1, item_bits(override={"ExteriorSegments": payload}))]))

    def test_member_placement_and_declaration(self):
        stray = BitWriter().packed(28).packed(8).write(1, 8).packed(0)   # Begin at item level
        self.assertRejects("misplaced_member", window([(1, stray)]))
        foreign = BitWriter().packed(22).packed(32).write(1, 32).packed(0)  # FinalCount
        self.assertRejects("unrecognized_member", window([(1, foreign)]))
        undeclared = BitWriter().packed(46).packed(32).write(1, 32).packed(0)
        self.assertRejects("undeclared_member_handle", window([(1, undeclared)]))
        twice = BitWriter().packed(40).packed(32).write(1, 32).packed(40).packed(32).write(1, 32).packed(0)
        self.assertRejects("duplicate_member", window([(1, twice)]))

    def test_cnc_handle_width_follows_declared_slots(self):
        # SerializeInt(max 3) spends two bits on handle 0; max 2 spends one.
        wide = window([(1, item_bits())], handle_bits=2)
        _, items, _ = decode(wide, schema(slots=3))
        self.assertEqual(items[0]["fields"], CELL)
        with self.assertRaises(gv.WireError):
            decode(wide, schema(slots=2))
        with self.assertRaises(gv.WireError):
            decode(window([(1, item_bits())]), schema(slots=3))

    def test_cnc_field_must_be_declared_fragment_info(self):
        self.assertRejects("unrecognized_cnc_field", window([(1, item_bits())]),
                           schema(cnc={0: ("FragmentInfo", 1)}))
        self.assertRejects("undeclared_cnc_handle", window([(1, item_bits())]), schema(cnc={}))

    def test_deleted_ids_and_header_words_are_signed(self):
        entries, items, _ = decode(window([], keys=(-1, -2), deleted=(3, -4)))
        self.assertEqual(items, [])
        self.assertEqual(entries[0]["deleted_item_ids"], [3, -4])
        self.assertEqual((entries[0]["array_replication_key"], entries[0]["base_replication_key"]), (-1, -2))

    def test_untyped_member_kept_raw_and_counted(self):
        decl = {**DECL, 44: ("TJunctions", 1038854951)}
        for value, nonzero in ((0, 0), (0x1234, 1)):
            member = BitWriter().packed(45).packed(16).write(value, 16).packed(0)
            _, items, counts = decode(window([(1, member)]), schema(decl))
            with self.subTest(value=value):
                self.assertEqual(items[0]["fields"]["TJunctions"],
                                 {"untyped_bits": 16, "value_hex": format(value, "x")})
                self.assertEqual((counts["untyped_members"], counts["untyped_nonzero_members"]), (1, nonzero))
        wrong = BitWriter().packed(45).packed(8).write(0, 8).packed(0)
        self.assertRejects("member_width_mismatch", window([(1, wrong)]), schema(decl))

    def test_schema_error_rejects_every_window(self):
        self.assertRejects("declaration_conflict", window([(1, item_bits())]),
                           schema(error="declaration_conflict"))

    def test_empty_window_rejects(self):
        self.assertRejects("empty_window", (b"", 0))


class ChecksumTests(unittest.TestCase):
    """Declared checksums against the formula, computed in this file.

    The numbers come from the replays (held in the tool); the CRC is computed
    here from each member's path in the game's struct. A mistyped checksum,
    a wrong resolved name, or a member read as a type other than the one its
    checksum encodes fails here.
    """

    @staticmethod
    def path_checksum(steps):
        return chain_checksum(ITEM_CHAIN + list(steps))

    def test_declared_checksums_reproduce_from_the_struct_chain(self):
        # Every identity the tool decodes, except the width-only ones.
        for identity in (set(gv.MEMBERS) | gv.ELEMENT_IDENTITIES) - WIDTH_ONLY:
            with self.subTest(identity=identity):
                self.assertIn(identity, REPRODUCED)
                self.assertEqual(self.path_checksum(REPRODUCED[identity]), identity[1])

    def test_every_member_is_reproduced_or_width_only(self):
        # A new identity in MEMBERS needs a decision: which of the two it is.
        self.assertEqual(set(gv.MEMBERS) | gv.ELEMENT_IDENTITIES, set(REPRODUCED) | WIDTH_ONLY)
        self.assertFalse(set(REPRODUCED) & WIDTH_ONLY)

    def test_members_are_read_as_the_type_their_checksum_encodes(self):
        for identity, steps in REPRODUCED.items():
            spec = gv.MEMBERS.get(identity)
            if spec is None:
                holders = [s for s in gv.MEMBERS.values() if isinstance(s, gv.Array) and identity in s.elements]
                self.assertEqual(len(holders), 1, identity)
                spec = holders[0].elements[identity]
            cpp_type = steps[-1][1]
            with self.subTest(identity=identity):
                if identity[0] == "TJunctions":
                    # An array whose element was never sent: kept raw at the
                    # 16 bits an empty array's count and terminator take.
                    self.assertEqual((cpp_type, spec), ("TArray", gv.Untyped(16)))
                elif cpp_type == "TArray":
                    self.assertIsInstance(spec, gv.Array)
                else:
                    self.assertEqual(spec, READ_AS[cpp_type])

    def test_resolved_names_are_the_paths_their_checksums_encode(self):
        self.assertEqual(set(gv.RESOLVED_NAMES), {("253", 1175316786), ("X", 2123226522), ("Y", 2134384775)})
        for identity, path in gv.RESOLVED_NAMES.items():
            steps = REPRODUCED[identity]
            with self.subTest(identity=identity):
                self.assertIn(identity, gv.MEMBERS)
                self.assertEqual(".".join(name for name, _ in steps), path)
                self.assertEqual(self.path_checksum(steps), identity[1])


class NameTests(unittest.TestCase):
    def test_status_names_only_for_the_declaration_they_were_read_from(self):
        self.assertEqual([gv.status_label(B1306, STATUS, value) for value in range(4)],
                         [("AllInside", "status_named"), ("PartiallyOutside", "status_named"),
                          ("PartiallyBlocked", "status_named"), ("Invalid", "status_named")])
        # Same identity in another build: a checksum does not carry the
        # enumerators, and this one does not even reproduce.
        self.assertEqual(gv.status_label(B1305, STATUS, 1), (None, "status_unnamed_declaration"))
        # Same build, another checksum under the name (a direct call: MEMBERS
        # admits one Status identity).
        self.assertEqual(gv.status_label(B1306, ("Status", STATUS[1] ^ 1), 1),
                         (None, "status_unnamed_declaration"))
        # Count, the enum's count sentinel, and the rest of the 3-bit range.
        for value in (4, 5, 7):
            with self.subTest(value=value):
                self.assertEqual(gv.status_label(B1306, STATUS, value), (None, "status_unnamed_value"))

    def test_resolved_names_follow_the_declared_checksum(self):
        everything = {"253": "ID", "X": "GridPos.X", "Y": "GridPos.Y"}
        self.assertEqual(gv.resolved_names(schema()), everything)
        # Handles move between builds; the names follow the identity.
        shifted = {handle - 1: identity for handle, identity in DECL.items() if handle >= 23}
        self.assertEqual(gv.resolved_names(schema(shifted)), everything)
        # The same name under another checksum is not relabelled.
        other = {**DECL, 23: ("253", 1175316786 ^ 1), 39: ("X", 2123226522 ^ 1)}
        self.assertEqual(gv.resolved_names(schema(other)), {"Y": "GridPos.Y"})
        # Builds before 12.08 declare no `253`.
        self.assertEqual(gv.resolved_names(schema({h: i for h, i in DECL.items() if i[0] != "253"})),
                         {"X": "GridPos.X", "Y": "GridPos.Y"})


def table(rows, types):
    return pa.Table.from_pylist(rows, schema=pa.schema(list(types.items())))


FIELD_TYPES = {"time_ms": pa.uint32(), "packet_id": pa.uint32(), "channel_index": pa.uint32(),
               "actor_net_guid": pa.uint32(), "object_net_guid": pa.uint32(),
               "group_path": pa.string(), "handle": pa.uint32(), "field_name": pa.string(),
               "compatible_checksum": pa.uint32(), "bit_count": pa.uint32(), "raw_bits": pa.binary()}


def manifest_groups(decl=DECL, cnc=(0, *CNC_IDENTITY)):
    return [{"path": gv.CLASS_GROUP, "path_name_index": 7, "fields": [
                {"handle": h, "name": n, "compatible_checksum": c} for h, (n, c) in sorted(decl.items())]},
            {"path": gv.CNC_GROUP, "path_name_index": 8, "fields": [
                {"handle": cnc[0], "name": cnc[1], "compatible_checksum": cnc[2]}]}]


def make_export(root, window_rows, build="13.02", slots=(2,), cp_fields=(), actors=None, groups=None,
                net_guids=None):
    source = root / "export"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({
        "replay_build": f"++Ares-Core+release-{build}",
        "net_field_export_groups": manifest_groups() if groups is None else groups}), encoding="utf-8")
    base = {"time_ms": 500, "packet_id": 9, "channel_index": 3, "actor_net_guid": 100,
            "object_net_guid": 104, "handle": 0xFFFFFFFF, "field_name": gv.UNRESOLVED_CNC,
            "compatible_checksum": None, "group_path": "PatchVolume"}
    rows = [dict(base, group_path="AbilitiesAndBuffsComponent", raw_bits=b"\x01", bit_count=1)]
    rows += [dict(base, **r) for r in window_rows]
    pq.write_table(table(rows, FIELD_TYPES), source / "fields.parquet")
    pq.write_table(table([], {"checkpoint_index": pa.uint32(), "checkpoint_id": pa.string(), **FIELD_TYPES}),
                   source / "checkpoint_fields.parquet")
    pq.write_table(table(actors if actors is not None else [
        {"actor_net_guid": 100, "event": "open", "class_path": "/Game/X/Patch_Test.Patch_Test_C"},
        {"actor_net_guid": 100, "event": "close", "class_path": None}],
        {"actor_net_guid": pa.uint32(), "event": pa.string(), "class_path": pa.string()}),
        source / "actors.parquet")
    pq.write_table(table([{"net_guid": 104, "path": "PatchVolume", "outer_net_guid": 100}]
                         if net_guids is None else net_guids,
                         {"net_guid": pa.uint32(), "path": pa.string(), "outer_net_guid": pa.uint32()}),
                   source / "net_guids.parquet")
    pq.write_table(table([{"checkpoint_index": i, "path_name_index": 8, "group_path": gv.CNC_GROUP,
                           "declared_slots": s} for i, s in enumerate(slots)],
                         {"checkpoint_index": pa.uint32(), "path_name_index": pa.uint32(),
                          "group_path": pa.string(), "declared_slots": pa.uint32()}),
                   source / "checkpoint_export_groups.parquet")
    pq.write_table(table(list(cp_fields), {"checkpoint_index": pa.uint32(), "path_name_index": pa.uint32(),
                                           "handle": pa.uint32(), "compatible_checksum": pa.uint32(),
                                           "rendered_name": pa.string()}),
                   source / "checkpoint_export_fields.parquet")
    return source


def window_row(stream_items=None, **extra):
    raw, count = window(stream_items or [(1, item_bits())])
    return dict({"raw_bits": raw, "bit_count": count}, **extra)


class SchemaTests(TempDirTestCase):
    def test_main_and_checkpoint_declarations_are_merged(self):
        tmp = self.tmp()
        groups = manifest_groups(decl={h: i for h, i in DECL.items() if h != 43})
        source = make_export(tmp, [], groups=groups, cp_fields=[
            {"checkpoint_index": 0, "path_name_index": 8, "handle": 43,
             "compatible_checksum": 3454040167, "rendered_name": "Floor"}])
        # The checkpoint row names handle 43 only if it joins the class
        # group; give the class group its own checkpoint declaration.
        groups_table = pq.read_table(source / "checkpoint_export_groups.parquet").to_pylist()
        groups_table.append({"checkpoint_index": 0, "path_name_index": 7,
                             "group_path": gv.CLASS_GROUP, "declared_slots": 46})
        pq.write_table(table(groups_table, {"checkpoint_index": pa.uint32(), "path_name_index": pa.uint32(),
                                            "group_path": pa.string(), "declared_slots": pa.uint32()}),
                       source / "checkpoint_export_groups.parquet")
        fields = pq.read_table(source / "checkpoint_export_fields.parquet").to_pylist()
        fields[0]["path_name_index"] = 7
        pq.write_table(table(fields, {"checkpoint_index": pa.uint32(), "path_name_index": pa.uint32(),
                                      "handle": pa.uint32(), "compatible_checksum": pa.uint32(),
                                      "rendered_name": pa.string()}),
                       source / "checkpoint_export_fields.parquet")
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        loaded = gv.load_schema(source, manifest)
        self.assertIsNone(loaded.error)
        self.assertEqual(loaded.members[43], ("Floor", 3454040167))
        self.assertEqual((loaded.cnc, loaded.cnc_slots), ({0: CNC_IDENTITY}, 2))

    def test_declaration_conflict_is_detected(self):
        tmp = self.tmp()
        source = make_export(tmp, [], cp_fields=[
            {"checkpoint_index": 0, "path_name_index": 8, "handle": 0,
             "compatible_checksum": 99, "rendered_name": "SomethingElse"}])
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(gv.load_schema(source, manifest).error, "declaration_conflict")

    def test_cnc_slot_count_must_be_declared_and_agree(self):
        for slots, error in (((), "cnc_slots_undeclared"), ((2, 3), "cnc_slots_conflict"), ((2, 2), None)):
            with self.subTest(slots=slots), tempfile.TemporaryDirectory() as tmp:
                source = make_export(Path(tmp), [], slots=slots)
                manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(gv.load_schema(source, manifest).error, error)


class RouteTests(unittest.TestCase):
    def record(self, **changes):
        raw, count = window([(1, item_bits())])
        row = {"time_ms": 1, "packet_id": 2, "channel_index": 3, "actor_net_guid": 4,
               "object_net_guid": 5, "group_path": "PatchVolume", "handle": 0xFFFFFFFF,
               "field_name": gv.UNRESOLVED_CNC, "compatible_checksum": None,
               "bit_count": count, "raw_bits": raw}
        population = changes.pop("population", "fields")
        build = changes.pop("build", "++Ares-Core+release-13.02")
        row.update(changes)
        return gv.window_record(row, 27, population, build, schema())

    def test_route_identity_is_checked(self):
        record, items, _ = self.record()
        self.assertEqual((record["status"], record["route"], len(items)), ("decoded_exact", "bare_patch_volume", 1))
        tail, _, _ = self.record(field_name=gv.REP_LAYOUT_TAIL, handle=0, group_path=gv.CLASS_GROUP)
        self.assertEqual((tail["status"], tail["route"], tail["window_kind"]),
                         ("decoded_exact", "declared_class", "rep_layout_tail"))
        for changes in ({"handle": 0}, {"field_name": gv.REP_LAYOUT_TAIL}):
            with self.subTest(changes=changes):
                bad, items, _ = self.record(**changes)
                self.assertEqual((bad["status"], items), ("route_identity", []))
                self.assertIsNotNone(bad["raw_bits_hex"])

    def test_unmeasured_builds_and_checkpoint_rows_stay_raw(self):
        for changes, reason in (({"build": "++Ares-Core+release-99.99"}, "unvalidated_build"),
                                ({"build": "++Ares-Core+release-13.06", "group_path": gv.CLASS_GROUP},
                                 "unvalidated_build"),
                                ({"population": "checkpoint_fields"}, "unvalidated_checkpoint_route")):
            with self.subTest(reason=reason):
                record, items, _ = self.record(**changes)
                self.assertEqual((record["status"], items, record["entries"]), (reason, [], None))
                self.assertEqual(record["physical_row_ordinal"], 27)
                self.assertIsNotNone(record["raw_bits_hex"])


class CliTests(TempDirTestCase):
    def test_items_windows_and_receipt(self):
        root = self.tmp()
        source = make_export(root, [window_row()])
        out = root / "result"
        receipt = gv.extract(source, out)
        counts = receipt["counts"]
        self.assertEqual((counts["rows"], counts["windows_exact"], counts["rejected"],
                          counts["changed_items"], counts["hulls"], counts["items_complete"]),
                         (1, 1, 0, 1, 1, 1))
        self.assertEqual((counts["rows_unresolved_cnc_payload"], counts["rows_rep_layout_tail"],
                          counts["rows_bare_patch_volume"], counts["rows_declared_class"]), (1, 0, 1, 0))
        self.assertEqual(receipt["input_sha256_before"], receipt["input_sha256_after"])
        self.assertEqual(receipt["items_sha256"], gv.sha(out / "items.ndjson"))
        self.assertEqual(receipt["wire_bits_sha256"], gv.sha(Path(gv.__file__).with_name("wire_bits.py")))
        self.assertEqual(receipt["declarations"]["cnc_declared_slots"], 2)
        item = json.loads((out / "items.ndjson").read_text(encoding="utf-8"))
        self.assertEqual(item["physical_row_ordinal"], 1)
        self.assertEqual(item["fields"], CELL)
        self.assertEqual(item["owner_class_path"], "/Game/X/Patch_Test.Patch_Test_C")
        self.assertEqual((item["object_path"], item["object_outer_net_guid"]), ("PatchVolume", 100))
        self.assertEqual((counts["object_outer_is_actor"], counts["object_outer_not_actor"],
                          counts["object_guid_unresolved"]), (1, 0, 0))
        self.assertEqual(item["hull"], {"points_xy": [p[:2] for p in POINTS],
                                        "floor": CELL["Floor"], "ceiling": CELL["Ceiling"]})
        window_record = json.loads((out / "windows.ndjson").read_text(encoding="utf-8"))
        self.assertEqual(window_record["status"], "decoded_exact")
        with self.assertRaisesRegex(ValueError, "already exists"):
            gv.extract(source, out)
        with self.assertRaisesRegex(ValueError, "outside"):
            gv.extract(source, source / "forbidden")

    def test_the_receipt_is_written_with_lf_line_endings(self):
        """LF on every platform, like the two ndjson files (not CRLF on Windows)."""
        root = self.tmp()
        gv.extract(make_export(root, [window_row()]), root / "result")
        data = (root / "result" / "receipt.json").read_bytes()
        self.assertIn(b"\n", data)
        self.assertNotIn(b"\r\n", data)

    def test_status_name_is_written_and_counted_per_declaration(self):
        cases = (("13.06", {}, (), "PartiallyOutside", "status_named"),
                 ("13.05", {}, (), None, "status_unnamed_declaration"),
                 ("13.06", {"Status": 4}, (), None, "status_unnamed_value"),
                 ("13.06", {}, ("Status",), None, None))
        for build, values, drop, name, counter in cases:
            with self.subTest(build=build, values=values, drop=drop), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = make_export(root, [window_row([(1, item_bits(values, drop=drop))])], build=build)
                receipt = gv.extract(source, root / "result")
                item = json.loads((root / "result" / "items.ndjson").read_text(encoding="utf-8"))
                self.assertEqual(item["status_name"], name)
                self.assertEqual(item["fields"].get("Status"), None if drop else values.get("Status", CELL["Status"]))
                self.assertEqual({k: receipt["counts"][k] for k in STATUS_COUNTERS},
                                 {k: int(k == counter) for k in STATUS_COUNTERS})
                self.assertEqual(receipt["declarations"]["resolved_names"],
                                 {"253": "ID", "X": "GridPos.X", "Y": "GridPos.Y"})
                self.assertEqual(receipt["schema_version"], 2)

    def test_every_counter_is_written_even_when_zero(self):
        root = self.tmp()
        receipt = gv.extract(make_export(root, []), root / "result")
        self.assertEqual(set(receipt["counts"]), set(gv.COUNTERS))
        self.assertTrue(all(value == 0 for value in receipt["counts"].values()))

    def test_partial_item_and_owner_status_are_counted(self):
        root = self.tmp()
        actors = [{"actor_net_guid": 100, "event": "open", "class_path": "/A.A_C"},
                  {"actor_net_guid": 100, "event": "open", "class_path": "/B.B_C"}]
        source = make_export(root, [window_row([(1, item_bits(drop=("Floor",)))])], actors=actors)
        counts = gv.extract(source, root / "result")["counts"]
        self.assertEqual((counts["items_partial"], counts["items_complete"]), (1, 0))
        self.assertEqual((counts["owner_class_ambiguous"], counts["owner_class_resolved"]), (1, 0))
        item = json.loads((root / "result" / "items.ndjson").read_text(encoding="utf-8"))
        self.assertEqual((item["complete"], item["owner_class_path"], item["hull"]["floor"]),
                         (False, None, None))

    def test_object_outer_is_checked_against_the_actor(self):
        cases = (([{"net_guid": 104, "path": "PatchVolume", "outer_net_guid": 7}], (0, 1, 0), ("PatchVolume", 7)),
                 ([], (0, 0, 1), (None, None)))
        for guids, expected, identity in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                counts = gv.extract(make_export(root, [window_row()], net_guids=guids), root / "result")["counts"]
                self.assertEqual((counts["object_outer_is_actor"], counts["object_outer_not_actor"],
                                  counts["object_guid_unresolved"]), expected)
                item = json.loads((root / "result" / "items.ndjson").read_text(encoding="utf-8"))
                self.assertEqual((item["object_path"], item["object_outer_net_guid"]), identity)

    def test_rejections_are_retained_and_exit_nonzero(self):
        root = self.tmp()
        raw, count = window([(1, item_bits())], body_suffix=1)
        source = make_export(root, [{"raw_bits": raw, "bit_count": count}])
        out = root / "result"
        code, printed, errors = run_cli(gv.main, "--export-dir", source, "--out-dir", out, prog="extract")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(printed)["rejected"], 1)
        self.assertEqual(json.loads(errors), {"unconsumed_entry": 1})
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["counts"]["rejected"], receipt["rejection_reasons"]),
                         (1, {"unconsumed_entry": 1}))
        record = json.loads((out / "windows.ndjson").read_text(encoding="utf-8"))
        self.assertEqual((record["status"], record["raw_bits_hex"]), ("unconsumed_entry", raw.hex()))
        self.assertEqual((out / "items.ndjson").read_text(encoding="utf-8"), "")

    def test_changed_input_does_not_publish(self):
        root = self.tmp()
        source = make_export(root, [window_row()])
        out = root / "result"
        original = gv.selected_rows

        def alter(path, checkpoint):
            yield from original(path, checkpoint)
            if checkpoint:
                with (source / "manifest.json").open("a", encoding="utf-8") as handle:
                    handle.write(" ")
        with patch.object(gv, "selected_rows", alter), self.assertRaisesRegex(ValueError, "changed during read"):
            gv.extract(source, out)
        self.assertFalse(out.exists())
        self.assertEqual(list(root.glob(".ground-volumes-*")), [])
