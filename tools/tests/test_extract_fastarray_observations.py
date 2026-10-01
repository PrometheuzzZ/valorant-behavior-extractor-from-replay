"""Wire boundaries and preserved failure evidence for numeric FastArray updates."""
import json
from pathlib import Path
import struct
import tempfile
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase, run_cli
import extract_fastarray_observations as fast
from wire_fixtures import packed

#: Route identities exactly as the parser exports them, literal rather than
#: read from the extractor: a misspelled group selects nothing and passes
#: every test that reads the extractor's own spelling.
CNC_H1 = ("AbilitiesAndBuffsComponent", "_cnc_h1")
CHAINED = ("/Script/ShooterGame.AresAbilitySystemComponent", "__vrfkit_chained_cnc_h1__")

#: Builds every route must keep rejecting: 13.07 stands for any future build;
#: the rest are malformed spellings of a measured one, because the gate
#: compares the manifest string exactly.
UNMEASURED_BUILDS = ("++Ares-Core+release-13.07", "++Ares-Core+release-11.05",
                     "13.06", "++Ares-Core+release-13.06 ", "")

METRICS = ("rows", "structural_exact", "rejected", "deleted_items", "changed_items", "raw_fields")

#: The exported column types, including the dictionary-encoded names.
NAME = pa.dictionary(pa.int32(), pa.string())
FIELD_SCHEMA = pa.schema([
    ("time_ms", pa.uint32()), ("packet_id", pa.uint32()), ("channel_index", pa.uint32()),
    ("actor_net_guid", pa.uint32()), ("object_net_guid", pa.uint32()), ("group_path", NAME),
    ("handle", pa.uint32()), ("field_name", NAME), ("compatible_checksum", pa.uint32()),
    ("bit_count", pa.uint32()), ("raw_bits", pa.binary())])
CHECKPOINT_SCHEMA = pa.schema([("checkpoint_index", pa.uint32()), ("checkpoint_id", pa.string()),
                               *FIELD_SCHEMA])


def payload(deleted=(), changed=(), keys=(8, 5)):
    body = bytearray(struct.pack("<iiii", *keys, len(deleted), len(changed)))
    for ident in deleted:
        body += struct.pack("<i", ident)
    for ident, fields in changed:
        body += struct.pack("<i", ident)
        for handle, raw in fields:
            body += packed(handle + 1) + packed(len(raw) * 8) + raw
        body += b"\0"
    # Shift the byte-aligned body to insert its single leading support bit.
    return (1 | (int.from_bytes(body, "little") << 1)).to_bytes(len(body) + 1, "little"), len(body) * 8 + 1


def field_row(identity, raw, count, handle=1):
    group, name = identity
    return {"time_ms": 10, "packet_id": 2, "channel_index": 3, "actor_net_guid": 4,
            "object_net_guid": 5, "group_path": group, "handle": handle, "field_name": name,
            "compatible_checksum": None, "bit_count": count, "raw_bits": raw}


def write_export(root, build, fields=(), checkpoint=()):
    source = root / "export"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"replay_build": build}), encoding="utf-8")
    pq.write_table(pa.Table.from_pylist(list(fields), schema=FIELD_SCHEMA), source / "fields.parquet")
    rows = [dict(row, checkpoint_index=0, checkpoint_id="cp0") for row in checkpoint]
    pq.write_table(pa.Table.from_pylist(rows, schema=CHECKPOINT_SCHEMA), source / "checkpoint_fields.parquet")
    return source


def records(out):
    return [json.loads(line) for line in (out / "observations.ndjson").read_text(encoding="utf-8").splitlines()]


def run(source, out):
    return run_cli(fast.main, "--export-dir", source, "--out-dir", out, prog="extract")[:2]


