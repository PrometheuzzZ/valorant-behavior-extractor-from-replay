"""Tests for the identifier-redacted raw-property corpus inventory."""
from __future__ import annotations

import io
import json
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


from support import TempDirTestCase
import analyze_raw_properties as raw_inventory


SCHEMA = pa.schema(
    [
        pa.field("packet_id", pa.uint32(), nullable=False),
        pa.field("channel_index", pa.uint32(), nullable=False),
        pa.field("actor_net_guid", pa.uint32(), nullable=False),
        pa.field("object_net_guid", pa.uint32(), nullable=True),
        pa.field("group_path", pa.string(), nullable=False),
        pa.field("handle", pa.uint32(), nullable=False),
        pa.field("field_name", pa.string(), nullable=True),
        pa.field("compatible_checksum", pa.uint32(), nullable=True),
        pa.field("bit_count", pa.uint32(), nullable=False),
        pa.field("raw_bits", pa.binary(), nullable=True),
        pa.field("value_i64", pa.int64(), nullable=True),
        pa.field("value_f64", pa.float64(), nullable=True),
        pa.field("value_bool", pa.bool_(), nullable=True),
        pa.field("value_str", pa.string(), nullable=True),
    ]
)


def row(
    *,
    packet: int,
    group: str,
    handle: int,
    name: str | None,
    bits: int,
    raw: bytes | None,
    value_i64: int | None = None,
) -> dict:
    return {
        "packet_id": packet,
        "channel_index": 2,
        "actor_net_guid": 3,
        "object_net_guid": 4,
        "group_path": group,
        "handle": handle,
        "field_name": name,
        "compatible_checksum": None,
        "bit_count": bits,
        "raw_bits": raw,
        "value_i64": value_i64,
        "value_f64": None,
        "value_bool": None,
        "value_str": None,
    }


