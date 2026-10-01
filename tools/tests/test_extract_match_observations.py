"""Regression coverage for evidence-labelled derived match observations."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


from support import TempDirTestCase
import extract_match_observations as observations


def write_export(root: Path) -> None:
    pq.write_table(pa.table({
        "net_guid": [100, 300, 400, 401, 402, 500, 601, 700],
        "path": ["MagazineAmmo", None, "/State/ReloadState", "/State/IdleState", "/State/ReloadStateEmpty", None, None, "InventoryComponent"],
        "outer_net_guid": [200, 200, None, None, None, 600, None, 600],
    }), root / "net_guids.parquet")
    pq.write_table(pa.table({
        "group": ["roundStarted", "spikeDefused"],
        "time1": [0, 55],
    }), root / "events.parquet")
    rows = [
        (10, 1, 0, 100, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 30, None),
        (11, 1, 0, 100, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 30, None),
        (20, 2, 0, 100, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 28, None),
        (21, 2, 0, 0, "/Script/ShooterGame.ReplayEffectComponent_ClassNetCache", "ReplayPlayContinuousEffectAtLocation.EffectID", 7, None),
        (5, 1, 701, 700, "/Script/ShooterGame.AresInventory", "CurrentEquippable", 200, None),
        (30, 1, 701, 700, "/Script/ShooterGame.AresInventory", "NewCurrentEquippable", 201, None),
        (12, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 400, None),
        (25, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 401, None),
        (40, 1, 800, 0, "/Game/GameModes/Bomb/TimedBomb.TimedBomb_C", "DefuseProgress", None, 0.0),
        (50, 1, 800, 0, "/Game/GameModes/Bomb/TimedBomb.TimedBomb_C", "DefuseProgress", None, 1.0),
        (60, 1, 800, 0, "/Game/GameModes/Bomb/TimedBomb.TimedBomb_C", "DefuseProgress", None, 0.0),
        (70, 1, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "RoundInfos[3].StartOfRoundMoney", 800, None),
        (71, 1, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "RoundInfos[3].EndOfRoundMoney", 900, None),
        (69, 1, 600, 0, "/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C", "Owner", 42, None),
        (69, 2, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "Owner", 42, None),
        (82, 1, 602, 0, "/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C", "Owner", 43, None),
        (82, 2, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "Owner", 43, None),
        (90, 1, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "RoundInfos[4].EndOfRoundMoney", 1000, None),
        (80, 1, 900, 0, "/Script/ShooterGame.BaseTeamState", "LoadoutValue", 5000, None),
        (80, 1, 900, 0, "/Script/ShooterGame.BaseTeamState", "AverageLoadoutValue", 1000, None),
        (81, 1, 901, 0, "/Game/GameModes/Bomb/BombGameState.BombGameState_C", "TeamEconomy[0].LoadoutValue", 4500, None),
        (81, 1, 901, 0, "/Game/GameModes/Bomb/BombGameState.BombGameState_C", "TeamEconomy[0].AverageLoadoutValue", 900, None),
        (90, 1, 0, 500, "/Script/ShooterGame.MoneyManagementComponent", "Money", 1000, None),
        (100, 1, 0, 500, "/Script/ShooterGame.MoneyManagementComponent", "Money", 700, None),
        # The component state arrives across packets. A later duplicate must
        # not become a second observation.
        (101, 1, 910, 911, "/Script/ShooterGame.PurchasedItemComponent", "PurchasingPlayerState", 600, None),
        (102, 2, 910, 911, "/Script/ShooterGame.PurchasedItemComponent", "Purchaseable", 200, None),
        (102, 2, 910, 911, "/Script/ShooterGame.PurchasedItemComponent", "PurchasableTransactionSource", 2, None),
        (103, 3, 910, 911, "/Script/ShooterGame.PurchasedItemComponent", "Purchaseable", 200, None),
        (102, 2, 912, 913, "/Script/ShooterGame.PurchasedItemComponent", "Purchaseable", 201, None),
    ]
    names = ("time_ms", "packet_id", "actor_net_guid", "object_net_guid",
             "group_path", "field_name", "value_i64", "value_f64")
    table = pa.table({name: [row[index] for row in rows]
                      for index, name in enumerate(names)})
    # Match vrfkit's exported schema, including dictionary-encoded names.
    for name in ("group_path", "field_name"):
        table = table.set_column(table.schema.get_field_index(name), name,
                                 table[name].dictionary_encode())
    pq.write_table(table, root / "fields.parquet")


def append_field_rows(root: Path, rows: list[tuple]) -> None:
    """Append rows using the fixture's exported Arrow schema."""
    path = root / "fields.parquet"
    table = pq.read_table(path)
    names = table.schema.names
    addition = pa.Table.from_pylist(
        [{name: row[index] for index, name in enumerate(names)} for row in rows],
        schema=table.schema,
    )
    pq.write_table(pa.concat_tables([table, addition]), path)


