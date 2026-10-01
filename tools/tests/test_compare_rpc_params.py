"""Guards for the RPC parameter comparison.

Both sides empty must not exit 0 (the shared `verdict` is tested in
test_compare_combat_report.py). The one expected difference (a 02d4d478
damage record only vrfkit emits; see docs/FOLLOWUP.md) is driven through the
real loaders over written files, and must not widen (another record, other
values, another packet, another replay, no manifest) or outlive what it
describes (STALE fails the run).
"""
import collections
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq


from support import TempDirTestCase, run_cli
import compare_rpc_params as guard


ONE_RPC = {"MulticastEndRound": [("NewRoundNumber", "int")]}


def records(values=None):
    """`ONE_RPC` records, one per value occurrence, at packets 1, 2, ..."""
    out = []
    for value, n in sorted((values or {}).items()):
        for _ in range(n):
            out.append(((len(out) + 1, 7, 7, 3, "MulticastEndRound"),
                        {"NewRoundNumber": value}))
    return out


def run(**kwargs):
    """`main`'s exit code and what it printed."""
    return run_cli(lambda _: guard.main(**kwargs))[:2]


class ExitCodeTests(unittest.TestCase):
    """No expected differences here, so no replay identity is needed."""

    def test_matching_data_exits_zero(self):
        code, _ = run(cs_records=records({1: 2}), rust_records=records({1: 2}),
                      rpcs=ONE_RPC, expected=())
        self.assertEqual(code, 0)

    def test_differing_data_exits_nonzero(self):
        code, _ = run(cs_records=records({1: 2}), rust_records=records({1: 1}),
                      rpcs=ONE_RPC, expected=())
        self.assertNotEqual(code, 0)

    def test_a_replay_with_none_of_these_rpcs_does_not_claim_a_match(self):
        code, _ = run(cs_records=[], rust_records=[], rpcs=ONE_RPC, expected=())
        self.assertNotEqual(code, 0)

    def test_one_parameter_missing_from_both_sides_is_not_a_pass(self):
        """A matching parameter must not carry the run past one nobody compared."""
        two = {"MulticastEndRound": [("NewRoundNumber", "int"), ("Other", "int")]}
        code, _ = run(cs_records=records({1: 2}), rust_records=records({1: 2}),
                      rpcs=two, expected=())
        self.assertEqual(code, 2)


class InputTests(unittest.TestCase):
    def test_a_missing_reference_exits_2_and_says_where_it_looked(self):
        missing = Path(__file__).with_name("no-such-reference.ndjson")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = guard.main(argv=["--reference", str(missing), "--ours", str(missing)])
        self.assertEqual(code, 2)
        self.assertIn(str(missing), err.getvalue())

    def test_the_default_reference_is_not_the_valplay_bundle(self):
        self.assertNotIn("valplay", guard.DEFAULT_REFERENCE.lower())

    def test_regional_damage_ordinals_follow_the_enum(self):
        """EAresRegionalDamage: RegionCount = 3, Invalid_Radial = 4."""
        self.assertEqual(guard.norm("regional_damage__region_count", "enum_byte"), 3)
        self.assertEqual(guard.norm("regional_damage__invalid__radial", "enum_byte"), 4)


# Files shaped like the real inputs.

LISTED = guard.EXPECTED_DIFFERENCES[0]
OTHER_REPLAY = "0" * 64

#: `(packet, actor, object or None, channel, function, {param: wire value})`.
#: None as the object is an RPC on the actor itself, which vrfkit writes with
#: a null object and the C# export with the actor's GUID.
COMMON = [
    (1000, 576, None, 10, "MulticastNotifyKilledEnemy",
     {"KillerCharacter": 576, "KilledCharacter": 1466, "MultikillLevel": 1}),
    (1001, 3704, 3706, 99, "MulticastNotifyDamage_Point",
     {"DamageDealt": 30.0, "DamageTaken": 20.0, "RegionalDamage": 0,
      "bDamageKilledTarget": True}),
    (1002, 5, None, 2, "MulticastEndRound", {"NewRoundNumber": 1}),
]


def listed(packet=LISTED.packet_id, **values):
    """The listed record as vrfkit writes it; `values` overrides a parameter."""
    params = {"DamageDealt": 29.447561264038086, "DamageTaken": 20.0,
              "RegionalDamage": 0, "bDamageKilledTarget": True}
    params.update(values)
    return (packet, LISTED.actor_net_guid, LISTED.object_net_guid,
            LISTED.channel, LISTED.function, params)


