"""Prioritize physical field rows whose typed overlay is wholly absent.

An inventory of raw/untyped wire rows, not a decoder or a semantic claim. A
row is untyped only when all four ``value_*`` columns are null (0, ``False``
and "" are typed); every ``*_bit_sum`` sums their declared ``bit_count``, not
payload values. Inputs are exports or parents of exports, read-only; the
output directory gets a summary and a deterministic JSON catalog, with main
and checkpoint entries apart because they are different decode paths.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from atomic_io import atomic_write_text
from export_scan import discover_exports, skipped_report


VALUE_COLUMNS = ("value_i64", "value_f64", "value_bool", "value_str")
REQUIRED_COLUMNS = ("group_path", "field_name", "compatible_checksum", "bit_count", "raw_bits", *VALUE_COLUMNS)
TABLES = ("fields", "checkpoint_fields")
BATCH_SIZE = 65_536
AGGREGATE_COLUMNS = (
    "row_count", "declared_bit_sum", "raw_present_rows", "preserved_raw_rows",
    "preserved_raw_bit_sum", "missing_raw_nonempty_rows", "missing_declared_bit_sum",
    "zero_bit_marker_rows", "wrong_raw_length_rows",
)
#: Each table's totals: row counts, then the untyped rows' aggregates.
TOTALS = ("physical_rows", "typed_rows", "untyped_rows", *(f"untyped_{name}" for name in AGGREGATE_COLUMNS[1:]))
#: A catalog key: text and checksum stored with an is-null flag, so a null
#: never collides with a literal "" or 0.
KEY_COLUMNS = {"table_name": "TEXT", "replay_build": "TEXT", "group_path": "TEXT", "group_is_null": "INTEGER",
               "field_name": "TEXT", "field_is_null": "INTEGER", "checksum_key": "INTEGER",
               "checksum_is_null": "INTEGER"}
GROUP_KEY = ", ".join(KEY_COLUMNS)
#: Adds a shard's or batch's aggregates to an existing catalog key.
MERGE_AGGREGATES = (f"ON CONFLICT({GROUP_KEY}) DO UPDATE SET "
                    + ", ".join(f"{name} = {name} + excluded.{name}" for name in AGGREGATE_COLUMNS))


#: Applies to every catalog entry: a key sums physical rows, not distinct facts.
RECURRENCE_NOTE = "physical occurrences are retained; this does not assert duplicate facts"


class InputError(ValueError):
    """An input cannot support an honest raw/untyped inventory."""


#: Export directories named directly, or the direct child exports of a parent.
discover = partial(discover_exports, error=InputError,
                   no_children="no direct child exports containing fields.parquet in")


def _build_and_manifest_sha(directory: Path) -> tuple[str, str]:
    path = directory / "manifest.json"
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
        build = manifest["replay_build"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InputError(f"{directory}: cannot read manifest replay_build: {exc}") from exc
    if not isinstance(build, str) or not build:
        raise InputError(f"{directory}: manifest replay_build must be a non-empty string")
    return build, hashlib.sha256(raw).hexdigest()


def _require_schema(parquet: pq.ParquetFile, path: Path) -> None:
    missing = set(REQUIRED_COLUMNS) - set(parquet.schema_arrow.names)
    if missing:
        raise InputError(f"{path}: missing required columns {sorted(missing)}")
    bit_count = parquet.schema_arrow.field("bit_count")
    if bit_count.type != pa.uint32():
        raise InputError(f"{path}: bit_count must be uint32, got {bit_count.type}")


def _typed_mask(batch: pa.RecordBatch) -> pa.Array:
    populated = pc.is_valid(batch.column(batch.schema.get_field_index(VALUE_COLUMNS[0])))
    for name in VALUE_COLUMNS[1:]:
        populated = pc.or_(populated, pc.is_valid(batch.column(batch.schema.get_field_index(name))))
    return pc.fill_null(populated, False)


def _scalar_sum(values: pa.Array) -> int:
    return int(pc.sum(values).as_py() or 0)


def _text_key(value: str | None) -> tuple[str, int]:
    """Store nullable text without colliding with any literal text value."""
    return ("", 1) if value is None else (value, 0)


def _uint_key(value: int | None) -> tuple[int, int]:
    return (0, 1) if value is None else (int(value), 0)


def _upsert_groups(connection: sqlite3.Connection, table: str, build: str, export_id: str,
                   grouped: pa.Table) -> None:
    rows = []
    for item in grouped.to_pylist():
        group_path, group_is_null = _text_key(item["group_path"])
        field_name, field_is_null = _text_key(item["field_name"])
        checksum, checksum_is_null = _uint_key(item["compatible_checksum"])
        rows.append((table, build, group_path, group_is_null, field_name, field_is_null, checksum, checksum_is_null,
                     *(int(item[name]) for name in AGGREGATE_COLUMNS), export_id))
    connection.executemany(
        f"""INSERT INTO groups({GROUP_KEY}, {', '.join(AGGREGATE_COLUMNS)})
            VALUES ({', '.join('?' * (len(KEY_COLUMNS) + len(AGGREGATE_COLUMNS)))}) {MERGE_AGGREGATES}""",
        [row[:-1] for row in rows],
    )
    connection.executemany(f"INSERT OR IGNORE INTO group_files({GROUP_KEY}, export_id) VALUES ({', '.join('?' * 9)})",
                           [(*row[:8], row[-1]) for row in rows])


def _scan_table(connection: sqlite3.Connection, directory: Path, export_id: str,
                build: str, table: str) -> dict[str, int]:
    path = directory / f"{table}.parquet"
    with path.open("rb") as source:
        parquet = pq.ParquetFile(source)
        _require_schema(parquet, path)
        totals = defaultdict(int)
        columns = list(REQUIRED_COLUMNS)
        for batch in parquet.iter_batches(batch_size=BATCH_SIZE, columns=columns, use_threads=False):
            typed = _typed_mask(batch)
            untyped = pc.invert(typed)
            totals["physical_rows"] += batch.num_rows
            totals["typed_rows"] += _scalar_sum(pc.cast(typed, pa.int64()))
            totals["untyped_rows"] += _scalar_sum(pc.cast(untyped, pa.int64()))
            bits = batch.column(batch.schema.get_field_index("bit_count"))
            if pc.any(pc.is_null(bits)).as_py():
                raise InputError(f"{path}: bit_count contains null values")
            totals["untyped_declared_bit_sum"] += _scalar_sum(pc.filter(bits, untyped))
            raw = batch.column(batch.schema.get_field_index("raw_bits"))
            # Widen before adding seven: a valid uint32 bit_count can be
            # UINT32_MAX, and uint32 addition would wrap to a plausible zero.
            bit_count_u64 = pc.cast(bits, pa.uint64())
            expected_bytes = pc.divide_checked(pc.add(bit_count_u64, pa.scalar(7, pa.uint64())), pa.scalar(8, pa.uint64()))
            raw_bytes_u64 = pc.cast(pc.binary_length(raw), pa.uint64())
            valid_raw = pc.and_(pc.is_valid(raw), pc.equal(raw_bytes_u64, expected_bytes))
            raw_present = pc.is_valid(raw)
            bit_is_zero = pc.equal(bits, pa.scalar(0, bits.type))
            raw_is_empty = pc.fill_null(pc.equal(pc.binary_length(raw), pa.scalar(0)), False)
            zero_marker = pc.and_(bit_is_zero, pc.or_(pc.invert(raw_present), raw_is_empty))
            missing_nonempty = pc.and_(pc.invert(raw_present), pc.invert(bit_is_zero))
            preserved = pc.and_(pc.fill_null(valid_raw, False), pc.invert(bit_is_zero))
            wrong_length = pc.and_(raw_present, pc.invert(pc.fill_null(valid_raw, False)))
            for name, mask in (("raw_present_rows", raw_present), ("preserved_raw_rows", preserved),
                               ("missing_raw_nonempty_rows", missing_nonempty),
                               ("zero_bit_marker_rows", zero_marker), ("wrong_raw_length_rows", wrong_length)):
                totals[f"untyped_{name}"] += _scalar_sum(pc.cast(pc.and_(untyped, mask), pa.int64()))
            totals["untyped_preserved_raw_bit_sum"] += _scalar_sum(pc.filter(bits, pc.and_(untyped, preserved)))
            totals["untyped_missing_declared_bit_sum"] += _scalar_sum(pc.filter(bits, pc.and_(untyped, missing_nonempty)))
            selected = pa.Table.from_arrays([
                batch.column(batch.schema.get_field_index(name)) for name in ("group_path", "field_name", "compatible_checksum", "bit_count")
            ] + [
                pc.cast(raw_present, pa.int64()), pc.cast(preserved, pa.int64()),
                pc.cast(missing_nonempty, pa.int64()), pc.cast(zero_marker, pa.int64()), pc.cast(wrong_length, pa.int64()),
                pc.if_else(missing_nonempty, pc.cast(bits, pa.int64()), pa.scalar(0, pa.int64())),
                pc.if_else(preserved, pc.cast(bits, pa.int64()), pa.scalar(0, pa.int64())),
            ], names=["group_path", "field_name", "compatible_checksum", "bit_count", "raw_present_rows",
                        "preserved_raw_rows", "missing_raw_nonempty_rows", "zero_bit_marker_rows",
                        "wrong_raw_length_rows", "missing_declared_bit_sum", "preserved_raw_bit_sum"]).filter(untyped)
            if selected.num_rows:
                grouped = selected.group_by(["group_path", "field_name", "compatible_checksum"]).aggregate(
                    [("bit_count", "count"), ("bit_count", "sum"), *((name, "sum") for name in AGGREGATE_COLUMNS[2:])]
                ).rename_columns(["group_path", "field_name", "compatible_checksum", *AGGREGATE_COLUMNS])
                _upsert_groups(connection, table, build, export_id, grouped)
                del grouped
        if totals["physical_rows"] != parquet.metadata.num_rows:
            raise InputError(f"{path}: scanned rows disagree with Parquet footer")
    return dict(totals)


def _database(connection: sqlite3.Connection) -> None:
    # A high-cardinality catalog: SQLite's page cache stays bounded, and temp
    # sort/index pages spill to the per-run directory, not process memory.
    connection.execute("PRAGMA cache_size = -32768")
    connection.execute("PRAGMA temp_store = FILE")
    key = ", ".join(f"{name} {kind} NOT NULL" for name, kind in KEY_COLUMNS.items())
    aggregates = ", ".join(f"{name} INTEGER NOT NULL" for name in AGGREGATE_COLUMNS)
    connection.executescript(
        f"""CREATE TABLE groups ({key}, {aggregates}, PRIMARY KEY({GROUP_KEY}));
            CREATE TABLE group_files ({key}, export_id TEXT NOT NULL, PRIMARY KEY({GROUP_KEY}, export_id));"""
    )


def _catalog(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        f"""SELECT {GROUP_KEY}, {', '.join(AGGREGATE_COLUMNS)} FROM groups
            ORDER BY preserved_raw_bit_sum DESC, preserved_raw_rows DESC, row_count DESC, {GROUP_KEY}"""
    )
    match = " AND ".join(f"{name} = ?" for name in KEY_COLUMNS)
    catalog = []
    for row in rows:
        key = dict(zip(KEY_COLUMNS, row))
        files = [item[0] for item in connection.execute(
            f"SELECT export_id FROM group_files WHERE {match} ORDER BY export_id", row[:len(KEY_COLUMNS)])]
        entry = {
            "table": key["table_name"], "replay_build": key["replay_build"],
            "group_path": None if key["group_is_null"] else key["group_path"],
            "field_name": None if key["field_is_null"] else key["field_name"],
            "compatible_checksum": None if key["checksum_is_null"] else key["checksum_key"],
            **dict(zip(AGGREGATE_COLUMNS, row[len(KEY_COLUMNS):])),
            "impacted_file_count": len(files), "impacted_export_ids": files,
        }
        entry["physical_untyped_rows"] = entry.pop("row_count")
        catalog.append(entry)
    return catalog


def _scan_export(directory: Path, shard: Path) -> tuple[dict | None, dict | None]:
    """Scan one export into its own disk-backed shard, safe for a worker thread."""
    connection = sqlite3.connect(shard)
    try:
        _database(connection)
        build, manifest_sha = _build_and_manifest_sha(directory)
        export_id = str(directory.resolve())
        record = {"export_id": export_id, "path": export_id, "replay_build": build,
                  "manifest_sha256": manifest_sha, "tables": {}}
        for table in TABLES:
            path = directory / f"{table}.parquet"
            if not path.exists():
                if table == "fields":
                    raise InputError(f"{directory}: required fields.parquet is missing")
                record["tables"][table] = {"present": False}
                continue
            counts = _scan_table(connection, directory, export_id, build, table)
            record["tables"][table] = {"present": True, "parquet_size_bytes": path.stat().st_size,
                                       "physical_rows": counts["physical_rows"], "counts": counts}
        connection.commit()
        return record, None
    except (OSError, pa.ArrowException, sqlite3.Error, InputError, ValueError) as exc:
        return None, {"export": str(directory), "error": str(exc)}
    finally:
        connection.close()


def _merge_shard(connection: sqlite3.Connection, path: Path, index: int) -> None:
    alias = f"shard_{index}"
    connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))
    try:
        group_rows = connection.execute(f"SELECT * FROM {alias}.groups")
        statement = (f"INSERT INTO groups VALUES ({', '.join('?' * (len(KEY_COLUMNS) + len(AGGREGATE_COLUMNS)))}) "
                     + MERGE_AGGREGATES)
        while batch := group_rows.fetchmany(10_000):
            connection.executemany(statement, batch)
        group_rows.close()
        file_rows = connection.execute(f"SELECT * FROM {alias}.group_files")
        while batch := file_rows.fetchmany(10_000):
            connection.executemany("INSERT OR IGNORE INTO group_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
        file_rows.close()
        connection.commit()
    finally:
        connection.execute(f"DETACH DATABASE {alias}")


def summarize(exports: list[Path], jobs: int = 1, top: int = 25) -> tuple[dict, list[dict]]:
    if jobs < 1 or jobs > 16:
        raise InputError("jobs must be between 1 and 16")
    if top < 1:
        raise InputError("top must be positive")
    totals = {name: defaultdict(int) for name in TABLES}
    provenance = []
    errors = []
    with tempfile.TemporaryDirectory(prefix="vrfkit-raw-priority-") as temp:
        shard_paths = [Path(temp) / f"shard-{index}.sqlite" for index in range(len(exports))]
        results: list[tuple[dict | None, dict | None]] = [(None, None)] * len(exports)
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            pending = {pool.submit(_scan_export, directory, shard_paths[index]): index
                       for index, directory in enumerate(exports)}
            for future in as_completed(pending):
                results[pending[future]] = future.result()
        connection = sqlite3.connect(Path(temp) / "aggregate.sqlite")
        try:
            _database(connection)
            for index, (record, error) in enumerate(results):
                if error is not None:
                    errors.append(error)
                    continue
                assert record is not None
                _merge_shard(connection, shard_paths[index], index)
                provenance.append(record)
                for table, detail in record["tables"].items():
                    for key, value in detail.get("counts", {}).items():
                        totals[table][key] += value
            connection.commit()
            catalog = _catalog(connection)
        finally:
            connection.close()
    table_summary = {}
    for table in TABLES:
        counts = {name: 0 for name in TOTALS} | totals[table]
        counts["untyped_fraction"] = counts["untyped_rows"] / counts["physical_rows"] if counts["physical_rows"] else None
        counts["catalog_keys"] = sum(item["table"] == table for item in catalog)
        table_summary[table] = counts
    report = {
        "schema_version": 2, "complete": not errors, "export_count": len(exports),
        "successful_exports": len(provenance), "failed_exports": len(errors),
        "classification": "untyped means all value_i64/value_f64/value_bool/value_str are null; declared bits and preserved raw payload are separate, and raw equality is never used as a fact",
        "movement_marker_note": "Current stream.rs routes successfully decoded MOVEMENT_RPC batches into movement.parquet and leaves their fields raw_bits null; this explains one observed class only and does not excuse other missing raw payloads.",
        "nested_raw_parent_caveat": "a raw parent payload can contain nested structure; a catalog key prioritizes preserved wire rows and does not prove that the parent is one unresolved semantic field",
        "duplicate_state": "physical row occurrences are summed exactly; recurrence and impacted files are reported separately and do not deduplicate or assert duplicate game facts",
        "recurrence_note": RECURRENCE_NOTE,
        "tables": table_summary, "top_preserved_raw": catalog[:top],
        "top_missing_declared_bits": sorted(catalog, key=lambda item: (-item["missing_declared_bit_sum"], -item["missing_raw_nonempty_rows"], item["table"], item["replay_build"], item["group_path"] or "", item["field_name"] or ""))[:top],
        "errors": sorted(errors, key=lambda item: item["export"]),
        "provenance": sorted(provenance, key=lambda item: item["export_id"]),
    }
    return report, catalog


def _write_json(path: Path, document: object) -> None:
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--jobs", type=int, default=4, help="parallel export scanners (1..16), each with a bounded SQLite shard")
    args = parser.parse_args(argv)
    try:
        skipped: list[Path] = []
        exports = discover(args.inputs, skipped)
        report, catalog = summarize(exports, args.jobs, args.top)
        report["skipped_generated_dirs"] = skipped_report(skipped)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(args.output_dir / "raw_untyped_summary.json", report)
        _write_json(args.output_dir / "raw_untyped_catalog.json",
                    {"schema_version": 2, "recurrence_note": RECURRENCE_NOTE, "entries": catalog})
    except (OSError, InputError, ValueError, pa.ArrowException) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"complete": report["complete"], "summary": str(args.output_dir / "raw_untyped_summary.json"),
                      "catalog": str(args.output_dir / "raw_untyped_catalog.json")}, sort_keys=True))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
