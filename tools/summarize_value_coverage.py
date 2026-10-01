"""Count physical typed rows in exports.

Inputs are exports or parents of exports, read-only; the JSON report goes to
stdout. A typed row has at least one non-null value_i64/f64/bool/str, 0,
False and "" included; several populated columns count once and are
reported separately. Not a percentage of game facts understood.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from export_scan import discover_exports, skipped_report

VALUE_COLUMNS = ("value_i64", "value_f64", "value_bool", "value_str")
TABLES = ("fields", "checkpoint_fields")

#: Export directories named directly, or the direct child exports of a parent.
discover = discover_exports


def count_table(path: Path) -> dict[str, int]:
    with path.open("rb") as source:
        parquet = pq.ParquetFile(source)
        missing = set(VALUE_COLUMNS) - set(parquet.schema_arrow.names)
        if missing:
            raise ValueError(f"{path.name}: missing value columns {sorted(missing)}")
        rows = typed = multi = 0
        for batch in parquet.iter_batches(batch_size=65536, columns=list(VALUE_COLUMNS), use_threads=False):
            populated = pc.cast(pc.is_valid(batch.column(0)), pa.uint8())
            for index in range(1, len(VALUE_COLUMNS)):
                populated = pc.add(populated, pc.cast(pc.is_valid(batch.column(index)), pa.uint8()))
            rows += batch.num_rows
            typed += pc.sum(pc.greater(populated, 0)).as_py() or 0
            multi += pc.sum(pc.greater(populated, 1)).as_py() or 0
        if rows != parquet.metadata.num_rows:
            raise ValueError(f"{path.name}: scanned rows disagree with footer")
    return {"rows": rows, "typed_rows": typed, "untyped_rows": rows - typed, "multi_value_rows": multi}


def count_export(directory: Path) -> dict[str, dict[str, int]]:
    if not (directory / "manifest.json").is_file():
        raise ValueError(f"{directory}: no manifest.json -- not a finished export")
    result = {"fields": count_table(directory / "fields.parquet")}
    checkpoint = directory / "checkpoint_fields.parquet"
    if checkpoint.exists():
        result["checkpoint_fields"] = count_table(checkpoint)
    return result


def summarize(exports: list[Path], jobs: int) -> dict:
    if jobs < 1:
        raise ValueError("jobs must be positive")
    totals = {name: {"exports_with_table": 0, "rows": 0, "typed_rows": 0,
                     "untyped_rows": 0, "multi_value_rows": 0} for name in TABLES}
    errors = []
    successful = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = {pool.submit(count_export, p): p for p in exports}
        for future in as_completed(pending):
            try:
                measured = future.result()
            except (OSError, ValueError, pa.ArrowException) as exc:
                errors.append({"export": str(pending[future]), "error": str(exc)})
                continue
            successful += 1
            for name, counts in measured.items():
                totals[name]["exports_with_table"] += 1
                for key, value in counts.items():
                    totals[name][key] += value
    for counts in totals.values():
        counts["typed_fraction"] = counts["typed_rows"] / counts["rows"] if counts["rows"] else None
    return {"schema_version": 1, "complete": not errors, "export_count": len(exports), "successful_exports": successful,
            "denominator": "physical field rows in successfully read export directories",
            "interpretation": "non-null typed values, not semantic verification, raw preservation, or framing coverage",
            "tables": totals, "errors": sorted(errors, key=lambda e: e["export"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        skipped: list[Path] = []
        report = summarize(discover(args.inputs, skipped), args.jobs)
        report["skipped_generated_dirs"] = skipped_report(skipped)
    except ValueError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