def write_reference(directory, rows, sha=LISTED.replay_sha256):
    """rpc_params.ndjson as the USAGE recipe greps it, and the export's manifest."""
    regional = {v: k for k, v in guard.REGIONAL_DAMAGE_MAP.items()}
    lines = []
    for packet, actor, obj, channel, func, params in rows:
        payload = {}
        for name, value in params.items():
            if name == "RegionalDamage":
                value = regional[value]
            payload[{"bDamageKilledTarget": "DamageKilledTarget"}.get(name, name)] = value
        lines.append(json.dumps({
            "type": "rpc_received", "packet_id": packet, "actor_net_guid": actor,
            "object_net_guid": actor if obj is None else obj, "channel": channel,
            "function_name": func, "payload": payload}))
    path = directory / "rpc_params.ndjson"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sha is not None:
        (directory / "manifest.json").write_text(
            json.dumps({"source_sha256": sha}), encoding="utf-8")
    return path


def write_ours(path, rows):
    """fields.parquet with vrfkit's column types, one row per parameter, plus a
    row nothing compares inside each record and an unrelated row between."""
    vtypes = {func: dict(params) for func, params in guard.RPCS_TO_CHECK.items()}
    cols = collections.defaultdict(list)

    def add(packet, actor, obj, channel, name, i64=None, f64=None, b=None):
        for column, value in (("packet_id", packet), ("channel_index", channel),
                              ("actor_net_guid", actor), ("object_net_guid", obj),
                              ("field_name", name), ("value_i64", i64),
                              ("value_f64", f64), ("value_bool", b),
                              ("value_str", None)):
            cols[column].append(value)

    for packet, actor, obj, channel, func, params in rows:
        for i, (name, value) in enumerate(params.items()):
            column = guard.RUST_VALUE_COLUMN[vtypes[func][name]]
            add(packet, actor, obj, channel, f"{func}.{name}",
                i64=value if column == "value_i64" else None,
                f64=value if column == "value_f64" else None,
                b=value if column == "value_bool" else None)
            if i == 0:
                add(packet, actor, obj, channel, f"{func}.EventInstigator", i64=198)
        add(packet + 1, actor, None, channel, "ReplicatedMovement")
    u32 = pa.uint32()
    pq.write_table(pa.table({
        "packet_id": pa.array(cols["packet_id"], u32),
        "channel_index": pa.array(cols["channel_index"], u32),
        "actor_net_guid": pa.array(cols["actor_net_guid"], u32),
        "object_net_guid": pa.array(cols["object_net_guid"], u32),
        "field_name": pa.array(cols["field_name"], pa.string()).dictionary_encode(),
        "value_i64": pa.array(cols["value_i64"], pa.int64()),
        "value_f64": pa.array(cols["value_f64"], pa.float64()),
        "value_bool": pa.array(cols["value_bool"], pa.bool_()),
        "value_str": pa.array(cols["value_str"], pa.string()).dictionary_encode(),
    }), path)
    return path


class FileTest(TempDirTestCase):
    def setUp(self):
        self.dir = self.tmp()

    def compare_files(self, reference_rows, our_rows, sha=LISTED.replay_sha256):
        reference = write_reference(self.dir, reference_rows, sha)
        ours = write_ours(self.dir / "fields.parquet", our_rows)
        return run(argv=["--reference", str(reference), "--ours", str(ours)])


