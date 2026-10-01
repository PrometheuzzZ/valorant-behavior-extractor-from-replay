import contextlib, copy, io, json, struct
import pyarrow as pa, pyarrow.parquet as pq

from support import TempDirTestCase
import extract_healing_observations as tool
from wire_fixtures import (
    CHECKPOINT_FIELD_SCHEMA as CHECKPOINT_SCHEMA,
    FIELD_SCHEMA as SCHEMA,
    array,
    field_row,
    packed,
)


def ref(v):
    return packed(v), 8 * len(packed(v))


def row(name, crc=None, raw=b"", bits=0, **kw):
    return field_row(dict(time_ms=1000, packet_id=2, channel_index=3, actor_net_guid=40, object_net_guid=41,
                          group_path=tool.OUTER_GROUP, handle=6, field_name=name, compatible_checksum=crc,
                          bit_count=bits, raw_bits=raw), **kw)


def fixture(value=-0.0, causer=True):
    f = struct.pack("<f", value)
    cr, cw = ref(60)
    fields = [(2, cw, cr), (3, 32, f), (4, 32, f), (5, 1, b"\1")]
    parent, pw = array([(0, fields)])
    rows = [
        row("MulticastNotifyHeal.HealTaken", 1894010429, f, 32, value_f64=value),
        row(
            "MulticastNotifyHeal.LifeChangeBySection[0].ChangedComponent",
            None,
            cr,
            cw,
            value_i64=60,
        ),
        row(
            "MulticastNotifyHeal.LifeChangeBySection[0].LifeResult",
            None,
            f,
            32,
            value_f64=value,
        ),
        row(
            "MulticastNotifyHeal.LifeChangeBySection[0].DeltaLife",
            None,
            f,
            32,
            value_f64=value,
        ),
        row(
            "MulticastNotifyHeal.LifeChangeBySection[0].bAliveAfterChange",
            None,
            b"\1",
            1,
            value_bool=True,
        ),
        row("MulticastNotifyHeal.LifeChangeBySection", 163390906, parent, pw),
    ]
    # Typed as the parser types them: the packed ObjectNetGuid in value_i64.
    for n, h, c, v in [
        ("EventInstigator", 7, 3087885251, 12),
        ("EventInstigatorPawn", 8, 3901949544, 50),
    ]:
        z, w = ref(v)
        rows.append(row("MulticastNotifyHeal." + n, c, z, w, value_i64=v))
    if causer:
        z, w = ref(70)
        rows.append(
            row("MulticastNotifyHeal.HealCauser", 546618027, z, w, value_i64=70)
        )
    return rows


def manifest():
    return {
        "net_field_export_groups": [
            {
                "path": tool.OUTER_GROUP,
                "fields": [
                    {
                        "handle": 6,
                        "name": "MulticastNotifyHeal",
                        "compatible_checksum": 791426194,
                    }
                ],
            },
            {
                "path": tool.PARAM_GROUP,
                "fields": [
                    {"handle": h, "name": n, "compatible_checksum": c}
                    for h, (n, c) in tool.DECL.items()
                ],
            },
        ],
        "players": [
            {"character_net_guid": 40, "subject": "recipient"},
            {"character_net_guid": 50, "subject": "source"},
        ],
    }


