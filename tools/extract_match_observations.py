#!/usr/bin/env python3
"""Extract conservative, joined match observations from a vrfkit export.

Publishes observations and their evidence, never a game metric the replay
cannot support. PurchasedItemComponent rows are snapshots; only Money
decreases are economic events, except those between `switchTeams` and the
next `roundStarted` (no buy phase is open): the team-switch credit reset,
published as `money_decreases_in_team_switch_window`, never as purchases.
"""

from __future__ import annotations

import bisect
import json
import re
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from atomic_io import run_json_cli
from wire_bits import load_net_guids


MAGAZINE_PATH = "MagazineAmmo"
SHOT_EFFECT_ID = "ReplayPlayContinuousEffectAtLocation.EffectID"
#: Gun-scoped RPC whose actor is the magazine's outer (the weapon).
WEAPON_RPC = "MulticastPlayContinuousEffectFromClient.EffectID"
#: Each ammo decrease by how many WEAPON_RPC rows of its weapon lie within 300 ms.
WEAPON_RPC_TALLY = ("no_weapon", "none", "unique", "multiple")
ROUND_MONEY_RE = re.compile(r"RoundInfos\[(\d+)\]\.(StartOfRoundMoney|EndOfRoundMoney)$")
TEAM_ECONOMY_RE = re.compile(
    r"TeamEconomy\[(\d+)\]\.(LoadoutValue|AverageLoadoutValue)$"
)
PURCHASE_FIELDS = {
    "PurchasingPlayerState",
    "Purchaseable",
    "PurchasableTransactionSource",
}


def _collapse(samples):
    """Keep value changes from one replicated scalar stream.

    The source row ordinal breaks `(time_ms, packet_id)` ties. Conflicting
    values in one packet have no observable order, so they become an explicit
    unknown boundary, never a value-sorted invented transition.
    """
    result = []
    ambiguous = 0
    previous = object()
    ordered = sorted(samples, key=lambda row: row[:3])
    index = 0
    while index < len(ordered):
        time_ms, packet_id, _, value = ordered[index]
        end = index + 1
        values = {value}
        while end < len(ordered) and ordered[end][:2] == (time_ms, packet_id):
            values.add(ordered[end][3])
            end += 1
        if len(values) != 1:
            result.append((time_ms, packet_id, None))
            previous = object()
            ambiguous += 1
        elif value != previous:
            result.append((time_ms, packet_id, value))
            previous = value
        index = end
    return result, ambiguous


def _changes(samples):
    """Return `(time, previous, value, delta)` after replication collapse."""
    values, ambiguous = _collapse(samples)
    return [
        (time_ms, before, after, after - before)
        for (_, _, before), (time_ms, _, after) in zip(values, values[1:])
        if before is not None and after is not None
    ], ambiguous


def _team_switch_windows(switches: list[int], round_starts: list[int]) -> list[tuple]:
    """`[switchTeams, first roundStarted after it)` per switch; end None = end of stream.

    A side switch resets credits (800 at half time, 5000 in overtime) with an
    ordinary `Money` write while no buy phase is open: 5,616 corpus decreases,
    all 7-10 ms after the switch. Keyed on the decrease's own time, not its
    collapsed interval: a player already on 800 gets no reset sample, and an
    interval rule took 105 real buys.
    """
    windows = []
    for switch in switches:
        index = bisect.bisect_right(round_starts, switch)
        windows.append((switch, round_starts[index] if index < len(round_starts) else None))
    return windows


def _team_switch_window(windows: list[tuple], time_ms: int) -> tuple | None:
    """The first window containing `time_ms`, or None."""
    for start, end in windows:
        if start <= time_ms and (end is None or time_ms < end):
            return start, end
    return None


def _round_context(round_starts: list[int], time_ms: int) -> int | None:
    """Zero-based replay round ordinal, or null before the first round event."""
    index = bisect.bisect_right(round_starts, time_ms) - 1
    return index if index >= 0 else None


def _timeline(samples):
    """Return replication-collapsed `(time, packet)` keys and values."""
    compact, _ = _collapse(samples)
    return [(sample[0], sample[1]) for sample in compact], [sample[2] for sample in compact]