class ExpectedDifferenceTests(FileTest):
    """The one listed record, and nothing wider."""

    def test_the_listed_record_is_excluded_and_the_run_passes(self):
        code, out = self.compare_files(COMMON, COMMON + [listed()])
        self.assertEqual(code, 0, out)
        self.assertIn("1 applied, 0 stale", out)
        self.assertIn("vrfkit-only records: 0", out)

    def test_another_vrfkit_only_record_still_fails(self):
        extra = (1003, 3704, 3706, 99, "MulticastNotifyDamage_Point",
                 {"DamageDealt": 29.45, "DamageTaken": 20.0, "RegionalDamage": 0,
                  "bDamageKilledTarget": True})
        code, out = self.compare_files(COMMON, COMMON + [listed(), extra])
        self.assertEqual(code, 1, out)
        self.assertIn("1 applied", out)
        self.assertIn("vrfkit-only records: 1", out)

    def test_the_listed_values_one_packet_away_are_not_excused(self):
        """Same actor, object, channel, function and values; wrong packet."""
        code, out = self.compare_files(COMMON, COMMON + [listed(packet=LISTED.packet_id + 1)])
        self.assertEqual(code, 1, out)
        self.assertIn("0 applied, 1 stale", out)
        self.assertIn("vrfkit-only records: 1", out)

    def test_the_listed_record_with_other_values_is_stale(self):
        code, out = self.compare_files(COMMON, COMMON + [listed(DamageDealt=30.0)])
        self.assertEqual(code, 1, out)
        self.assertIn("STALE", out)
        self.assertIn("0 applied", out)

    def test_the_record_reaching_the_reference_is_stale(self):
        """The C# side gaining the record: everything else matches, and it still fails."""
        code, out = self.compare_files(COMMON + [listed()], COMMON + [listed()])
        self.assertEqual(code, 1, out)
        self.assertIn("STALE", out)
        self.assertIn("vrfkit-only records: 0", out)
        self.assertIn("C#-only records: 0", out)
        self.assertIn("the C# export now has 1 record(s) there", out)

    def test_the_record_leaving_vrfkit_is_stale(self):
        code, out = self.compare_files(COMMON, COMMON)
        self.assertEqual(code, 1, out)
        self.assertIn("STALE", out)
        self.assertIn("vrfkit has 0 record(s) there", out)

    def test_two_records_at_the_listed_identity_are_stale(self):
        code, out = self.compare_files(COMMON, COMMON + [listed(), listed()])
        self.assertEqual(code, 1, out)
        self.assertIn("vrfkit has 2 record(s) there", out)

    def test_another_replay_does_not_apply_the_entry(self):
        code, out = self.compare_files(COMMON, COMMON + [listed()], sha=OTHER_REPLAY)
        self.assertEqual(code, 1, out)
        self.assertIn("0 applied, 0 stale, 1 not for this replay, 0 unchecked", out)
        self.assertIn("vrfkit-only records: 1", out)

    def test_no_manifest_means_an_unknown_replay_and_nothing_applied(self):
        code, out = self.compare_files(COMMON, COMMON + [listed()], sha=None)
        self.assertEqual(code, 1, out)
        self.assertIn("Replay: UNKNOWN", out)
        self.assertIn("0 applied, 0 stale, 0 not for this replay, 1 unchecked", out)

    def test_no_manifest_cannot_pass_where_staleness_would_hide(self):
        """Both sides have the record, so the entry is stale -- but without the
        replay nothing can say so. That must not read as agreement."""
        code, out = self.compare_files(COMMON + [listed()], COMMON + [listed()],
                                       sha=None)
        self.assertEqual(code, 2, out)
        self.assertIn("could not be checked", out)
        self.assertNotIn("MATCH (floats", out)

    def test_every_entry_lists_every_compared_parameter_normalised(self):
        """An entry missing a parameter could never match a loaded record, and
        an unnormalised value could never equal one."""
        for entry in guard.EXPECTED_DIFFERENCES:
            with self.subTest(entry=entry.describe()):
                spec = dict(guard.RPCS_TO_CHECK[entry.function])
                values = dict(entry.values)
                self.assertEqual(set(values), set(spec))
                for name, value in values.items():
                    self.assertEqual(guard.norm(value, spec[name]), value)
                self.assertTrue(entry.reason)


class RecordTests(FileTest):
    """The record check the expected difference is keyed on."""

    def test_a_record_moved_to_another_packet_fails_though_every_value_matches(self):
        moved = list(COMMON)
        moved[1] = (2001,) + moved[1][1:]
        code, out = self.compare_files(COMMON, moved + [listed()])
        self.assertEqual(code, 1, out)
        self.assertNotIn("DIFFER (", out)
        self.assertIn("vrfkit-only records: 1", out)
        self.assertIn("C#-only records: 1", out)

    def test_two_invocations_at_one_identity_are_two_records(self):
        rows = [COMMON[1], COMMON[1]]
        loaded = guard.load_rust_records(write_ours(self.dir / "f.parquet", rows))
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0], loaded[1])

    def test_an_actor_rpc_is_keyed_by_the_actor_on_both_sides(self):
        reference = write_reference(self.dir, [COMMON[0]])
        ours = write_ours(self.dir / "fields.parquet", [COMMON[0]])
        self.assertEqual(guard.load_cs_records(reference),
                         guard.load_rust_records(ours))
        self.assertEqual(guard.load_cs_records(reference)[0][0],
                         (1000, 576, 576, 10, "MulticastNotifyKilledEnemy"))
