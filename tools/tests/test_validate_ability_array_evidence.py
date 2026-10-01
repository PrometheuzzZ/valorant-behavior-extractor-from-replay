"""Fail-closed checks for the independent ability-array wire validator."""

from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from support import run_cli
import validate_ability_array_evidence as evidence
from wire_fixtures import array


def path_point(fields=((1, 32), (2, 192), (3, 192))):
    raw, count = array([(0, [(handle, width, bytes(width // 8)) for handle, width in fields])])
    return {"field_name": "MulticastSetPath.NetworkedProjectilePath", "raw_bits": raw, "bit_count": count}


class AbilityArrayEvidenceTests(unittest.TestCase):
    def test_blind_empty_delta_admits_only_one_zero_trailer(self):
        key = next(key for key in evidence.ROUTES if key[1] == "ActiveBlinds")
        for raw in (b"\x02\0", b"\x02\0\0", b"\x04\0\0"):
            row = {"field_name": key[1], "raw_bits": raw, "bit_count": len(raw) * 8}
            self.assertEqual(evidence.inspect(row, evidence.ROUTES[key]), (0, Counter(), {}))
        for raw in (b"\x02\0\x01", b"\x02\0\x02", b"\x02\0\0\0"):
            row = {"field_name": key[1], "raw_bits": raw, "bit_count": len(raw) * 8}
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                evidence.inspect(row, evidence.ROUTES[key])

    def test_blind_sparse_delta_decodes_null_actor(self):
        key = next(key for key in evidence.ROUTES if key[1] == "ActiveBlinds")
        raw = b"\x02\x02\x18\x10\0\0\0"
        row = {"field_name": key[1], "raw_bits": raw, "bit_count": len(raw) * 8}
        count, members, children = evidence.inspect(row, evidence.ROUTES[key])
        self.assertEqual(count, 1)
        self.assertEqual(members, Counter({(11, 8): 1}))
        self.assertEqual(children, {"ActiveBlinds[0].CausingActor": (11, 8, b"\0", "value_i64", 0)})
        for altered in (raw + b"\0", raw[:4] + b"\x01" + raw[5:]):
            with self.subTest(raw=altered), self.assertRaises(ValueError):
                evidence.inspect({**row, "raw_bits": altered, "bit_count": len(altered) * 8}, evidence.ROUTES[key])

    def test_complete_path_point_consumes_entire_window(self):
        key = next(key for key in evidence.ROUTES if "NetworkedProjectilePath" in key[1])
        count, members, children = evidence.inspect(path_point(), evidence.ROUTES[key])
        self.assertEqual(count, 1)
        self.assertEqual(members, Counter({(1, 32): 1, (2, 192): 1, (3, 192): 1}))
        self.assertEqual(len(children), 3)

    def test_changed_width_unknown_handle_and_suffix_are_rejected(self):
        key = next(key for key in evidence.ROUTES if "NetworkedProjectilePath" in key[1])
        spec = evidence.ROUTES[key]
        for row in (
            path_point(((1, 32), (2, 184), (3, 192))),
            path_point(((1, 32), (4, 192), (3, 192))),
            {**path_point(), "raw_bits": path_point()["raw_bits"] + b"\0", "bit_count": path_point()["bit_count"] + 8},
        ):
            with self.subTest(row=row["bit_count"]), self.assertRaises(ValueError):
                evidence.inspect(row, spec)

    def test_missing_terminator_or_truncated_member_is_rejected(self):
        key = next(key for key in evidence.ROUTES if "NetworkedProjectilePath" in key[1])
        spec = evidence.ROUTES[key]
        row = path_point()
        for width in (row["bit_count"] - 1, row["bit_count"] - 8, 16):
            with self.subTest(width=width), self.assertRaises(ValueError):
                evidence.inspect({**row, "bit_count": width}, spec)

    def test_missing_member_and_zero_width_unknown_are_rejected(self):
        key = next(key for key in evidence.ROUTES if "NetworkedProjectilePath" in key[1])
        spec = evidence.ROUTES[key]
        for row in (
            path_point(((1, 32), (2, 192))),
            path_point(((1, 32), (2, 192), (3, 192), (4, 0))),
        ):
            with self.subTest(raw=row["raw_bits"]), self.assertRaises(ValueError):
                evidence.inspect(row, spec)

    def test_a_payload_shorter_than_its_bit_count_is_a_reported_failure(self):
        """main() reports each row's ValueError and moves on; an `assert` would
        escape that handler, and vanish under `python -O`."""
        key = next(key for key in evidence.ROUTES if key[1] == "ActiveBlinds")
        row = {"field_name": key[1], "raw_bits": b"\x02", "bit_count": 16}
        with self.assertRaisesRegex(ValueError, "bit_count"):
            evidence.inspect(row, evidence.ROUTES[key])

    def test_an_int_packed_fifth_byte_past_32_bits_is_refused(self):
        """Only four bits of the fifth byte fit in a u32, the limit vrf-bitio
        and validate_type_evidence enforce."""
        self.assertEqual(evidence.Bits(b"\xff\xff\xff\xff\x1e", 40).int_packed(), 0xFFFFFFFF)
        with self.assertRaisesRegex(ValueError, "IntPacked"):
            evidence.Bits(b"\xff\xff\xff\xff\x20", 40).int_packed()

    def test_typed_comparison_fails_if_children_disappear_or_go_null(self):
        key = next(key for key in evidence.ROUTES if "NetworkedProjectilePath" in key[1])
        row = path_point()
        row.update({"time_ms": 1, "packet_id": 2, "channel_index": 3,
                    "actor_net_guid": 4, "object_net_guid": 5, "group_path": key[0]})
        _, _, expected = evidence.inspect(row, evidence.ROUTES[key])
        with self.assertRaisesRegex(ValueError, "emitted children"):
            evidence.compare_children(row, expected, [])
        emitted = []
        for name, (handle, width, raw, column, value) in expected.items():
            child = {key: row[key] for key in (
                "time_ms", "packet_id", "channel_index", "actor_net_guid", "object_net_guid", "group_path")}
            child.update({"field_name": name, "handle": 0, "compatible_checksum": None,
                          "bit_count": width, "raw_bits": raw, "value_i64": None,
                          "value_f64": None, "value_bool": None, "value_str": None})
            if column == "value_str" and isinstance(value, tuple):
                child[column] = "(" + ",".join(str(v) for v in value) + ")"
            else:
                child[column] = value
            emitted.append(child)
        evidence.compare_children(row, expected, emitted)
        emitted[0]["value_f64"] = None
        with self.assertRaisesRegex(ValueError, "null typed value"):
            evidence.compare_children(row, expected, emitted)


PATH_KEY = next(key for key in evidence.ROUTES if "NetworkedProjectilePath" in key[1])
CONTEXT = {"time_ms": 1, "packet_id": 2, "channel_index": 3, "actor_net_guid": 4, "object_net_guid": 5}


def export_rows(parent):
    """A path parent preceded by its emitted children, as vrfkit writes them."""
    _, _, expected = evidence.inspect(parent, evidence.ROUTES[PATH_KEY])
    rows = []
    for name, (_handle, width, raw, column, value) in expected.items():
        child = {"field_name": name, "handle": 0, "compatible_checksum": None,
                 "bit_count": width, "raw_bits": raw}
        child[column] = "(0,0,0)" if isinstance(value, tuple) else value
        rows.append(child)
    return rows + [{**parent, "handle": 1, "compatible_checksum": PATH_KEY[2]}]


def run_main(rows, declared=True, flags=("--compare-typed", "--require-routes")):
    """main() over one synthetic export: `(exit code, stdout)`."""
    with tempfile.TemporaryDirectory() as directory:
        export = Path(directory)
        groups = [{"path": evidence.PATH_GROUP, "fields": [
            {"handle": 0, "name": "NetworkedProjectilePath", "compatible_checksum": PATH_KEY[2]}]}]
        (export / "manifest.json").write_text(
            json.dumps({"net_field_export_groups": groups if declared else []}), encoding="utf-8")
        types = {"compatible_checksum": pa.uint32(), "bit_count": pa.uint32(), "raw_bits": pa.binary(),
                 "handle": pa.uint32(), "field_name": pa.string(), "value_i64": pa.int64(),
                 "value_f64": pa.float64(), "value_bool": pa.bool_(), "value_str": pa.string()}
        pq.write_table(pa.table({
            "group_path": [PATH_KEY[0]] * len(rows),
            **{name: [value] * len(rows) for name, value in CONTEXT.items()},
            **{name: pa.array([row.get(name) for row in rows], kind) for name, kind in types.items()},
        }), export / "fields.parquet")
        return run_cli(evidence.main, export, *flags)[:2]


class MainTests(unittest.TestCase):
    def test_children_pair_with_the_parent_after_them(self):
        rows = export_rows(path_point())
        code, out = run_main(rows)
        self.assertIn("rows={'ActiveBlinds': 0, 'MulticastSetPath.NetworkedProjectilePath': 1} "
                      "elements={'MulticastSetPath.NetworkedProjectilePath': 1} "
                      "typed_children={'ActiveBlinds': 0, 'MulticastSetPath.NetworkedProjectilePath': 3}", out)
        self.assertIn("missing observed route", out)  # --require-routes: no ActiveBlinds row
        self.assertEqual(code, 1)
        self.assertEqual(run_main(rows, flags=["--compare-typed"])[0], 0)
        self.assertNotIn("orphan", out)
        self.assertNotIn("declaration", out)

    def test_an_orphan_child_and_a_missing_declaration_fail(self):
        rows = export_rows(path_point())
        code, out = run_main(rows + rows[:1], declared=False)
        self.assertEqual(code, 1)
        self.assertIn("orphan children: {'ActiveBlinds': 0, 'MulticastSetPath.NetworkedProjectilePath': 1}", out)
        self.assertIn(f"declaration mismatch: '{evidence.PATH_GROUP}'", out)

    def test_children_in_the_wrong_order_are_not_counted(self):
        rows = export_rows(path_point())
        code, out = run_main([rows[1], rows[0], *rows[2:]])
        self.assertEqual(code, 1)
        self.assertIn("child path", out)
        self.assertIn("typed_children={'ActiveBlinds': 0, 'MulticastSetPath.NetworkedProjectilePath': 0}", out)