class MatchObservationTests(TempDirTestCase):
    def _replace_state_rows(self, root: Path, rows: list[tuple]) -> None:
        table = pq.read_table(root / "fields.parquet")
        keep = pa.array([not group.endswith("EquippableStateMachineComponent")
                         for group in table["group_path"].to_pylist()])
        pq.write_table(table.filter(keep), root / "fields.parquet")
        append_field_rows(root, rows)

    def test_build_deduplicates_and_labels_all_v1_evidence(self):
        root = self.tmp()
        write_export(root)
        result = observations.build(root)

        self.assertEqual(len(result["ammo_changes"]), 1)
        self.assertEqual(result["ammo_changes"][0]["delta"], -2)
        self.assertTrue(result["ammo_changes"][0]["global_effect_id_within_300ms"])
        self.assertEqual({row["source_field"] for row in result["equip_intervals"]},
                         {"CurrentEquippable", "NewCurrentEquippable"})
        self.assertTrue(all(row["duration_ms"] is None
                            for row in result["equip_intervals"]))
        self.assertEqual(result["equip_intervals"][0]["inventory_component_guid"], 700)
        self.assertEqual(result["equip_intervals"][0]["inventory_actor_guid"], 701)
        self.assertEqual(result["equip_intervals"][0]["owner_guid"], 600)
        self.assertIsNone(result["reload_intervals"][0]["duration_ms"])
        self.assertEqual(result["reload_intervals"][0]["observed_span_ms"], 13)
        self.assertTrue(result["reload_intervals"][0]["left_censored"])
        self.assertFalse(result["defuse_progress_transitions"][0]["authoritative_completion"])
        self.assertEqual(result["defuse_completions"][0]["time_ms"], 55)
        self.assertEqual(result["round_balances"][1]["money"], 900)
        self.assertEqual(result["round_balances"][1]["player_state_guid"], 600)
        self.assertEqual(result["round_balances"][1]["owner_controller_guid"], 42)
        self.assertEqual(result["round_balances"][1]["player_join_source"],
                         "OwnerExclusivePlayerInfo.Owner@time -> BombPlayerState.Owner@time")
        self.assertEqual(result["round_balances"][2]["owner_controller_guid"], 43)
        self.assertEqual(result["round_balances"][2]["player_state_guid"], 602)
        self.assertEqual(result["round_balances"][1]["round_info_slot"], 3)
        self.assertEqual({row["source"] for row in result["team_loadouts"]},
                         {"BaseTeamState", "BombGameState.TeamEconomy"})
        self.assertTrue(all(row["average_times_five_matches_total"]
                            for row in result["team_loadouts"]))
        self.assertEqual(result["money_decreases"][0]["amount"], 300)
        self.assertEqual(len(result["transaction_snapshots"]), 1)
        self.assertEqual(result["transaction_snapshots"][0]["source_packet_id"], 2)
        self.assertEqual(result["transaction_snapshots"][0]["round_ordinal"], 0)
        self.assertEqual(result["transaction_snapshots"][0]["nearby_money_decrease_count_2s"], 1)
        self.assertEqual(result["transaction_snapshots"][0]["transaction_source"], 2)
        self.assertIn("state transition; not a purchase ledger",
                      result["transaction_snapshots"][0]["evidence"])
        self.assertEqual(result["attribution_coverage"]["equip_owner_outer"], {
            "joined": 2,
            "total": 2,
            "source": "net_guids.outer_net_guid(InventoryComponent)",
        })
        self.assertEqual(result["attribution_coverage"]["round_balance_player"]["joined"], 3)

    def test_weapon_scoped_rpcs_are_counted_per_ammo_decrease(self):
        """Magazine 100's weapon is its outer 200, a dynamic actor with no
        net_guids row; the decrease 30 -> 28 is at t=20."""
        gun = "/Game/Equippables/Guns/Rifles/Test.Test_C_ClassNetCache"
        other = [(21, 1, 201, 0, gun, observations.WEAPON_RPC, 7, None),
                 (21, 1, 200, 0, "/Script/ShooterGame.X_ClassNetCache", observations.WEAPON_RPC, 7, None)]
        for times, count, tally in (((), 0, "none"), ((21,), 1, "unique"),
                                    ((15, 320), 2, "multiple"), ((321,), 0, "none")):
            with self.subTest(times=times), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                write_export(root)
                append_field_rows(root, other + [(t, 1, 200, 0, gun, observations.WEAPON_RPC, 7, None)
                                                 for t in times])
                result = observations.build(root)
            self.assertEqual(result["ammo_changes"][0]["weapon_rpc_within_300ms"], count)
            self.assertEqual(result["ammo_decrease_weapon_rpc_within_300ms"],
                             {k: int(k == tally) for k in observations.WEAPON_RPC_TALLY})
        root = self.tmp()
        write_export(root)
        net = pq.read_table(root / "net_guids.parquet").to_pylist()
        pq.write_table(pa.Table.from_pylist([dict(r, outer_net_guid=None) if r["net_guid"] == 100
                                             else r for r in net]), root / "net_guids.parquet")
        result = observations.build(root)
        self.assertIsNone(result["ammo_changes"][0]["weapon_rpc_within_300ms"])
        self.assertEqual(result["ammo_decrease_weapon_rpc_within_300ms"]["no_weapon"], 1)

    def test_same_packet_conflict_is_not_value_sorted_into_a_change(self):
        changes, ambiguous = observations._changes([
            (10, 1, 0, 30),
            (10, 1, 1, 28),
            (20, 2, 2, 27),
        ])
        self.assertEqual(changes, [])
        self.assertEqual(ambiguous, 1)

    def test_reload_conflict_closes_at_an_unknown_boundary(self):
        root = self.tmp()
        write_export(root)
        append_field_rows(root, [
            (20, 3, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 400, None),
            (20, 3, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 401, None),
        ])
        result = observations.build(root)

        self.assertEqual(len(result["reload_intervals"]), 1)
        interval = result["reload_intervals"][0]
        self.assertEqual(interval["to_ms"], 20)
        self.assertIsNone(interval["duration_ms"])
        self.assertFalse(interval["closed_by_state_change"])
        self.assertEqual(interval["end_boundary"], "ambiguous_same_packet")

    def test_reload_magazine_evidence_is_same_weapon_and_inside_interval(self):
        root = self.tmp()
        write_export(root)
        append_field_rows(root, [
            # 30 -> 35 is a positive magazine observation while the
            # observed ReloadState interval is still open.
            (18, 2, 0, 100, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 35, None),
            # Different component/object has no proven same-weapon outer
            # join and must not be attached.
            (19, 1, 0, 999, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 99, None),
        ])
        result = observations.build(root)

        interval = result["reload_intervals"][0]
        self.assertEqual(interval["magazine_increase_count"], 1)
        self.assertEqual(interval["magazine_increase_join"],
                         "same_weapon_outer_guid; observed_interval")
        self.assertEqual(len(result["reload_magazine_increases"]), 1)
        self.assertEqual(result["reload_magazine_increases"][0]["time_ms"], 18)
        self.assertEqual(result["reload_magazine_increases"][0]["magazine_component_guid"], 100)

    def test_unknown_state_path_breaks_reload_interval(self):
        root = self.tmp()
        write_export(root)
        append_field_rows(root, [
            (20, 2, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 999, None),
        ])
        result = observations.build(root)

        interval = result["reload_intervals"][0]
        self.assertEqual(interval["end_boundary"], "unknown_state_path")
        self.assertIsNone(interval["duration_ms"])
        self.assertTrue(interval["left_censored"])

    def test_known_nonreload_then_reload_empty_has_observed_entry(self):
        root = self.tmp()
        write_export(root)
        self._replace_state_rows(root, [
            (5, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 401, None),
            (12, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 402, None),
            (25, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 401, None),
        ])
        result = observations.build(root)

        interval = result["reload_intervals"][0]
        self.assertEqual(interval["state_guid"], 402)
        self.assertFalse(interval["left_censored"])
        self.assertFalse(interval["right_censored"])
        self.assertEqual(interval["end_boundary"], "state_change")

    def test_first_reload_normal_exit_is_left_censored(self):
        root = self.tmp(); write_export(root)
        interval = observations.build(root)["reload_intervals"][0]
        self.assertTrue(interval["left_censored"])
        self.assertFalse(interval["right_censored"])

    def test_known_idle_reload_stream_end_is_not_left_censored(self):
        root = self.tmp(); write_export(root)
        self._replace_state_rows(root, [
            (5, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 401, None),
            (12, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 400, None),
        ])
        interval = observations.build(root)["reload_intervals"][0]
        self.assertFalse(interval["left_censored"])
        self.assertTrue(interval["right_censored"])

    def test_same_timestamp_later_packet_is_inside_but_boundary_packet_is_excluded(self):
        root = self.tmp(); write_export(root)
        append_field_rows(root, [
            # Same state-entry packet is unorderable and excluded; packet 2
            # at the same timestamp is strictly inside the interval.
            (12, 1, 0, 100, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 35, None),
            (12, 2, 0, 100, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 40, None),
        ])
        rows = observations.build(root)["reload_magazine_increases"]
        self.assertEqual([(row["time_ms"], row["packet_id"]) for row in rows], [(12, 2)])

    def test_unknown_then_reload_is_left_censored(self):
        root = self.tmp(); write_export(root)
        append_field_rows(root, [
            (13, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 999, None),
            (14, 1, 0, 300, "/Script/ShooterGame.EquippableStateMachineComponent", "CurrentState", 400, None),
        ])
        rows = observations.build(root)["reload_intervals"]
        self.assertTrue(rows[-1]["left_censored"])

    def test_a_repeated_net_guid_fails_loudly(self):
        root = self.tmp(); write_export(root)
        net = pq.read_table(root / "net_guids.parquet")
        pq.write_table(pa.concat_tables([net, net.slice(0, 1)]), root / "net_guids.parquet")
        with self.assertRaisesRegex(ValueError, "repeats"):
            observations.build(root)

    def test_a_second_weapons_magazine_does_not_join(self):
        root = self.tmp(); write_export(root)
        net = pq.read_table(root / "net_guids.parquet")
        pq.write_table(pa.concat_tables([net, pa.table({
            "net_guid": [101], "path": ["MagazineAmmo"], "outer_net_guid": [201],
        })]), root / "net_guids.parquet")
        append_field_rows(root, [
            (10, 1, 0, 101, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 30, None),
            (18, 1, 0, 101, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 35, None),
        ])
        rows = observations.build(root)["reload_magazine_increases"]
        self.assertEqual(rows, [])

    def test_round_reset_breaks_reload_magazine_join(self):
        root = self.tmp()
        write_export(root)
        append_field_rows(root, [
            (18, 2, 0, 100, "/Script/ShooterGame.AmmoComponent", "AuthResourceAmount", 35, None),
        ])
        events = pq.read_table(root / "events.parquet")
        pq.write_table(pa.concat_tables([events, pa.table({
            "group": ["roundStarted"], "time1": [20],
        })]), root / "events.parquet")
        result = observations.build(root)

        interval = result["reload_intervals"][0]
        self.assertTrue(interval["reset_boundary_crossed"])
        self.assertEqual(interval["to_ms"], 20)
        self.assertIsNone(interval["to_packet_id"])
        self.assertTrue(interval["right_censored"])
        self.assertIsNone(interval["duration_ms"])
        self.assertEqual(interval["magazine_increase_count"], 0)
        self.assertEqual(result["reload_magazine_increases"], [])

    def test_team_conflicts_are_null_and_row_order_independent(self):
        temp = self.tmp()
        original = temp / "original"
        shuffled = temp / "shuffled"
        original.mkdir()
        shuffled.mkdir()
        write_export(original)
        write_export(shuffled)
        conflict = [
            (80, 1, 900, 0, "/Script/ShooterGame.BaseTeamState", "LoadoutValue", 5100, None),
        ]
        append_field_rows(original, conflict)
        append_field_rows(shuffled, conflict)
        table = pq.read_table(shuffled / "fields.parquet")
        reverse = pa.array(range(table.num_rows - 1, -1, -1))
        pq.write_table(table.take(reverse), shuffled / "fields.parquet")
        result = observations.build(original)
        self.assertEqual(result, observations.build(shuffled))

        base = next(row for row in result["team_loadouts"] if row["source"] == "BaseTeamState")
        self.assertIsNone(base["loadout_value"])
        self.assertEqual(base["average_loadout_value"], 1000)
        self.assertIsNone(base["average_times_five_matches_total"])
        self.assertEqual(result["ambiguous_same_packet_counts"]["team_loadout"], 1)

    def test_owner_join_does_not_look_ahead_to_a_later_same_time_packet(self):
        root = self.tmp()
        write_export(root)
        append_field_rows(root, [
            (82, 1, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "RoundInfos[5].EndOfRoundMoney", 1200, None),
        ])
        result = observations.build(root)

        balance = next(row for row in result["round_balances"]
                       if row["round_info_slot"] == 5)
        self.assertEqual(balance["packet_id"], 1)
        self.assertEqual(balance["owner_controller_guid"], 42)
        self.assertEqual(balance["player_state_guid"], 600)

    def test_a_cleared_owner_ends_the_round_balance_join(self):
        """Owner 43 is cleared to 0 on both links (a disconnect), then a
        balance is written: no controller holds it, so no player joins."""
        root = self.tmp()
        write_export(root)
        append_field_rows(root, [
            (95, 1, 602, 0, "/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C", "Owner", 0, None),
            (95, 2, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "Owner", 0, None),
            (100, 1, 601, 0, "/Script/ShooterGame.OwnerExclusivePlayerInfo", "RoundInfos[6].EndOfRoundMoney", 1300, None),
        ])
        result = observations.build(root)

        balance = next(row for row in result["round_balances"] if row["round_info_slot"] == 6)
        self.assertEqual((balance["owner_controller_guid"], balance["player_state_guid"],
                          balance["player_join_source"]), (0, None, "unavailable"))
        self.assertEqual(result["attribution_coverage"]["round_balance_player"]["joined"], 3)

    def test_packet_order_is_stable_when_source_rows_are_shuffled(self):
        samples = [
            (20, 2, 4, 28),
            (10, 1, 2, 30),
            (30, 3, 8, 27),
        ]
        self.assertEqual(observations._changes(samples),
                         observations._changes(list(reversed(samples))))

    def test_round_balance_player_stays_null_without_the_owner_chain(self):
        root = self.tmp()
        write_export(root)
        table = pq.read_table(root / "fields.parquet")
        keep = pa.array([
            not (group.endswith("BombPlayerState_C") and name == "Owner")
            for group, name in zip(table.column("group_path").to_pylist(),
                                   table.column("field_name").to_pylist())
        ])
        pq.write_table(table.filter(keep), root / "fields.parquet")
        result = observations.build(root)

        self.assertTrue(all(row["player_state_guid"] is None
                            for row in result["round_balances"]))
        self.assertTrue(all(row["player_join_source"] == "unavailable"
                            for row in result["round_balances"]))
        self.assertEqual(result["attribution_coverage"]["round_balance_player"]["joined"], 0)

    @staticmethod
    def _team_switch(root: Path, events: dict, money: list[tuple], buyer_snapshot_ms=None):
        """Append switch/round events, a Money component 510 of PlayerState 610,
        and optionally a PurchasedItemComponent snapshot of that player."""
        net = pq.read_table(root / "net_guids.parquet")
        pq.write_table(pa.concat_tables([net, pa.table({
            "net_guid": [510, 511], "path": pa.array([None, None], pa.string()),
            "outer_net_guid": [610, 611],
        })]), root / "net_guids.parquet")
        old = pq.read_table(root / "events.parquet")
        pq.write_table(pa.concat_tables([old, pa.table(events)]), root / "events.parquet")
        group = "/Script/ShooterGame.MoneyManagementComponent"
        rows = [(time_ms, packet_id, 0, component, group, "Money", value, None)
                for time_ms, packet_id, component, value in money]
        if buyer_snapshot_ms is not None:
            item = "/Script/ShooterGame.PurchasedItemComponent"
            rows += [(buyer_snapshot_ms, 1, 920, 921, item, "PurchasingPlayerState", 610, None),
                     (buyer_snapshot_ms, 1, 920, 921, item, "Purchaseable", 202, None)]
        append_field_rows(root, rows)

    def test_team_switch_credit_reset_is_not_a_money_decrease(self):
        """Shaped like 0002c486 (13.02): the resets 8 ms after switchTeams,
        before the round start, are no snapshot's nearest decrease."""
        root = self.tmp()
        write_export(root)
        self._team_switch(
            root, {"group": ["switchTeams", "roundStarted"], "time1": [200, 250]},
            [(100, 1, 510, 5200), (208, 2, 510, 800),
             (100, 1, 511, 2300), (209, 2, 511, 0)],
            buyer_snapshot_ms=300)
        result = observations.build(root)

        self.assertEqual([row["money_component_guid"] for row in result["money_decreases"]],
                         [500])
        window = result["money_decreases_in_team_switch_window"]
        self.assertEqual([(row["money_component_guid"], row["before"], row["after"],
                           row["amount"], row["team_switch_ms"], row["next_round_start_ms"])
                          for row in window],
                         [(510, 5200, 800, 4400, 200, 250), (511, 2300, 0, 2300, 200, 250)])
        snapshot = next(row for row in result["transaction_snapshots"]
                        if row["purchasing_player_state_guid"] == 610)
        self.assertEqual(snapshot["nearby_money_decrease_count_2s"], 0)
        self.assertIsNone(snapshot["nearest_money_decrease_ms"])
        self.assertIsNone(snapshot["nearest_money_decrease_amount"])
        self.assertEqual(result["team_switch_windows"], {
            "switches": 1, "closed_by_round_start": 1, "closed_by_end_of_stream": 0})

    def test_a_carried_over_800_then_a_buy_after_the_round_start_is_a_decrease(self):
        """The buy's collapsed interval spans the switch; its own time does not
        (see `_team_switch_windows`)."""
        root = self.tmp()
        write_export(root)
        self._team_switch(
            root, {"group": ["switchTeams", "roundStarted"], "time1": [200, 250]},
            [(100, 1, 510, 800), (208, 2, 510, 800), (300, 3, 510, 300)],
            buyer_snapshot_ms=300)
        result = observations.build(root)

        self.assertEqual(result["money_decreases_in_team_switch_window"], [])
        buy = next(row for row in result["money_decreases"]
                   if row["money_component_guid"] == 510)
        self.assertEqual((buy["time_ms"], buy["before"], buy["after"]), (300, 800, 300))
        snapshot = next(row for row in result["transaction_snapshots"]
                        if row["purchasing_player_state_guid"] == 610)
        self.assertEqual(snapshot["nearest_money_decrease_ms"], 300)
        self.assertEqual(snapshot["nearest_money_decrease_amount"], 500)

    def test_a_final_switch_with_no_later_round_start_windows_to_the_end(self):
        """As in the overtime replays that end soon after their last switch."""
        root = self.tmp()
        write_export(root)
        self._team_switch(root, {"group": ["switchTeams"], "time1": [200]},
                          [(100, 1, 510, 300), (208, 2, 510, 0)])
        result = observations.build(root)

        window = result["money_decreases_in_team_switch_window"]
        self.assertEqual([(row["after"], row["next_round_start_ms"]) for row in window],
                         [(0, None)])
        self.assertEqual(result["team_switch_windows"]["closed_by_end_of_stream"], 1)

    def test_a_decrease_before_the_switch_is_untouched(self):
        root = self.tmp()
        write_export(root)
        self._team_switch(
            root, {"group": ["switchTeams", "roundStarted"], "time1": [200, 250]},
            [(100, 1, 510, 5200), (199, 2, 510, 800)])
        result = observations.build(root)

        self.assertEqual(result["money_decreases_in_team_switch_window"], [])
        self.assertIn(199, [row["time_ms"] for row in result["money_decreases"]])