def _value_at(timeline, time_ms: int, packet_id: int):
    """Return the last known scalar value at a packet, preserving unknowns."""
    keys, values = timeline
    index = bisect.bisect_right(keys, (time_ms, packet_id)) - 1
    return values[index] if index >= 0 else None


def _read_columns(path: Path, columns: list[str], *, observation_fields=False) -> dict[str, list]:
    table = pq.read_table(path, columns=columns)
    if observation_fields:
        # Filtered in Arrow before millions of Python objects; order is kept.
        names = pc.cast(table.column("field_name"), pa.string())
        scalar_names = pa.array(sorted(PURCHASE_FIELDS | {
            "AuthResourceAmount", "CurrentEquippable", "NewCurrentEquippable",
            "CurrentState", "Money", "DefuseProgress", "LoadoutValue",
            "AverageLoadoutValue", "Owner", SHOT_EFFECT_ID, WEAPON_RPC,
        }))
        indexed = pc.match_substring_regex(
            names, "^(?:" + ROUND_MONEY_RE.pattern + "|" + TEAM_ECONOMY_RE.pattern + ")"
        )
        table = table.filter(pc.fill_null(pc.or_kleene(
            pc.is_in(names, value_set=scalar_names), indexed), False))
    return {name: table.column(name).to_pylist() for name in columns}


def _near(sorted_times: list[int], time_ms: int, window_ms: int) -> int:
    """How many of `sorted_times` lie within +/-window_ms of time_ms."""
    return (bisect.bisect_right(sorted_times, time_ms + window_ms)
            - bisect.bisect_left(sorted_times, time_ms - window_ms))


