"""Physical coverage counts rows once and never substitutes truthiness for null."""
import json

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase, run_cli
import summarize_value_coverage as coverage


class PhysicalCoverageTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()

    def write(self, directory, filename="fields.parquet"):
        directory.mkdir(exist_ok=True)
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
        pq.write_table(pa.table({
            "group_path": pa.array(["/Game/Test"] * 5),
            "field_name": pa.array(["ReviewedField"] * 5),
            "value_i64": pa.array([0, None, None, None, 7], type=pa.int64()),
            "value_f64": pa.array([None, None, None, None, 2.5], type=pa.float64()),
            "value_bool": pa.array([None, False, None, None, None], type=pa.bool_()),
            "value_str": pa.array([None, None, "", None, None], type=pa.string()),
        }), directory / filename)

    def test_null_union_and_checkpoint_denominators_are_independent(self):
        first, second = self.root / "first", self.root / "second"
        self.write(first)
        self.write(first, "checkpoint_fields.parquet")
        self.write(second)
        paths = coverage.discover([self.root, first])
        self.assertEqual(len(paths), 2)
        report = coverage.summarize(paths, 2)
        main = report["tables"]["fields"]
        cp = report["tables"]["checkpoint_fields"]
        self.assertTrue(report["complete"])
        self.assertEqual((main["rows"], main["typed_rows"], main["multi_value_rows"]), (10, 8, 2))
        self.assertEqual(main["typed_fraction"], 0.8)
        self.assertEqual((cp["exports_with_table"], cp["rows"], cp["typed_rows"]), (1, 5, 4))

    def test_export_leftovers_beside_an_export_are_not_counted(self):
        """`vrfkit export` siblings are not exports, and the report says so."""
        self.write(self.root / "pub2")
        self.write(self.root / ".pub2.vrfkit-previous-4242-7")
        staging = self.root / ".pub2.vrfkit-staging-55396-0"
        staging.mkdir()
        (staging / "fields.parquet").write_bytes(b"PAR1 no footer")
        expected_skipped = [str((self.root / name).resolve()) for name in (
            ".pub2.vrfkit-previous-4242-7", ".pub2.vrfkit-staging-55396-0")]

        self.assertEqual(coverage.discover([self.root]), [(self.root / "pub2").resolve()])
        code, output, _ = run_cli(coverage.main, self.root, "--jobs", "1")
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(report["export_count"], 1)
        self.assertEqual(report["tables"]["fields"]["rows"], 5)
        self.assertEqual(report["skipped_generated_dirs"], expected_skipped)

    def test_a_parent_holding_only_leftovers_is_an_error_that_counts_them(self):
        self.write(self.root / ".pub2.vrfkit-previous-4242-7")
        with self.assertRaisesRegex(ValueError, r"no direct child exports.*1 "):
            coverage.discover([self.root])

    def test_bad_export_is_explicit_and_cli_fails(self):
        self.write(self.root / "good")
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "fields.parquet").write_bytes(b"not parquet")
        code, output, _ = run_cli(coverage.main, self.root)
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["successful_exports"], 1)
        self.assertEqual(len(report["errors"]), 1)

    def test_a_table_without_a_manifest_is_a_failed_export(self):
        """vrfkit writes manifest.json last: without it the export may be a
        partial copy whose checkpoint table has not arrived yet."""
        self.write(self.root / "done")
        partial = self.root / "partial"
        self.write(partial)
        (partial / "manifest.json").unlink()
        code, output, _ = run_cli(coverage.main, self.root)
        report = json.loads(output)
        self.assertEqual((code, report["complete"], report["successful_exports"]), (1, False, 1))
        self.assertIn("manifest.json", report["errors"][0]["error"])

    def test_empty_table_has_unknown_fraction_and_schema_is_required(self):
        empty = self.root / "fields.parquet"
        (self.root / "manifest.json").write_text("{}", encoding="utf-8")
        pq.write_table(pa.table({name: pa.array([], type=pa.int64())
                                for name in coverage.VALUE_COLUMNS}), empty)
        self.assertEqual(coverage.count_table(empty)["rows"], 0)
        report = coverage.summarize([self.root], 1)
        self.assertTrue(report["complete"])
        self.assertIsNone(report["tables"]["fields"]["typed_fraction"])
        self.assertIsNone(report["tables"]["checkpoint_fields"]["typed_fraction"])
        pq.write_table(pa.table({"different": [1]}), empty)
        with self.assertRaisesRegex(ValueError, "missing value columns"):
            coverage.count_table(empty)
