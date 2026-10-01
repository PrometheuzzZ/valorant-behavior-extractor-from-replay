"""Focused behavior checks for the raw/untyped priority catalog."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


from support import TempDirTestCase
import summarize_unresolved_fields as priority


SCHEMA = pa.schema([
    pa.field("group_path", pa.string(), nullable=False), pa.field("field_name", pa.string()),
    pa.field("compatible_checksum", pa.uint32()), pa.field("bit_count", pa.uint32(), nullable=False),
    pa.field("raw_bits", pa.binary()), pa.field("value_i64", pa.int64()), pa.field("value_f64", pa.float64()),
    pa.field("value_bool", pa.bool_()), pa.field("value_str", pa.string()),
])


def field(group: str, name: str | None, checksum: int | None, bits: int, raw: bytes | None, **values: object) -> dict:
    result = {"group_path": group, "field_name": name, "compatible_checksum": checksum, "bit_count": bits,
              "raw_bits": raw, "value_i64": None, "value_f64": None, "value_bool": None, "value_str": None}
    result.update(values)
    return result


class RawPriorityTests(TempDirTestCase):
    def write_export(self, directory: Path, rows: list[dict], checkpoint: list[dict] | None = None, build: str = "13.05") -> None:
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({"replay_build": build}), encoding="utf-8")
        pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), directory / "fields.parquet")
        if checkpoint is not None:
            pq.write_table(pa.Table.from_pylist(checkpoint, schema=SCHEMA), directory / "checkpoint_fields.parquet")

    def test_only_all_null_values_are_untyped_and_keys_keep_width_checksum_and_nulls(self):
        rows = [
            field("/A", "zero", 7, 8, b"\0", value_i64=0),
            field("/A", "false", 7, 8, b"\0", value_bool=False),
            field("/A", "raw", 7, 8, b"\x01"),
            field("/A", "raw", 7, 9, b"\x01\0"),
            field("/A", None, None, 3, b"\x05"),
            field("/A", None, None, 3, b"\x01"),
        ]
        td = self.tmp()
        root = td / "one"
        self.write_export(root, rows, checkpoint=[field("/A", "raw", 7, 4, b"\x03")])
        report, catalog = priority.summarize(priority.discover([root]))
        self.assertEqual(report["tables"]["fields"]["physical_rows"], 6)
        self.assertEqual(report["tables"]["fields"]["typed_rows"], 2)
        self.assertEqual(report["tables"]["fields"]["untyped_rows"], 4)
        self.assertEqual(report["tables"]["fields"]["untyped_declared_bit_sum"], 23)
        self.assertEqual(report["tables"]["fields"]["untyped_preserved_raw_bit_sum"], 23)
        raw = [entry for entry in catalog if entry["table"] == "fields" and entry["field_name"] == "raw"]
        self.assertEqual([(entry["physical_untyped_rows"], entry["declared_bit_sum"]) for entry in raw], [(2, 17)])
        unnamed = next(entry for entry in catalog if entry["table"] == "fields" and entry["field_name"] is None)
        self.assertEqual((unnamed["compatible_checksum"], unnamed["physical_untyped_rows"], unnamed["declared_bit_sum"]), (None, 2, 6))
        self.assertEqual(report["tables"]["checkpoint_fields"]["untyped_rows"], 1)

    def test_repeated_physical_rows_and_impacted_files_are_separate(self):
        rows = [field("/A", "raw", 9, 8, b"\x01"), field("/A", "raw", 9, 8, b"\x02")]
        parent = self.tmp()
        self.write_export(parent / "one", rows)
        self.write_export(parent / "two", rows)
        _report, catalog = priority.summarize(priority.discover([parent]))
        entry = next(item for item in catalog if item["table"] == "fields")
        self.assertEqual((entry["physical_untyped_rows"], entry["preserved_raw_bit_sum"], entry["impacted_file_count"]), (4, 32, 2))
        self.assertEqual([Path(item).name for item in entry["impacted_export_ids"]], ["one", "two"])

    def test_literal_nul_and_null_key_remain_distinct(self):
        td = self.tmp()
        root = td / "one"
        self.write_export(root, [field("/A", "", 0, 1, b"\0"), field("/A", None, None, 1, b"\0")])
        _report, catalog = priority.summarize([root], jobs=2)
        self.assertEqual({(item["field_name"], item["compatible_checksum"]) for item in catalog}, {("", 0), (None, None)})

    def test_bit_count_must_be_uint32_and_non_null(self):
        td = self.tmp()
        root = td / "bad"
        root.mkdir()
        (root / "manifest.json").write_text('{"replay_build":"13.05"}', encoding="utf-8")
        fields = list(SCHEMA)
        fields[3] = pa.field("bit_count", pa.int64())
        bad = pa.schema(fields)
        pq.write_table(pa.Table.from_pylist([field("/A", "raw", None, 1, b"\0")], schema=bad), root / "fields.parquet")
        report, _catalog = priority.summarize([root])
        self.assertFalse(report["complete"])
        self.assertIn("bit_count must be uint32", report["errors"][0]["error"])

    def test_export_leftovers_beside_an_export_are_not_counted(self):
        """A `previous` sibling carries its own manifest, so only discovery
        can skip it."""
        rows = [field("/A", "raw", 9, 8, b"\x01")]
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as out:
            parent = Path(td)
            self.write_export(parent / "pub2", rows)
            self.write_export(parent / ".pub2.vrfkit-previous-4242-7", rows)
            staging = parent / ".pub2.vrfkit-staging-55396-0"
            staging.mkdir()
            (staging / "fields.parquet").write_bytes(b"PAR1 no footer")
            expected_skipped = [str((parent / name).resolve()) for name in (
                ".pub2.vrfkit-previous-4242-7", ".pub2.vrfkit-staging-55396-0")]

            self.assertEqual(priority.discover([parent]), [(parent / "pub2").resolve()])
            code = priority.main([str(parent), "--output-dir", out, "--jobs", "1"])
            summary = json.loads((Path(out) / "raw_untyped_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(summary["export_count"], 1)
        self.assertEqual(summary["tables"]["fields"]["physical_rows"], 1)
        self.assertEqual(summary["skipped_generated_dirs"], expected_skipped)

    def test_both_reports_are_replaced_atomically_with_lf_line_endings(self):
        """Through atomic_io, LF on every platform (not CRLF on Windows)."""
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as out:
            parent = Path(td)
            self.write_export(parent / "one", [field("/A", "raw", 9, 8, b"\x01")])
            code = priority.main([str(parent), "--output-dir", out, "--jobs", "1"])
            names = sorted(path.name for path in Path(out).iterdir())
            reports = {name: (Path(out) / name).read_bytes() for name in names}
        self.assertEqual(code, 0)
        self.assertEqual(names, ["raw_untyped_catalog.json", "raw_untyped_summary.json"])
        for name, data in reports.items():
            with self.subTest(name=name):
                self.assertIn(b"\n", data)
                self.assertNotIn(b"\r\n", data)

    def test_a_parent_holding_only_leftovers_is_an_error_that_counts_them(self):
        parent = self.tmp()
        self.write_export(parent / ".pub2.vrfkit-previous-4242-7", [field("/A", "raw", 9, 8, b"\x01")])
        with self.assertRaisesRegex(priority.InputError, r"no direct child exports.*1 "):
            priority.discover([parent])

    def test_missing_fields_or_required_schema_is_an_error(self):
        td = self.tmp()
        root = td / "bad"
        root.mkdir()
        (root / "manifest.json").write_text('{"replay_build":"13.05"}', encoding="utf-8")
        pq.write_table(pa.table({"group_path": ["/A"]}), root / "fields.parquet")
        report, _catalog = priority.summarize([root])
        self.assertFalse(report["complete"])
        self.assertEqual(report["failed_exports"], 1)
        self.assertIn("missing required columns", report["errors"][0]["error"])
        with self.assertRaises(priority.InputError):
            priority.discover([td / "does-not-exist"])

    def test_bad_raw_byte_length_is_visible_without_reclassifying_the_row(self):
        td = self.tmp()
        root = td / "one"
        self.write_export(root, [field("/A", "bad", None, 9, b"\x01")])
        report, _catalog = priority.summarize([root])
        self.assertEqual(report["tables"]["fields"]["untyped_rows"], 1)
        self.assertEqual(report["tables"]["fields"]["untyped_wrong_raw_length_rows"], 1)

    def test_maximum_uint32_bit_count_cannot_wrap_to_a_zero_byte_payload(self):
        td = self.tmp()
        root = td / "one"
        self.write_export(root, [field("/A", "huge", None, (1 << 32) - 1, b"")])
        report, _catalog = priority.summarize([root])
        self.assertEqual(report["tables"]["fields"]["untyped_wrong_raw_length_rows"], 1)

    def test_missing_nonempty_and_zero_bit_markers_are_not_preserved_raw(self):
        rows = [field("/A", "missing", None, 8, None), field("/A", "zero-null", None, 0, None),
                field("/A", "zero-empty", None, 0, b""), field("/A", "preserved", None, 8, b"\x01")]
        td = self.tmp()
        root = td / "one"
        self.write_export(root, rows)
        report, catalog = priority.summarize([root])
        totals = report["tables"]["fields"]
        self.assertEqual((totals["untyped_declared_bit_sum"], totals["untyped_preserved_raw_bit_sum"]), (16, 8))
        self.assertEqual((totals["untyped_missing_raw_nonempty_rows"], totals["untyped_missing_declared_bit_sum"]), (1, 8))
        self.assertEqual(totals["untyped_zero_bit_marker_rows"], 2)
        self.assertEqual(report["top_preserved_raw"][0]["field_name"], "preserved")
        missing = next(item for item in catalog if item["field_name"] == "missing")
        self.assertEqual((missing["preserved_raw_bit_sum"], missing["missing_declared_bit_sum"]), (0, 8))