def _stable_rows(rows: list[dict]) -> list[dict]:
    """Sort output independently of Parquet's physical row order."""
    return sorted(
        rows,
        key=lambda row: (
            row.get("time_ms", row.get("from_ms")),
            json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
    )


def build(export_dir: Path) -> dict:
    """Build evidence-labelled observations for one export directory."""
    required = ("fields.parquet", "net_guids.parquet", "events.parquet")
    missing = [name for name in required if not (export_dir / name).is_file()]
    if missing:
        raise ValueError(f"missing required export tables: {', '.join(missing)}")

    net = load_net_guids(export_dir, "path", "outer_net_guid")
    path_of = {guid: path for guid, (path, _) in net.items() if path}
    outer_of = {guid: outer for guid, (_, outer) in net.items() if outer is not None}

    events = _read_columns(export_dir / "events.parquet", ["group", "time1"])
    round_starts = sorted(time for group, time in zip(events["group"], events["time1"])
                          if group == "roundStarted")
    defuse_events = sorted(time for group, time in zip(events["group"], events["time1"])
                           if group == "spikeDefused")
    switch_windows = _team_switch_windows(
        sorted(time for group, time in zip(events["group"], events["time1"])
               if group == "switchTeams"),
        round_starts)

    fields = _read_columns(
        export_dir / "fields.parquet",
        ["time_ms", "packet_id", "actor_net_guid", "object_net_guid",
         "group_path", "field_name", "value_i64", "value_f64"],
        observation_fields=True,
    )

    magazine = defaultdict(list)
    inventory = defaultdict(list)
    state_machine = defaultdict(list)
    money = defaultdict(list)
    defuse_progress = defaultdict(list)
    balances = defaultdict(list)
    team_samples = defaultdict(list)
    purchase_updates = defaultdict(list)
    owner_info_controllers = defaultdict(list)
    player_controllers = defaultdict(list)
    shot_times = []
    weapon_rpc_times = defaultdict(list)

    for row in range(len(fields["time_ms"])):
        time_ms = fields["time_ms"][row]
        packet_id = fields["packet_id"][row]
        actor = fields["actor_net_guid"][row]
        obj = fields["object_net_guid"][row]
        group = fields["group_path"][row] or ""
        name = fields["field_name"][row]
        integer = fields["value_i64"][row]
        floating = fields["value_f64"][row]

        if group.endswith("AmmoComponent") and name == "AuthResourceAmount" and integer is not None:
            if path_of.get(obj) == MAGAZINE_PATH:
                magazine[obj].append((time_ms, packet_id, row, integer))
        elif group.endswith("AresInventory") and name in ("CurrentEquippable", "NewCurrentEquippable"):
            if integer is not None:
                # Current and New have no documented order inside a packet.
                # Keyed by component AND actor: the actor alone joins wrong.
                inventory[(obj, actor, name)].append((time_ms, packet_id, row, integer))
        elif group.endswith("EquippableStateMachineComponent") and name == "CurrentState":
            if integer is not None:
                # By component: an equippable actor can own several.
                state_machine[obj].append((time_ms, packet_id, row, integer))
        elif group.endswith("MoneyManagementComponent") and name == "Money" and integer is not None:
            money[obj].append((time_ms, packet_id, row, integer))
        elif group.endswith("TimedBomb_C") and name == "DefuseProgress":
            value = floating if floating is not None else integer
            if value is not None:
                defuse_progress[actor].append((time_ms, packet_id, row, value))
        elif integer is not None and (match := ROUND_MONEY_RE.fullmatch(name or "")):
            slot, value_kind = match.groups()
            balances[(actor, int(slot), value_kind)].append((time_ms, packet_id, row, integer))
        elif group.endswith("OwnerExclusivePlayerInfo") and name == "Owner" and integer is not None:
            # The owning controller; 0 (cleared) is kept so it ends the join.
            owner_info_controllers[actor].append((time_ms, packet_id, row, integer))
        elif group.endswith("BombPlayerState_C") and name == "Owner" and integer is not None:
            # The inverse controller -> player state link.
            player_controllers[actor].append((time_ms, packet_id, row, integer))
        elif group.endswith("BaseTeamState") and name in ("LoadoutValue", "AverageLoadoutValue"):
            if integer is not None:
                team_samples[(
                    actor, "BaseTeamState", None, time_ms, packet_id, name
                )].append(integer)
        elif integer is not None and (match := TEAM_ECONOMY_RE.fullmatch(name or "")):
            slot, value_kind = match.groups()
            team_samples[(
                actor, "BombGameState.TeamEconomy", int(slot), time_ms, packet_id, value_kind
            )].append(integer)
        elif group.endswith("PurchasedItemComponent") and name in PURCHASE_FIELDS:
            purchase_updates[(actor, obj)].append((time_ms, packet_id, row, name, integer))
        elif (group.endswith("ReplayEffectComponent_ClassNetCache")
              and name == SHOT_EFFECT_ID):
            shot_times.append(time_ms)
        elif (name == WEAPON_RPC and group.startswith("/Game/Equippables/Guns/")
              and group.endswith("_ClassNetCache")):
            weapon_rpc_times[actor].append(time_ms)

    shot_times.sort()
    for times in weapon_rpc_times.values():
        times.sort()
    owner_info_controller_timelines = {
        info: _timeline(samples) for info, samples in owner_info_controllers.items()
    }
    player_controller_timelines = {
        player: _timeline(samples) for player, samples in player_controllers.items()
    }
    team_values = defaultdict(lambda: defaultdict(dict))
    ambiguous_team_loadout_packets = 0
    for (team_guid, source, slot, time_ms, packet_id, value_kind), samples in sorted(team_samples.items()):
        values = set(samples)
        if len(values) == 1:
            value = values.pop()
        else:
            value = None
            ambiguous_team_loadout_packets += 1
        team_values[(team_guid, source, slot)][(time_ms, packet_id)][value_kind] = value
    ammo_changes = []
    ambiguous_ammo_packets = 0
    weapon_rpc_tally = dict.fromkeys(WEAPON_RPC_TALLY, 0)
    for component, samples in magazine.items():
        weapon = outer_of.get(component)
        changes, ambiguous = _changes(samples)
        ambiguous_ammo_packets += ambiguous
        for time_ms, before, after, delta in changes:
            if not delta:
                continue
            rpcs = None
            if delta < 0:
                rpcs = _near(weapon_rpc_times.get(weapon, []), time_ms, 300) if weapon else None
                weapon_rpc_tally[WEAPON_RPC_TALLY[1 + min(rpcs, 2)] if weapon else "no_weapon"] += 1
            ammo_changes.append({
                "time_ms": time_ms,
                "magazine_component_guid": component,
                "weapon_guid": weapon,
                "before": before,
                "after": after,
                "delta": delta,
                "kind": "decrease" if delta < 0 else "increase",
                "round_start_within_150ms": bool(_near(round_starts, time_ms, 150)),
                # Global timing only: EffectID has no demonstrated weapon join.
                "global_effect_id_within_300ms": (
                    bool(_near(shot_times, time_ms, 300)) if delta < 0 else None
                ),
                "weapon_rpc_within_300ms": rpcs,
            })

    # Raw counter observations, joined to reload state intervals only through
    # one non-null weapon outer GUID; never a completed reload or a shot.
    positive_magazine_by_weapon = defaultdict(list)
    for component, samples in magazine.items():
        weapon = outer_of.get(component)
        if weapon is None:
            continue
        compact, _ = _collapse(samples)
        for (before_ms, before_packet, before), (time_ms, packet_id, after) in zip(compact, compact[1:]):
            reset = any(before_ms <= start <= time_ms for start in round_starts)
            if before is not None and after is not None and after > before and not reset:
                positive_magazine_by_weapon[weapon].append({
                    "time_ms": time_ms, "packet_id": packet_id,
                    "before_ms": before_ms, "before_packet_id": before_packet,
                    "magazine_component_guid": component,
                    "before": before, "after": after, "delta": after - before,
                })

    equip_intervals = []
    ambiguous_inventory_packets = 0
    for (inventory_component, inventory_actor, source_field), samples in inventory.items():
        compact, ambiguous = _collapse(samples)
        ambiguous_inventory_packets += ambiguous
        for index, (start_ms, _, weapon) in enumerate(compact):
            if weapon is None:
                continue
            end_ms = compact[index + 1][0] if index + 1 < len(compact) else None
            equip_intervals.append({
                "inventory_component_guid": inventory_component,
                "inventory_actor_guid": inventory_actor,
                "owner_guid": outer_of.get(inventory_component),
                "source_field": source_field,
                "weapon_guid": weapon,
                "from_ms": start_ms,
                "to_ms": end_ms,
                "duration_ms": end_ms - start_ms if end_ms is not None else None,
            })

    reload_intervals = []
    ambiguous_reload_packets = 0
    for component, samples in state_machine.items():
        compact, ambiguous = _collapse(samples)
        ambiguous_reload_packets += ambiguous
        open_start = open_state = None
        left_censored = True
        entry_boundary = "first_observation"
        prior_boundary = "first_observation"
        next_reset = 0

        def close_interval(end_ms, end_packet, boundary):
            nonlocal open_start, open_state
            if open_start is None:
                return
            right_censored = boundary != "state_change"
            observed_span = None if end_ms is None else end_ms - open_start[0]
            reload_intervals.append({
                "state_machine_guid": component,
                "weapon_guid": outer_of.get(component), "state_guid": open_state,
                "from_ms": open_start[0], "from_packet_id": open_start[1],
                "to_ms": end_ms, "to_packet_id": end_packet,
                "observed_span_ms": observed_span,
                "duration_ms": observed_span if not left_censored and not right_censored else None,
                "closed_by_state_change": boundary == "state_change",
                "start_boundary": entry_boundary,
                "left_censored": left_censored, "right_censored": right_censored,
                "end_boundary": boundary,
            })
            open_start = open_state = None

        for time_ms, packet_id, state in compact:
            # Round events have no packet id, so their order against an update
            # at the same time is unknown: reset the entry evidence first.
            while next_reset < len(round_starts) and round_starts[next_reset] <= time_ms:
                close_interval(round_starts[next_reset], None, "round_reset")
                prior_boundary = "after_round_reset"
                next_reset += 1
            state_path = path_of.get(state) if state is not None else None
            if state_path is None:
                boundary = "unknown_state_path" if state is not None else "ambiguous_same_packet"
                close_interval(time_ms, packet_id, boundary)
                prior_boundary = "after_" + boundary
                continue
            is_reload = state_path.rsplit("/", 1)[-1] in {"ReloadState", "ReloadStateEmpty"}
            if is_reload and open_start is None:
                open_start, open_state = (time_ms, packet_id), state
                entry_boundary = prior_boundary
                left_censored = prior_boundary != "known_nonreload_transition"
            elif not is_reload:
                close_interval(time_ms, packet_id, "state_change")
                prior_boundary = "known_nonreload_transition"
        if open_start is not None:
            if next_reset < len(round_starts):
                close_interval(round_starts[next_reset], None, "round_reset")
            else:
                close_interval(None, None, "end_of_stream")

    reload_magazine_increases = []
    for interval in reload_intervals:
        weapon = interval["weapon_guid"]
        end = interval["to_ms"]
        start_key = (interval["from_ms"], interval["from_packet_id"])
        end_key = ((end, interval.get("to_packet_id")) if end is not None else None)
        reset_crossed = interval["end_boundary"] == "round_reset"
        evidence = []
        if weapon is not None and interval["end_boundary"] == "state_change":
            evidence = [change for change in positive_magazine_by_weapon[weapon]
                        if start_key < (change["time_ms"], change["packet_id"]) < end_key]
        interval["magazine_increase_count"] = len(evidence)
        interval["magazine_increase_join"] = (
            "same_weapon_outer_guid; observed_interval" if evidence else None
        )
        interval["reset_boundary_crossed"] = reset_crossed
        for change in evidence:
            reload_magazine_increases.append({
                "state_machine_guid": interval["state_machine_guid"],
                "weapon_guid": weapon,
                "reload_from_ms": interval["from_ms"],
                "reload_from_packet_id": interval["from_packet_id"],
                "reload_to_ms": end,
                "reload_to_packet_id": interval.get("to_packet_id"),
                "magazine_component_guid": change["magazine_component_guid"],
                "time_ms": change["time_ms"],
                "packet_id": change["packet_id"],
                "before_ms": change["before_ms"],
                "before_packet_id": change["before_packet_id"],
                "before": change["before"],
                "after": change["after"],
                "delta": change["delta"],
                "evidence": "reload-state interval with same-weapon magazine increase",
            })

    progress_transitions = []
    ambiguous_defuse_packets = 0
    for bomb_guid, samples in defuse_progress.items():
        changes, ambiguous = _changes(samples)
        ambiguous_defuse_packets += ambiguous
        for time_ms, before, after, delta in changes:
            if not delta:
                continue
            progress_transitions.append({
                "time_ms": time_ms,
                "bomb_guid": bomb_guid,
                "before_seconds": before,
                "after_seconds": after,
                "delta_seconds": delta,
                "kind": "increase" if delta > 0 else "decrease",
                # Completion is the replay event below, never a threshold.
                "authoritative_completion": False,
            })

    round_balances = []
    ambiguous_balance_packets = 0
    for (info_guid, slot, value_kind), samples in balances.items():
        compact, ambiguous = _collapse(samples)
        ambiguous_balance_packets += ambiguous
        for time_ms, packet_id, value in compact:
            if value is not None:
                controller = _value_at(
                    owner_info_controller_timelines.get(info_guid, ([], [])), time_ms, packet_id
                )
                players = [
                    player for player, timeline in player_controller_timelines.items()
                    if controller not in (None, 0) and _value_at(timeline, time_ms, packet_id) == controller
                ]
                player = players[0] if len(players) == 1 else None
                join_source = (
                    "OwnerExclusivePlayerInfo.Owner@time -> BombPlayerState.Owner@time"
                    if player is not None else "unavailable"
                )
                round_balances.append({
                    "time_ms": time_ms,
                    "packet_id": packet_id,
                    "owner_exclusive_info_guid": info_guid,
                    "owner_controller_guid": controller,
                    "player_state_guid": player,
                    "player_join_source": join_source,
                    "round_info_slot": slot,
                    "value_kind": value_kind,
                    "money": value,
                })

    team_loadouts = []
    for (team_guid, source, slot), by_time in team_values.items():
        for (time_ms, _), values in sorted(by_time.items()):
            total = values.get("LoadoutValue")
            average = values.get("AverageLoadoutValue")
            team_loadouts.append({
                "time_ms": time_ms,
                "team_state_guid": team_guid,
                "team_slot": slot,
                "loadout_value": total,
                "average_loadout_value": average,
                "average_times_five_matches_total": (
                    average * 5 == total if average is not None and total is not None else None
                ),
                "source": source,
            })

    money_decreases = []
    switch_window_decreases = []
    ambiguous_money_packets = 0
    for component, samples in money.items():
        player = outer_of.get(component)
        changes, ambiguous = _changes(samples)
        ambiguous_money_packets += ambiguous
        for time_ms, before, after, delta in changes:
            if delta < 0:
                decrease = {
                    "time_ms": time_ms,
                    "money_component_guid": component,
                    "player_state_guid": player,
                    "before": before,
                    "after": after,
                    "amount": -delta,
                    "evidence": "MoneyManagementComponent.Money decrease",
                }
                window = _team_switch_window(switch_windows, time_ms)
                if window is None:
                    money_decreases.append(decrease)
                else:
                    # Kept, not dropped, and kept out of `money_by_player`
                    # below, so it is never a snapshot's nearest decrease.
                    switch_window_decreases.append({
                        **decrease,
                        "team_switch_ms": window[0],
                        "next_round_start_ms": window[1],
                        "evidence": ("MoneyManagementComponent.Money decrease between "
                                     "switchTeams and the next roundStarted (end of "
                                     "stream if none); no buy phase is open"),
                    })

    money_by_player = defaultdict(list)
    for event in money_decreases:
        if event["player_state_guid"] is not None:
            money_by_player[event["player_state_guid"]].append(event)
    transaction_snapshots = []
    ambiguous_purchase_packets = 0
    for (actor, component), updates in purchase_updates.items():
        state = {}
        previous_pair = object()
        ordered = sorted(updates)
        index = 0
        while index < len(ordered):
            time_ms, packet_id = ordered[index][:2]
            end = index + 1
            while end < len(ordered) and ordered[end][:2] == (time_ms, packet_id):
                end += 1
            packet_updates = ordered[index:end]
            # In row order; a field with two values in one packet is left out
            # of the forward-filled state rather than given a numeric winner.
            names = {update[3] for update in packet_updates}
            for name in sorted(names):
                values = {update[4] for update in packet_updates if update[3] == name}
                if len(values) != 1:
                    ambiguous_purchase_packets += 1
                    state.pop(name, None)
                else:
                    state[name] = values.pop()
            buyer = state.get("PurchasingPlayerState")
            item = state.get("Purchaseable")
            pair = (buyer, item)
            if buyer is None or item is None:
                previous_pair = object()
            elif pair != previous_pair:
                previous_pair = pair
                nearby = [event for event in money_by_player.get(buyer, [])
                          if abs(event["time_ms"] - time_ms) <= 2000]
                nearest = min(nearby, key=lambda event: abs(event["time_ms"] - time_ms), default=None)
                transaction_snapshots.append({
                    "time_ms": time_ms,
                    "source_packet_id": packet_id,
                    "round_ordinal": _round_context(round_starts, time_ms),
                    "purchased_item_component_actor_guid": actor,
                    "purchased_item_component_object_guid": component,
                    "purchasing_player_state_guid": buyer,
                    "purchaseable_guid": item,
                    "transaction_source": state.get("PurchasableTransactionSource"),
                    "nearby_money_decrease_count_2s": len(nearby),
                    "nearest_money_decrease_ms": nearest["time_ms"] if nearest else None,
                    "nearest_money_decrease_amount": nearest["amount"] if nearest else None,
                    "evidence": "PurchasedItemComponent state transition; not a purchase ledger",
                })
            index = end

    observations = {
        "schema_version": 1,
        "evidence_rules": {
        "scalar_dedup": "consecutive equal values collapse by time_ms, packet_id",
            "same_packet_conflict": "conflicting values become unknown boundaries, never value-sorted transitions",
            "ammo_shot_join": ("a decrease carries a global EffectID observation and a count "
                               "of its weapon's gun-scoped RPCs within +/-300ms; neither is a "
                               "shot count"),
            "defuse_completion": "events.spikeDefused is authoritative; progress is never completion",
            "purchase": "PurchasedItemComponent rows are snapshots; Money decreases are separate events",
            "money_team_switch": ("a Money decrease at or after switchTeams and before the "
                                  "next roundStarted (or the end of the stream) is published "
                                  "in money_decreases_in_team_switch_window, not "
                                  "money_decreases, and is never a snapshot's nearest decrease"),
        },
        "ammo_changes": _stable_rows(ammo_changes),
        "equip_intervals": _stable_rows(equip_intervals),
        "reload_intervals": _stable_rows(reload_intervals),
        "reload_magazine_increases": _stable_rows(reload_magazine_increases),
        "defuse_progress_transitions": _stable_rows(progress_transitions),
        "defuse_completions": [
            {"time_ms": time_ms, "source": "events.spikeDefused", "authoritative": True}
            for time_ms in defuse_events
        ],
        "round_balances": _stable_rows(round_balances),
        "team_loadouts": _stable_rows(team_loadouts),
        "money_decreases": _stable_rows(money_decreases),
        "money_decreases_in_team_switch_window": _stable_rows(switch_window_decreases),
        "team_switch_windows": {
            "switches": len(switch_windows),
            "closed_by_round_start": sum(end is not None for _, end in switch_windows),
            "closed_by_end_of_stream": sum(end is None for _, end in switch_windows),
        },
        "transaction_snapshots": _stable_rows(transaction_snapshots),
        "attribution_coverage": {
            "equip_owner_outer": {
                "joined": sum(row["owner_guid"] is not None for row in equip_intervals),
                "total": len(equip_intervals),
                "source": "net_guids.outer_net_guid(InventoryComponent)",
            },
            "round_balance_player": {
                "joined": sum(row["player_state_guid"] is not None for row in round_balances),
                "total": len(round_balances),
                "source": "OwnerExclusivePlayerInfo.Owner@time -> BombPlayerState.Owner@time",
            },
        },
        "ammo_decrease_weapon_rpc_within_300ms": weapon_rpc_tally,
        "ambiguous_same_packet_counts": {
            "ammo": ambiguous_ammo_packets,
            "inventory": ambiguous_inventory_packets,
            "reload": ambiguous_reload_packets,
            "defuse": ambiguous_defuse_packets,
            "round_balance": ambiguous_balance_packets,
            "team_loadout": ambiguous_team_loadout_packets,
            "money": ambiguous_money_packets,
            "purchased_item": ambiguous_purchase_packets,
        },
    }
    observations["quality_gaps"] = [gap for missing, gap in (
        (not round_balances, "RoundInfos money is unavailable in this export"),
        (not team_loadouts, "Team loadout values are unavailable in this export"),
        (not shot_times, "No firing EffectID observations were present"),
        (not defuse_events, "No authoritative spikeDefused events were present"),
    ) if missing]
    return observations


def summary(result, out):
    yield f"wrote {out}"
    for name in ("ammo_changes", "equip_intervals", "reload_intervals",
                 "defuse_progress_transitions", "defuse_completions",
                 "round_balances", "team_loadouts", "money_decreases",
                 "money_decreases_in_team_switch_window", "transaction_snapshots"):
        yield f"  {name}: {len(result[name])}"
    yield (f"  ammo decreases by weapon RPCs within 300 ms: "
           f"{json.dumps(result['ammo_decrease_weapon_rpc_within_300ms'])}")
    windows = result["team_switch_windows"]
    yield (f"  team switch windows: {windows['switches']} "
           f"({windows['closed_by_end_of_stream']} open to the end of the stream)")
    for gap in result["quality_gaps"]:
        yield f"  quality gap: {gap}"


def main(argv=None) -> int:
    return run_json_cli(__doc__, build, summary, argv, sources=[Path(__file__)], indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