class Tests(TempDirTestCase):
    def make(self, rows=None, actors=None, checkpoint=None):
        p = self.tmp()
        (p / "manifest.json").write_text(json.dumps(manifest()), encoding="utf-8")
        pq.write_table(
            pa.Table.from_pylist(rows or fixture(), schema=SCHEMA), p / "fields.parquet"
        )
        checkpoint_rows = [
            {"checkpoint_index": 0, "checkpoint_id": "cp-0", **item}
            for item in (checkpoint or [])
        ]
        pq.write_table(
            pa.Table.from_pylist(checkpoint_rows, schema=CHECKPOINT_SCHEMA),
            p / "checkpoint_fields.parquet",
        )
        actor_schema = pa.schema(
            [
                ("time_ms", pa.uint32()),
                ("packet_id", pa.uint32()),
                ("channel_index", pa.uint32()),
                ("actor_net_guid", pa.uint32()),
                ("event", pa.string()),
                ("class_path", pa.string()),
            ]
        )
        pq.write_table(
            pa.Table.from_pylist(actors or [], schema=actor_schema),
            p / "actors.parquet",
        )
        guid_schema = pa.schema([("net_guid", pa.uint32()), ("path", pa.string())])
        pq.write_table(
            pa.Table.from_pylist(
                [{"net_guid": 60, "path": "HealthDamageSection"}], schema=guid_schema
            ),
            p / "net_guids.parquet",
        )
        return p

    def test_signed_zero_missing_causer_keeps_valid_amount_and_edges(self):
        p = self.make(fixture(causer=False))
        d = tool.extract(p)
        o = d["observations"][0]
        self.assertEqual(o["amount"]["status"], "validated")
        self.assertEqual(struct.pack("<f", o["amount"]["heal_taken"]), b"\0\0\0\x80")
        self.assertEqual(o["source_corroboration"]["status"], "absent")
        self.assertEqual(
            o["source_corroboration"]["event_instigator_pawn"]["value"], 50
        )
        self.assertTrue(o["recipient_corroboration"]["static_manifest_character"])
        self.assertIn(
            "serialized_heal_amount_sum",
            next(k for k in d["summaries"] if "recipient" in k),
        )

    def test_source_and_recipient_statuses_are_tallied_and_printed_with_zeros(self):
        opened = [{"time_ms": 10, "packet_id": 1, "channel_index": c, "actor_net_guid": g,
                   "event": "open", "class_path": "/Game/X.X_C"} for c, g in ((8, 40), (9, 70))]
        for actors, source, recipient in (
                (None, "no_prior_actor_open", "no_prior_actor_open"),
                (opened, "no_manifest_character_reference", "active")):
            with self.subTest(source=source):
                p = self.make(actors=actors)
                counts = tool.extract(p)["counts"]
                self.assertEqual(counts["source_status"],
                                 {s: int(s == source) for s in tool.SOURCE_STATUSES})
                self.assertEqual(counts["recipient_lifecycle_status"],
                                 {s: int(s == recipient) for s in tool.LIFECYCLE_STATUSES})
                out = p / "out.json"
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    self.assertEqual(tool.main(["--export", str(p), "--out", str(out)]), 0)
                self.assertIn(f'"{source}": 1', printed.getvalue())
                self.assertIn('"actor_closed": 0', printed.getvalue())

    def test_duplicate_and_disjoint_groups_are_ambiguous(self):
        rows = fixture()
        duplicate = copy.deepcopy(rows[0])
        other = copy.deepcopy(rows[0])
        other["actor_net_guid"] = 99
        other["object_net_guid"] = 100
        p = self.make(rows[:1] + [other] + rows[1:] + [duplicate])
        obs = tool.extract(p)["observations"]
        target = next(x for x in obs if x["identity"]["actor_net_guid"] == 40)
        self.assertIn("duplicate_same_coordinate_member", target["ambiguity_reasons"])
        self.assertIn("disjoint_physical_segments", target["ambiguity_reasons"])
        rows = fixture()
        rows.insert(6, row("Unrelated", raw=b"\0", bits=1))
        p2 = self.make(rows)
        d = tool.extract(p2)
        self.assertIn("disjoint_physical_segments", d["observations"][0]["ambiguity_reasons"])
        self.assertEqual(d["summaries"]["validated_observations"], 0)

    def test_same_time_lifecycle_is_unresolved_and_direct_edges_remain(self):
        actors = [
            {
                "time_ms": 1000,
                "packet_id": 1,
                "channel_index": 9,
                "actor_net_guid": 70,
                "event": "open",
                "class_path": "/Game/Characters/X/Abilities/A.A_C",
            }
        ]
        p = self.make(actors=actors)
        s = tool.extract(p)["observations"][0]["source_corroboration"]
        self.assertEqual(s["status"], "lifecycle_boundary_same_time")
        self.assertEqual(s["event_instigator"]["value"], 12)

    def test_raw_typed_mismatch_fails_atomically(self):
        rows = fixture(1.0)
        rows[0]["value_f64"] = 2.0
        p = self.make(rows)
        out = p / "out.json"
        out.write_text("old", encoding="utf-8")
        self.assertEqual(tool.main(["--export", str(p), "--out", str(out)]), 1)
        self.assertEqual(out.read_text(encoding="utf-8"), "old")

    def test_checkpoint_rows_are_preserved_separately(self):
        first = {
            "checkpoint_index": 2,
            "checkpoint_id": "cp-a",
            **copy.deepcopy(fixture()[0]),
        }
        second = {
            "checkpoint_index": 7,
            "checkpoint_id": "cp-b",
            **copy.deepcopy(fixture()[0]),
        }
        cp = [first, second]
        p = self.make(checkpoint=cp)
        d = tool.extract(p)
        self.assertEqual(d["counts"]["checkpoint_selected_rows"], 2)
        self.assertEqual(
            [
                (x["checkpoint_index"], x["checkpoint_id"])
                for x in d["checkpoint_observations"]
            ],
            [(2, "cp-a"), (7, "cp-b")],
        )

    def test_optional_declarations_may_be_jointly_absent_with_no_source_rows(self):
        p = self.make(fixture(causer=False)[:6])
        data = manifest()
        data["net_field_export_groups"][1]["fields"] = [
            x for x in data["net_field_export_groups"][1]["fields"] if x["handle"] <= 5
        ]
        (p / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        result = tool.extract(p)
        self.assertEqual(result["counts"]["amount_validated"], 1)
        self.assertEqual(
            result["observations"][0]["source_corroboration"]["status"], "absent"
        )

    def test_observed_undeclared_optional_edge_and_changed_declaration_fail(self):
        p = self.make()
        data = manifest()
        data["net_field_export_groups"][1]["fields"] = [
            x for x in data["net_field_export_groups"][1]["fields"] if x["handle"] != 9
        ]
        (p / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(tool.IntegrityError, "lacks its declaration"):
            tool.extract(p)
        data = manifest()
        next(
            x for x in data["net_field_export_groups"][1]["fields"] if x["handle"] == 7
        )["compatible_checksum"] = 1
        (p / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(tool.InputError, "optional heal parameter"):
            tool.extract(p)

    def test_a_declared_unknown_parameter_is_a_schema_error(self):
        p = self.make(fixture() + [row("MulticastNotifyHeal.HealSource", 12345, b"\0", 1)])
        data = manifest()
        data["net_field_export_groups"][1]["fields"].append(
            {"handle": 6, "name": "HealSource", "compatible_checksum": 12345})
        (p / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        o = tool.extract(p)["observations"][0]
        self.assertEqual(o["amount"]["status"], "invalid")
        self.assertIn("foreign_or_invalid_schema", o["ambiguity_reasons"])
        self.assertEqual(o["schema_errors"], [{"source_row": 9, "error": "unknown heal parameter row"}])

    def test_another_routes_declarations_do_not_gate_healing(self):
        p = self.make()
        data = manifest()
        field = {"handle": 0, "name": "DamageTaken", "compatible_checksum": 373546733}
        data["net_field_export_groups"].append({
            "path": "/Script/ShooterGame.DamageableComponent:MulticastNotifyDamage_Point",
            "fields": [field, field]})
        (p / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(tool.extract(p)["counts"]["amount_validated"], 1)

    def test_foreign_schema_is_preserved_but_not_validated(self):
        rows = fixture()
        rows[0]["group_path"] = "/Script/Foreign"
        p = self.make(rows)
        o = tool.extract(p)["observations"][0]
        self.assertEqual(o["amount"]["status"], "invalid")
        self.assertIn("foreign_or_invalid_schema", o["ambiguity_reasons"])
        self.assertEqual(o["source_rows"][0]["group_path"], "/Script/Foreign")

    def test_owner_evidence_is_raw_validated_and_reopen_is_rejected(self):
        cls = "/Game/Characters/X/Abilities/A.A_C"
        actors = [
            {
                "time_ms": 100,
                "packet_id": 1,
                "channel_index": 9,
                "actor_net_guid": 70,
                "event": "open",
                "class_path": cls,
            },
            {
                "time_ms": 200,
                "packet_id": 2,
                "channel_index": 9,
                "actor_net_guid": 70,
                "event": "open",
                "class_path": cls,
            },
        ]
        z, w = ref(50)
        owner = row(
            "Owner",
            None,
            z,
            w,
            time_ms=300,
            packet_id=3,
            channel_index=9,
            actor_net_guid=70,
            object_net_guid=None,
            group_path=cls,
            handle=0,
            value_i64=50,
        )
        p = self.make(fixture() + [owner], actors)
        s = tool.extract(p)["observations"][0]["source_corroboration"]
        self.assertEqual(s["status"], "actor_reopened_without_unique_active_instance")
        bad = copy.deepcopy(owner)
        bad["value_i64"] = 51
        p2 = self.make(fixture() + [bad], actors[:1])
        with self.assertRaises(tool.IntegrityError):
            tool.extract(p2)

    def test_output_may_not_alias_an_input(self):
        p = self.make()
        source = p / "fields.parquet"
        before = source.read_bytes()
        self.assertEqual(tool.main(["--export", str(p), "--out", str(source)]), 1)
        self.assertEqual(source.read_bytes(), before)

    def test_typed_instigator_edges_are_present_and_raw_checked(self):
        p = self.make()
        d = tool.extract(p)
        s = d["observations"][0]["source_corroboration"]
        for key, value in (("event_instigator", 12), ("event_instigator_pawn", 50)):
            with self.subTest(edge=key):
                self.assertEqual(s[key]["status"], "present")
                self.assertEqual(s[key]["value"], value)
                self.assertEqual(
                    d["counts"]["source_edge_status"][key],
                    {"present": 1, "null": 0, "absent": 0, "duplicate": 0, "invalid": 0},
                )
        # The established target is stated, including that it never joins.
        semantics = s["event_instigator"]["semantics"]
        self.assertIn("PlayerController", semantics)
        self.assertIn("does not join", semantics)

    def test_reference_typed_corruption_fails_atomically(self):
        for name in ("EventInstigator", "EventInstigatorPawn", "HealCauser"):
            with self.subTest(field=name):
                rows = fixture()
                target = next(
                    r for r in rows if r["field_name"] == "MulticastNotifyHeal." + name
                )
                target["value_i64"] += 1
                p = self.make(rows)
                out = p / "out.json"
                out.write_text("old", encoding="utf-8")
                self.assertEqual(tool.main(["--export", str(p), "--out", str(out)]), 1)
                self.assertEqual(out.read_text(encoding="utf-8"), "old")

    def test_an_invalid_edge_is_counted_not_just_labelled(self):
        # A malformed reference window keeps the amount, marks the edge
        # invalid, and reaches the counts.
        rows = fixture()
        target = next(
            r for r in rows if r["field_name"] == "MulticastNotifyHeal.EventInstigator"
        )
        target["bit_count"] += 8
        p = self.make(rows)
        d = tool.extract(p)
        o = d["observations"][0]
        self.assertEqual(o["amount"]["status"], "validated")
        self.assertEqual(o["source_corroboration"]["event_instigator"]["status"], "invalid")
        self.assertEqual(d["counts"]["source_edge_status"]["event_instigator"]["invalid"], 1)
        self.assertEqual(d["counts"]["source_edge_status"]["causer"]["present"], 1)

    def test_untyped_instigator_from_an_older_export_fails_loudly(self):
        # A stale export carries the raw window only: that stops the run
        # rather than turning the edge "invalid" behind a successful exit.
        for name in ("EventInstigator", "EventInstigatorPawn"):
            with self.subTest(field=name):
                rows = fixture()
                target = next(
                    r for r in rows if r["field_name"] == "MulticastNotifyHeal." + name
                )
                target["value_i64"] = None
                p = self.make(rows)
                with self.assertRaisesRegex(tool.IntegrityError, "untyped"):
                    tool.extract(p)

    def test_selected_batch_ordinals_include_filtered_rows_and_boundary(self):
        path = self.tmp() / "fields.parquet"
        filler = row("Unrelated", raw=b"\0", bits=1)
        rows = [filler] * 65534 + [fixture()[0], filler, fixture()[1]]
        pq.write_table(
            pa.Table.from_pylist(rows, schema=SCHEMA), path, row_group_size=65536
        )
        got = list(tool.iter_selected_fields(path, False))
        self.assertEqual([ordinal for ordinal, _ in got], [65534, 65536])


class EarlierPawnTests(TempDirTestCase):
    """The recipient pawn 40 is the one a player had before reconnecting:
    SpawnedCharacter goes 40 -> 0 -> 45 and the manifest keeps 45."""

    make = Tests.make

    def test_recipient_on_an_earlier_pawn_is_joined_to_its_player(self):
        state = "/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C"
        history = [
            row("SpawnedCharacter", time_ms=t, packet_id=t, actor_net_guid=7,
                object_net_guid=None, group_path=state, handle=0, value_i64=v)
            for t, v in ((10, 40), (20, 0), (30, 45))
        ]
        p = self.make(history + fixture())
        data = manifest()
        data["players"] = [
            {"actor_net_guid": 7, "subject": "recipient", "character_net_guid": 45},
            {"actor_net_guid": 8, "subject": "source", "character_net_guid": 50},
        ]
        (p / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        d = tool.extract(p)
        recipient = d["observations"][0]["recipient_corroboration"]
        self.assertTrue(recipient["static_manifest_character"])
        self.assertEqual(recipient["subject"], "recipient")
        self.assertEqual(d["player_identity"]["non_final_spawned_character_pawns"], 1)
        self.assertEqual(d["counts"]["amount_validated"], 1)