class AnalyzeExportTests(TempDirTestCase):
    def _write_export(self, root: Path, rows: list[dict]) -> None:
        (root / "manifest.json").write_text(
            json.dumps({"replay_build": "++Ares-Core+release-13.04"}),
            encoding="utf-8",
        )
        pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), root / "fields.parquet")

    def test_counts_only_unnamed_replicated_properties(self):
        rows = [
            row(packet=1, group="/private/group", handle=1, name="Typed", bits=8,
                raw=b"\x05", value_i64=5),
            row(packet=2, group="/private/group", handle=2, name="NamedRaw", bits=8,
                raw=b"\x06"),
            row(packet=3, group="/private/group", handle=3, name=None, bits=3,
                raw=b"\x00"),
            row(packet=4, group="/private/group", handle=3, name=None, bits=3,
                raw=b"\x05"),
            # RPC rows are not replicated properties and must not enter this tally.
            row(packet=5, group="/private/Rpc_ClassNetCache", handle=4, name=None,
                bits=8, raw=b"\xff"),
        ]
        root = self.tmp()
        self._write_export(root, rows)
        inventory = raw_inventory.Inventory()
        build = raw_inventory.analyze_export(root, inventory, replay_ordinal=1)

        self.assertEqual(build, "13.04")
        self.assertEqual(inventory.field_rows[build], 5)
        self.assertEqual(inventory.property_rows[build], 4)
        self.assertEqual(inventory.property_raw_only_rows[build], 3)
        self.assertEqual(inventory.named_raw_only_rows[build], 1)
        self.assertEqual(inventory.unnamed_rows[build], 2)
        self.assertEqual(inventory.unnamed_raw_rows[build], 2)
        self.assertEqual(inventory.unnamed_zero_rows[build], 1)
        self.assertEqual(inventory.unnamed_checksum_rows[build], 0)
        self.assertEqual(inventory.unnamed_sentinel_handle_rows[build], 0)
        self.assertEqual(inventory.unnamed_wrong_length_rows[build], 0)
        self.assertEqual(inventory.unnamed_widths[build], Counter({3: 2}))
        self.assertEqual(len(inventory.signatures), 1)
        recurrence = next(iter(inventory.signatures.values()))
        self.assertEqual(recurrence.rows, 2)
        self.assertEqual(recurrence.rows_by_build, Counter({"13.04": 2}))
        self.assertTrue(recurrence.payload_varied)

    def test_missing_raw_bits_is_a_visible_integrity_failure(self):
        rows = [
            row(packet=1, group="/private/group", handle=1, name=None, bits=0, raw=None)
        ]
        root = self.tmp()
        self._write_export(root, rows)
        inventory = raw_inventory.Inventory()
        raw_inventory.analyze_export(root, inventory, replay_ordinal=1)

        self.assertEqual(inventory.integrity_failures, 1)
        self.assertEqual(inventory.unnamed_raw_rows["13.04"], 0)

    def test_wrong_raw_byte_length_is_a_visible_integrity_failure(self):
        rows = [
            row(packet=1, group="/private/group", handle=1, name=None,
                bits=9, raw=b"\x01")
        ]
        root = self.tmp()
        self._write_export(root, rows)
        inventory = raw_inventory.Inventory()
        raw_inventory.analyze_export(root, inventory, replay_ordinal=1)

        self.assertEqual(inventory.integrity_failures, 1)
        self.assertEqual(inventory.unnamed_wrong_length_rows["13.04"], 1)

    def test_report_cannot_expose_structural_identifiers(self):
        private_group = "/private/account-derived/group"
        rows = [
            row(packet=991, group=private_group, handle=987654, name=None,
                bits=24, raw=b"\x01\x02\x03")
        ]
        root = self.tmp()
        self._write_export(root, rows)
        inventory = raw_inventory.Inventory()
        raw_inventory.analyze_export(root, inventory, replay_ordinal=1)

        report = raw_inventory.render_report(
            inventory,
            eligible_by_build=Counter({"13.04": 1}),
            selected_by_build=Counter({"13.04": 1}),
            excluded=0,
            recursive=False,
        )
        self.assertNotIn(private_group, report)
        self.assertNotIn("987654", report)
        self.assertNotIn("991", report)
        self.assertNotIn("010203", report)
        self.assertIn("24:1", report)

    def test_report_text_is_pinned(self):
        """Every line of the text report, from three exports over two builds.

        The fixture reaches each counter with a nonzero value, ties in the
        width and layout rankings (so their order is pinned too), an
        eligible build with nothing analyzed and an integrity failure; the
        empty report pins the other branch of every conditional line.
        """
        group, rpc = "/private/group", "/private/Rpc_ClassNetCache"
        exports = [
            ("13.04", [
                row(packet=1, group=group, handle=1, name="Typed", bits=8,
                    raw=b"\x05", value_i64=5),
                row(packet=1, group=group, handle=2, name="NamedRaw", bits=8, raw=b"\x06"),
                row(packet=2, group=group, handle=3, name=None, bits=3, raw=b"\x00"),
                row(packet=3, group=group, handle=3, name=None, bits=3, raw=b"\x05"),
                row(packet=3, group=group, handle=4, name=None, bits=16, raw=b"\x01\x02"),
                {**row(packet=4, group=group, handle=2**32 - 1, name=None, bits=8,
                       raw=b"\x07"), "compatible_checksum": 77},
                row(packet=5, group=rpc, handle=4, name=None, bits=8, raw=b"\xff"),
            ]),
            ("13.04", [row(packet=7, group=group, handle=3, name=None, bits=3, raw=b"\x05")]),
            ("13.05", [
                row(packet=9, group=group, handle=3, name=None, bits=3, raw=b"\x05"),
                row(packet=9, group=group, handle=5, name=None, bits=9, raw=b"\x01"),
                row(packet=10, group=group, handle=6, name=None, bits=8, raw=None),
                row(packet=11, group=group, handle=7, name=None, bits=8, raw=b"\x03",
                    value_i64=3),
            ]),
        ]
        inventory = raw_inventory.Inventory()
        td = self.tmp()
        for ordinal, (build, rows) in enumerate(exports, 1):
            root = td / str(ordinal)
            root.mkdir()
            (root / "manifest.json").write_text(
                json.dumps({"replay_build": f"++Ares-Core+release-{build}"}),
                encoding="utf-8",
            )
            pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA),
                           root / "fields.parquet")
            raw_inventory.analyze_export(root, inventory, replay_ordinal=ordinal)

        report = raw_inventory.render_report(
            inventory,
            eligible_by_build=Counter({"13.02": 2, "13.04": 2, "13.05": 1}),
            selected_by_build=Counter({"13.04": 2, "13.05": 1}),
            excluded=3,
            recursive=False,
        )
        self.assertEqual(report.splitlines(), [
            "=== Raw replicated-property inventory (identifier-redacted) ===",
            "corpus scope: 5 eligible replay(s); 3 replay(s) excluded by non-recursive discovery",
            "release-13.02: 2 eligible, 0 analyzed",
            "release-13.04: 2 eligible, 2 analyzed",
            "release-13.05: 1 eligible, 1 analyzed",
            "",
            "=== release-13.04 aggregate ===",
            "replays: 2",
            "field rows: 8",
            "replicated-property rows: 7",
            "raw-only property rows: 6 (85.71%)",
            "named raw-only / unnamed rows: 1 / 5",
            "unnamed rows preserving raw_bits: 5 (100.00%)",
            "unnamed typed / missing raw_bits: 0 / 0",
            "unnamed raw_bits with wrong byte length: 0",
            "unnamed rows with compatible checksum / sentinel handle: 1 / 1",
            "unnamed zero / nonzero payload rows: 1 / 4",
            "unnamed byte-aligned / non-byte-aligned rows: 2 / 3",
            "top unnamed widths (bits:rows): 3:3, 16:1, 8:1",
            "",
            "=== release-13.05 aggregate ===",
            "replays: 1",
            "field rows: 4",
            "replicated-property rows: 4",
            "raw-only property rows: 2 (50.00%)",
            "named raw-only / unnamed rows: 0 / 4",
            "unnamed rows preserving raw_bits: 3 (75.00%)",
            "unnamed typed / missing raw_bits: 1 / 1",
            "unnamed raw_bits with wrong byte length: 1",
            "unnamed rows with compatible checksum / sentinel handle: 0 / 0",
            "unnamed zero / nonzero payload rows: 0 / 3",
            "unnamed byte-aligned / non-byte-aligned rows: 1 / 2",
            "top unnamed widths (bits:rows): 3:1, 9:1, 8:1",
            "",
            "=== Anonymous structural recurrence ===",
            "field signatures: 5",
            "rows in repeated signatures: 4",
            "signatures seen in multiple replays / builds: 1 / 1",
            "repeated signatures with constant / varying payload: 0 / 1",
            "property updates containing unnamed rows: 6",
            "distinct unnamed layouts: 5",
            "updates in repeated layouts: 2",
            "layouts seen in multiple replays / builds: 1 / 0",
            "per-build structural reuse:",
            "  release-13.04: 3 signatures; 3/5 rows use a cross-build signature; "
            "3 layouts; 0/4 updates use a cross-build layout",
            "  release-13.05: 3 signatures; 1/3 rows use a cross-build signature; "
            "2 layouts; 0/2 updates use a cross-build layout",
            "top anonymous layouts (rank:updates,fields,replays,builds):",
            "  1:2,1,2,1",
            "  2:1,2,1,1",
            "  3:1,1,1,1",
            "  4:1,2,1,1",
            "  5:1,1,1,1",
            "",
            "integrity: FAIL -- unnamed property payload preservation violations: 2",
            "typing note: recurrence is structural evidence only; no field name or "
            "type is inferred.",
        ])

        empty = raw_inventory.render_report(
            raw_inventory.Inventory(),
            eligible_by_build=Counter({"13.04": 0}),
            selected_by_build=Counter(),
            excluded=0,
            recursive=True,
        )
        self.assertEqual(empty.splitlines(), [
            "=== Raw replicated-property inventory (identifier-redacted) ===",
            "corpus scope: 0 eligible replay(s); recursive",
            "release-13.04: 0 eligible, 0 analyzed",
            "",
            "=== Anonymous structural recurrence ===",
            "field signatures: 0",
            "rows in repeated signatures: 0",
            "signatures seen in multiple replays / builds: 0 / 0",
            "repeated signatures with constant / varying payload: 0 / 0",
            "property updates containing unnamed rows: 0",
            "distinct unnamed layouts: 0",
            "updates in repeated layouts: 0",
            "layouts seen in multiple replays / builds: 0 / 0",
            "per-build structural reuse:",
            "top anonymous layouts (rank:updates,fields,replays,builds):",
            "  none",
            "",
            "integrity: PASS -- every unnamed property row preserved exact-length raw_bits",
            "typing note: recurrence is structural evidence only; no field name or "
            "type is inferred.",
        ])

    def test_json_summary_is_versioned_and_cannot_expose_identifiers(self):
        private_group = "/private/account-derived/group"
        rows = [
            row(packet=991, group=private_group, handle=987654, name=None,
                bits=24, raw=b"\x01\x02\x03")
        ]
        root = self.tmp()
        self._write_export(root, rows)
        inventory = raw_inventory.Inventory()
        raw_inventory.analyze_export(root, inventory, replay_ordinal=1)

        document = raw_inventory.summary_document(
            inventory,
            eligible_by_build=Counter({"13.04": 1}),
            selected_by_build=Counter({"13.04": 1}),
            excluded=0,
            recursive=False,
        )
        encoded = json.dumps(document, sort_keys=True)
        self.assertEqual(document["schema_version"], 1)
        self.assertTrue(document["identifier_redacted"])
        self.assertFalse(document["typing_inference_performed"])
        self.assertNotIn(private_group, encoded)
        self.assertNotIn("987654", encoded)
        self.assertNotIn("991", encoded)
        self.assertNotIn("010203", encoded)
        self.assertEqual(
            document["builds"]["13.04"]["unnamed_widths_bits"], {"24": 1}
        )