class FastArrayTests(TempDirTestCase):
    def test_changed_fields_and_deleted_ids_exact_raw_offsets(self):
        raw, count = payload([1, -3], [(5, [(0, b"\x96\xab"), (18, b""), (130, b"\xfe")])])
        result = fast.decode(raw, count)
        self.assertEqual(result["deleted_item_ids"], [1, -3])
        self.assertEqual((result["array_replication_key"], result["base_replication_key"]), (8, 5))
        self.assertEqual(result["consumed_bits"], count)
        item = result["changed_items"][0]
        self.assertEqual(item["item_id"], 5)
        self.assertEqual([f["handle"] for f in item["fields"]], [0, 18, 130])
        decoded = [(int.from_bytes(raw, "little") >> f["bit_offset"]) & ((1 << f["bit_count"]) - 1) for f in item["fields"]]
        self.assertEqual(decoded, [0xab96, 0, 254])
        self.assertEqual([f["bit_count"] for f in item["fields"]], [16, 0, 8])

    def test_deletion_only_and_signed_keys(self):
        raw, count = payload([1, 3], keys=(-1, -2))
        result = fast.decode(raw, count)
        self.assertEqual(result["array_replication_key"], -1)
        self.assertEqual(result["deleted_item_ids"], [1, 3])
        self.assertEqual(result["changed_items"], [])

    def test_each_truncation_is_rejected(self):
        raw, count = payload([1], [(3, [(2, b"\x81\x12")])])
        for width in range(count):
            with self.subTest(width=width), self.assertRaises(fast.WireError):
                fast.decode(raw[:(width + 7) // 8], width)

    def test_suffix_and_count_mutations_are_rejected(self):
        raw, count = payload([1, 3])
        with self.assertRaisesRegex(fast.WireError, "unconsumed_suffix"):
            fast.decode(raw, count + 1)
        bits = int.from_bytes(raw, "little")
        # Replace NumDeletes (bits 65..96) with -1 or an impossible positive.
        for value, reason in [(0xffffffff, "negative_count"), (0x7fffffff, "count_bounds")]:
            altered = ((bits & ~(0xffffffff << 65)) | (value << 65)).to_bytes(len(raw), "little")
            with self.assertRaisesRegex(fast.WireError, reason):
                fast.decode(altered, count)

    def test_unreal_packed_overflow_and_termination(self):
        self.assertEqual(fast.Bits(bytes.fromhex("ffffffff1e"), 40).packed(), 0xffffffff)
        self.assertEqual(fast.Bits(bytes.fromhex("171e"), 16).packed(), 1931)
        for raw, reason in [("ffffffff20", "packed_overflow"), ("0101010101", "packed_unterminated")]:
            with self.assertRaisesRegex(fast.WireError, reason):
                fast.Bits(bytes.fromhex(raw), 40).packed()

    def test_unknown_support_mode_and_bad_window(self):
        raw, count = payload()
        with self.assertRaisesRegex(fast.WireError, "unsupported_support_bit"):
            fast.decode(bytes([raw[0] & 254]) + raw[1:], count)
        for bad, size in [(None, count), (raw[:-1], count), (raw, -1)]:
            with self.assertRaises(fast.WireError):
                fast.decode(bad, size)

    def test_identity_and_unvalidated_context_stay_raw(self):
        raw, count = payload([1, 3])
        row = {"group_path": CNC_H1[0], "field_name": CNC_H1[1], "handle": 1,
               "raw_bits": raw, "bit_count": count}
        for population, build, handle, reason in [
            ("fields", "future", 1, "unvalidated_build"),
            ("checkpoint_fields", "++Ares-Core+release-13.05", 1, "unvalidated_checkpoint_route"),
            ("fields", "++Ares-Core+release-13.05", 2, "route_identity")]:
            record = fast.observation(dict(row, handle=handle), 27, population, build)
            self.assertEqual(record["status"], reason)
            self.assertEqual(record["raw_bits_hex"], raw.hex())
            self.assertEqual(record["physical_row_ordinal"], 27)
            self.assertIsNone(record["structure"])

    def test_routes_are_the_exported_identities(self):
        self.assertEqual(fast.ROUTES, {"cnc_h1": CNC_H1, "chained_cnc_h1": CHAINED})
        self.assertEqual(fast.ROUTE_HANDLE, 1)

    def test_every_measured_build_decodes_on_its_route_and_stream(self):
        raw, count = payload([1, 3], [(5, [(0, b"\x96\xab")])])
        builds = set().union(*fast.ACCEPTED_BUILDS.values())
        for (route, stream), accepted in fast.ACCEPTED_BUILDS.items():
            identity = CNC_H1 if route == "cnc_h1" else CHAINED
            row = {"group_path": identity[0], "field_name": identity[1], "handle": 1,
                   "raw_bits": raw, "bit_count": count}
            for build in sorted(builds):
                with self.subTest(route=route, stream=stream, build=build):
                    record = fast.observation(dict(row), 3, stream, build)
                    self.assertEqual((record["route"], record["population"]), (route, stream))
                    if build in accepted:
                        self.assertEqual(record["status"], "structural_exact")
                        self.assertEqual(record["structure"]["deleted_item_ids"], [1, 3])
                    else:
                        # 12.10/12.11 on cnc_h1 main; every build on cnc_h1 checkpoint.
                        self.assertEqual(record["status"], "unvalidated_build" if accepted
                                         else "unvalidated_checkpoint_route")
                        self.assertIsNone(record["structure"])

    def test_unmeasured_builds_are_rejected_with_raw_bits_kept(self):
        # A body that decodes exactly: only the build can reject it.
        raw, count = payload([1, 3], [(5, [(0, b"\x96\xab")])])
        self.assertEqual(fast.decode(raw, count)["consumed_bits"], count)
        for identity, stream in ((CNC_H1, "fields"), (CHAINED, "fields"), (CHAINED, "checkpoint_fields")):
            row = {"group_path": identity[0], "field_name": identity[1], "handle": 1,
                   "raw_bits": raw, "bit_count": count}
            for build in UNMEASURED_BUILDS:
                with self.subTest(identity=identity, stream=stream, build=build):
                    record = fast.observation(dict(row), 4, stream, build)
                    self.assertEqual(record["status"], "unvalidated_build")
                    self.assertIsNone(record["structure"])
                    self.assertEqual(record["raw_bits_hex"], raw.hex())

    def test_chained_rows_are_selected_and_decoded_in_both_streams(self):
        # Literal identities and only extract(): a misspelled route fails here.
        raw, count = payload([1, 3], [(5, [(0, b"\x96\xab"), (18, b"")])])
        chained = field_row(CHAINED, raw, count)
        root = self.tmp()
        source = write_export(root, "++Ares-Core+release-12.10",
                              fields=[field_row(("unrelated", "x"), raw, count), chained],
                              checkpoint=[chained])
        receipt = fast.extract(source, root / "result")
        counts = receipt["counts"]
        self.assertEqual((counts["fields_rows"], counts["checkpoint_fields_rows"]), (1, 1))
        self.assertEqual((counts["rows"], counts["structural_exact"], counts["rejected"]), (2, 2, 0))
        self.assertEqual(receipt["rejection_reasons"], {})
        for stream in ("fields", "checkpoint_fields"):
            self.assertEqual({m: counts[f"chained_cnc_h1.{stream}.{m}"] for m in METRICS},
                             {"rows": 1, "structural_exact": 1, "rejected": 0,
                              "deleted_items": 2, "changed_items": 1, "raw_fields": 2})
        got = records(root / "result")
        self.assertEqual([(r["route"], r["population"], r["physical_row_ordinal"], r["status"]) for r in got],
                         [("chained_cnc_h1", "fields", 1, "structural_exact"),
                          ("chained_cnc_h1", "checkpoint_fields", 0, "structural_exact")])
        self.assertEqual([r["raw_bits_hex"] for r in got], [raw.hex(), raw.hex()])
        self.assertEqual(got[1]["identity"]["checkpoint_id"], "cp0")
        self.assertEqual(got[0]["structure"]["changed_items"][0]["fields"][1]["handle"], 18)

    def test_only_exact_route_pairs_are_selected(self):
        raw, count = payload([1, 3])
        near_misses = [
            field_row((CNC_H1[0], CHAINED[1]), raw, count),  # a cross product of two routes' halves
            field_row((CHAINED[0], CNC_H1[1]), raw, count),
            field_row(("AbilitiesAndBuffsComponent_ClassNetCache", CNC_H1[1]), raw, count),
            field_row((CHAINED[0], "OwnerActor"), raw, count),  # not a route name, so not counted
        ]
        root = self.tmp()
        source = write_export(root, "++Ares-Core+release-13.05", fields=near_misses,
                              checkpoint=near_misses[:1])
        receipt = fast.extract(source, root / "result")
        counts = receipt["counts"]
        self.assertEqual((counts["rows"], counts["rejected"]), (0, 0))
        self.assertEqual(counts["unselected_route_name.fields.rows"], 3)
        self.assertEqual(counts["unselected_route_name.checkpoint_fields.rows"], 1)
        self.assertEqual(records(root / "result"), [])

    def test_route_rows_with_another_handle_are_rejected_not_dropped(self):
        raw, count = payload([1, 3])
        wrong = field_row(CHAINED, raw, count, handle=2)
        root = self.tmp()
        source = write_export(root, "++Ares-Core+release-13.05", fields=[wrong], checkpoint=[wrong])
        receipt = fast.extract(source, root / "result")
        self.assertEqual(receipt["rejection_reasons"], {"route_identity": 2})
        self.assertEqual((receipt["counts"]["chained_cnc_h1.fields.rejected"],
                          receipt["counts"]["chained_cnc_h1.checkpoint_fields.rejected"]), (1, 1))
        self.assertEqual([r["raw_bits_hex"] for r in records(root / "result")], [raw.hex()] * 2)

    def test_receipt_counts_every_route_and_stream_even_when_zero(self):
        raw, count = payload([1, 3], [(5, [(0, b"\x96\xab"), (18, b"")])])
        root = self.tmp(); out = root / "result"
        source = write_export(root, "++Ares-Core+release-13.05", fields=[field_row(CNC_H1, raw, count)])
        code, printed = run(source, out)
        self.assertEqual(code, 0)
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        counts = receipt["counts"]
        self.assertEqual(json.loads(printed), counts)
        self.assertEqual(set(counts), {
            *METRICS, "fields_rows", "checkpoint_fields_rows",
            *(f"{route}.{stream}.{metric}" for route in ("cnc_h1", "chained_cnc_h1")
              for stream in ("fields", "checkpoint_fields") for metric in METRICS),
            "unselected_route_name.fields.rows", "unselected_route_name.checkpoint_fields.rows"})
        self.assertEqual({k: v for k, v in counts.items() if v}, {
            "rows": 1, "structural_exact": 1, "deleted_items": 2, "changed_items": 1, "raw_fields": 2,
            "fields_rows": 1, "cnc_h1.fields.rows": 1, "cnc_h1.fields.structural_exact": 1,
            "cnc_h1.fields.deleted_items": 2, "cnc_h1.fields.changed_items": 1,
            "cnc_h1.fields.raw_fields": 2})
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["routes"], {
            "cnc_h1": {"group_path": CNC_H1[0], "field_name": CNC_H1[1], "handle": 1},
            "chained_cnc_h1": {"group_path": CHAINED[0], "field_name": CHAINED[1], "handle": 1}})

    def test_cli_exit_follows_the_build_gate(self):
        raw, count = payload([1, 3])
        cases = [
            (CNC_H1, "fields", "++Ares-Core+release-11.06", None),
            (CNC_H1, "fields", "++Ares-Core+release-13.06", None),
            (CNC_H1, "fields", "++Ares-Core+release-12.10", "unvalidated_build"),
            (CNC_H1, "fields", "++Ares-Core+release-13.07", "unvalidated_build"),
            (CNC_H1, "checkpoint_fields", "++Ares-Core+release-13.05", "unvalidated_checkpoint_route"),
            (CHAINED, "fields", "++Ares-Core+release-12.10", None),
            (CHAINED, "checkpoint_fields", "++Ares-Core+release-12.11", None),
            (CHAINED, "checkpoint_fields", "++Ares-Core+release-13.07", "unvalidated_build"),
        ]
        for identity, stream, build, reason in cases:
            with self.subTest(identity=identity, stream=stream, build=build), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root / "result"
                rows = [field_row(identity, raw, count)]
                source = write_export(root, build, **{"fields" if stream == "fields" else "checkpoint": rows})
                code, printed = run(source, out)
                self.assertEqual(code, 1 if reason else 0)
                receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
                self.assertEqual(json.loads(printed), receipt["counts"])
                self.assertEqual(receipt["replay_build"], build)
                self.assertEqual((receipt["counts"]["rows"], receipt["counts"][f"{stream}_rows"]), (1, 1))
                self.assertEqual(receipt["counts"]["rejected"], 1 if reason else 0)
                self.assertEqual(receipt["rejection_reasons"], {reason: 1} if reason else {})

    def test_cli_artifact_receipts_and_physical_ordinal(self):
        raw, count = payload([1, 3])
        root = self.tmp(); out = root / "result"
        source = write_export(root, "++Ares-Core+release-13.05",
                              fields=[field_row(("unrelated", "x"), raw, count), field_row(CNC_H1, raw, count)])
        receipt = fast.extract(source, out)
        self.assertEqual(receipt["counts"]["structural_exact"], 1)
        self.assertEqual(receipt["counts"]["rejected"], 0)
        self.assertEqual(receipt["input_sha256_before"], receipt["input_sha256_after"])
        self.assertEqual(receipt["observations_sha256"], fast.sha(out / "observations.ndjson"))
        self.assertEqual(receipt["wire_bits_sha256"], fast.sha(Path(fast.__file__).with_name("wire_bits.py")))
        record = json.loads((out / "observations.ndjson").read_text(encoding="utf-8"))
        self.assertEqual(record["physical_row_ordinal"], 1)
        self.assertEqual((record["route"], record["population"]), ("cnc_h1", "fields"))
        self.assertEqual(record["structure"]["deleted_item_ids"], [1, 3])
        with self.assertRaisesRegex(ValueError, "already exists"):
            fast.extract(source, out)
        with self.assertRaisesRegex(ValueError, "outside"):
            fast.extract(source, source / "forbidden")

    def test_the_receipt_is_written_with_lf_line_endings(self):
        """LF on every platform, like observations.ndjson (not CRLF on Windows)."""
        raw, count = payload([1, 3])
        root = self.tmp(); out = root / "result"
        source = write_export(root, "++Ares-Core+release-13.05", fields=[field_row(CNC_H1, raw, count)])
        fast.extract(source, out)
        data = (out / "receipt.json").read_bytes()
        self.assertIn(b"\n", data)
        self.assertNotIn(b"\r\n", data)

    def test_cli_rejections_retained_and_nonzero_exit(self):
        raw, count = payload([1, 3])
        root = self.tmp(); out = root / "result"
        source = write_export(root, "++Ares-Core+release-13.05", fields=[field_row(CNC_H1, raw, count + 1)])
        code, _ = run(source, out)
        self.assertEqual(code, 1)
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["counts"]["rejected"], 1)
        self.assertEqual(receipt["counts"]["cnc_h1.fields.rejected"], 1)
        record = json.loads((out / "observations.ndjson").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "unconsumed_suffix")
        self.assertIsNotNone(record["raw_bits_hex"])

    def test_changed_input_does_not_publish(self):
        raw, count = payload([1, 3])
        root = self.tmp(); out = root / "result"
        source = write_export(root, "++Ares-Core+release-13.05", fields=[field_row(CNC_H1, raw, count)])
        original = fast.selected_rows
        def alter(path, checkpoint):
            yield from original(path, checkpoint)
            if checkpoint:
                with (source / "manifest.json").open("a", encoding="utf-8") as handle:
                    handle.write(" ")
        with patch.object(fast, "selected_rows", alter), self.assertRaisesRegex(ValueError, "changed during read"):
            fast.extract(source, out)
        self.assertFalse(out.exists())
        self.assertEqual(list(root.glob(".fastarray-*")), [])