class SamplingTests(unittest.TestCase):
    def test_sample_spans_size_range_without_duplicates(self):
        candidates = [
            raw_inventory.ReplayCandidate(Path(f"private-{i}.vrf"), "13.04", i)
            for i in range(10)
        ]
        sampled = raw_inventory.stratified_sample(candidates, 4)
        self.assertEqual([item.size for item in sampled], [0, 3, 6, 9])
        self.assertEqual(len({item.path for item in sampled}), 4)

    def test_single_sample_selects_median_size(self):
        candidates = [
            raw_inventory.ReplayCandidate(Path(f"private-{i}.vrf"), "13.02", i)
            for i in range(5)
        ]
        sampled = raw_inventory.stratified_sample(candidates, 1)
        self.assertEqual(sampled[0].size, 2)


class BuildTests(unittest.TestCase):
    def test_parse_build_ignores_other_version_numbers(self):
        text = "Replay version: 5.3.2\nBranch: ++Ares-Core+release-13.04"
        self.assertEqual(raw_inventory.parse_build(text), "13.04")

    def test_parse_build_requires_release_label(self):
        self.assertIsNone(raw_inventory.parse_build("Replay version: 5.3.2"))

    def test_cli_rejects_non_numeric_build_label_before_it_can_be_printed(self):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit):
            raw_inventory.parse_args(
                ["vrfkit", "corpus", "--build", "/private/replay-name"]
            )
        self.assertNotIn("/private/replay-name", error.getvalue())

    def test_build_help_states_the_default_builds(self):
        """--help names the builds a default run samples."""
        printed = io.StringIO()
        with redirect_stdout(printed), self.assertRaises(SystemExit):
            raw_inventory.parse_args(["--help"])
        text = " ".join(printed.getvalue().split())
        self.assertIn(f"(default: the newest {raw_inventory.DEFAULT_BUILD_COUNT} builds found)", text)

    def test_the_default_builds_are_the_newest_by_number(self):
        """Compared as text, 9.10 would outrank 13.06 and 13.2 would outrank 13.10."""
        labels = ["13.06", "9.10", "12.10", "13.05", "11.06", "13.06", "10.01"]
        self.assertEqual(raw_inventory.newest_builds(labels), ("12.10", "13.05", "13.06"))
        self.assertEqual(raw_inventory.newest_builds(["13.10", "13.2", "13.06"], 2), ("13.06", "13.10"))
