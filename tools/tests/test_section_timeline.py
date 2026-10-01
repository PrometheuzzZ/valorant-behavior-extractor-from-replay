import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase
import section_timeline as tool


def observation(ordinal, time, value=80.0, obj=2, ref=9, route="MulticastNotifyHeal", tokens=None,
                packet=None):
    return {"route": route, "identity": {"time_ms": time, "packet_id": time if packet is None else packet,
        "channel_index": 1, "actor_net_guid": 1, "object_net_guid": obj},
        "source_rows": [{"physical_row_ordinal": ordinal}], "schema_errors": [],
        "ambiguity_reasons": [], "raw_lifecycle_tokens": tokens or {},
        "section_state": {"status": "validated_array", "sections": [{"index": 0,
            "changed_component_ref": ref, "changed_component_path": None,
            "life_result": value, "delta_life": 5.0, "alive_after_change": True}],
            "relation": {"matches": True}, "eligible_for_state_comparison": True}}


def event(ordinal, time, kind="open", actor=1, channel=1, packet=None):
    return (ordinal, {"time_ms": time, "packet_id": time if packet is None else packet,
        "channel_index": channel, "actor_net_guid": actor, "event": kind})


def run(observations, actors=None):
    return tool.build({"observations": observations}, [event(0, 0)] if actors is None else actors)


def token(value, role="VictimRespawnNumber", ordinal=1):
    return {role: [{"physical_row_ordinal": ordinal, "bit_count": 32,
                   "raw_bits_hex": value.to_bytes(4, "little").hex()}]}


class TimelineTests(TempDirTestCase):
    def test_positive_adjacency_retains_source_and_unproved_life(self):
        node = run([observation(1, 1, 75.0), observation(2, 2)])["nodes"][-1]
        self.assertTrue(node["continuity"]["eligible"])
        self.assertEqual(node["previous_node_id"], [1, 0])
        self.assertEqual(node["observed_before_after_difference"], 5.0)
        self.assertTrue(node["route_arithmetic"]["matches"])
        self.assertEqual(node["continuity"]["game_life"], "unproved")
        view = node["packet_view"]
        self.assertEqual((view["game_life"], view["component_life"]), ("unproved", "unproved"))

    def test_different_actor_takes_channel(self):
        node = run([observation(1, 1), observation(2, 3)],
                   [event(0, 0), event(1, 2, actor=4)])["nodes"][-1]
        self.assertFalse(node["continuity"]["eligible"])
        for lifecycle in (node["actor_lifecycle"], node["packet_view"]["actor_lifecycle"]):
            self.assertEqual(lifecycle["channel_status"], "reopen_without_close")
            self.assertEqual(lifecycle["status"], "unresolved")

    def test_close_reopen_changes_both_endpoint_instance(self):
        node = run([observation(1, 1), observation(2, 5)],
                   [event(0, 0), event(1, 2, "close"), event(2, 3)])["nodes"][-1]
        self.assertIn("actor_channel_instance_changed", node["continuity"]["reasons"])
        self.assertIn("packet_actor_channel_instance_changed", node["packet_view"]["reasons"])
        self.assertFalse(node["continuity"]["eligible"])

    def test_prior_lifecycle_boundary_cannot_seed_verified_link(self):
        node = run([observation(1, 0), observation(2, 2)])["nodes"][-1]
        self.assertIn("prior_lifecycle_unresolved", node["continuity"]["reasons"])

    def test_lifecycle_future_regression_is_not_sorted_away(self):
        node = run([observation(1, 1), observation(2, 2)],
                   [event(0, 0), event(1, 9, "close"), event(2, 8)])["nodes"][-1]
        self.assertEqual(node["actor_lifecycle"]["actor_status"], "lifecycle_clock_duplicate_or_regression")
        self.assertFalse(node["continuity"]["eligible"])

    def test_future_trace_reopen_defect_rejected_globally(self):
        node = run([observation(1, 1)], [event(0, 0), event(5, 5)])["nodes"][0]
        self.assertEqual(node["packet_view"]["actor_lifecycle"]["actor_status"], "reopen_without_close")

    def test_duplicate_lifecycle_position_is_unresolved(self):
        node = run([observation(1, 1)], [event(0, 0), event(1, 0)])["nodes"][0]
        self.assertEqual(node["actor_lifecycle"]["status"], "unresolved")

    def test_dormancy_is_not_destruction(self):
        node = run([observation(1, 1), observation(2, 3)],
                   [event(0, 0), event(1, 2, "dormant")])["nodes"][-1]
        self.assertTrue(node["continuity"]["eligible"])

    def test_actor_object_return_even_with_other_section(self):
        node = run([observation(1, 1), observation(2, 2, obj=3, ref=10),
                    observation(3, 3)])["nodes"][-1]
        self.assertIn("persistent_barrier_epoch", node["continuity"]["reasons"])
        self.assertFalse(node["continuity"]["eligible"])

    def test_persistent_parentless_barrier_reaches_reappearing_section(self):
        gap = observation(2, 2, ref=10)
        gap["section_state"].update(status="parentless_rpc", sections=[])
        result = run([observation(1, 1), gap, observation(3, 3, ref=11), observation(4, 4)])
        self.assertEqual(len(result["barriers"]), 1)
        self.assertIn("persistent_barrier_epoch", result["nodes"][-1]["continuity"]["reasons"])

    def test_schema_error_is_barrier_even_with_valid_array(self):
        gap = observation(2, 2); gap["schema_errors"] = ["wrong scoped declaration"]
        result = run([observation(1, 1), gap, observation(3, 3)])
        self.assertEqual(len(result["barriers"]), 1)
        self.assertEqual(result["barriers"][0]["kind"], "schema_invalid")
        self.assertEqual(result["counts"]["nodes"], 2)
        self.assertFalse(result["nodes"][-1]["continuity"]["eligible"])

    def test_disjoint_raw_group_retains_values_but_censors_both_edges(self):
        item = observation(2, 2); item["ambiguity_reasons"] = ["disjoint_physical_segments"]
        nodes = run([observation(1, 1), item, observation(3, 3)])["nodes"]
        self.assertEqual(len(nodes), 3)
        self.assertIn("raw_observation_ambiguous", nodes[1]["continuity"]["reasons"])
        self.assertIn("prior_raw_observation_ambiguous", nodes[2]["continuity"]["reasons"])

    def test_all_same_time_nodes_and_next_predecessor_are_marked(self):
        nodes = run([observation(1, 1), observation(2, 1, 20.0), observation(3, 2)])["nodes"]
        self.assertTrue(nodes[0]["same_time_tie"])
        self.assertTrue(nodes[1]["same_time_tie"])
        self.assertIn("prior_tie_censor", nodes[2]["continuity"]["reasons"])

    def test_reset_has_no_incoming_delta_edge_but_absolute_state_survives(self):
        nodes = run([observation(1, 1), observation(2, 2, 100.0, route=tool.RESET),
                     observation(3, 3, 105.0)])["nodes"]
        self.assertFalse(nodes[1]["continuity"]["eligible"])
        self.assertIsNone(nodes[1]["route_arithmetic"]["matches"])
        self.assertTrue(nodes[2]["continuity"]["eligible"])
        self.assertEqual(nodes[2]["previous_node_id"], [2, 0])

    def test_regressed_node_and_outgoing_link_are_censored(self):
        nodes = run([observation(1, 3), observation(2, 2), observation(3, 4)])["nodes"]
        self.assertFalse(nodes[1]["continuity"]["eligible"])
        self.assertIn("prior_clock_regression", nodes[2]["continuity"]["reasons"])

    def test_tokens_preserve_roles_gap_and_source_ids(self):
        nodes = run([observation(1, 1, tokens=token(1)),
                     observation(2, 2, tokens=token(1, role="RespawnNumber", ordinal=2))])["nodes"]
        self.assertIn("opaque_token_gap", nodes[-1]["continuity"]["reasons"])
        # A strict barrier packet order cannot resolve stays in the packet view.
        self.assertIn("opaque_token_gap", nodes[-1]["packet_view"]["reasons"])
        self.assertEqual(nodes[-1]["raw_token_comparisons"][1]["previous"][0]["physical_row_ordinal"], 1)

    def test_equal_raw_token_does_not_prove_game_life(self):
        nodes = run([observation(1, 1, tokens=token(1)), observation(2, 2, tokens=token(1, ordinal=2))])["nodes"]
        self.assertTrue(nodes[-1]["continuity"]["eligible"])
        self.assertEqual(nodes[-1]["continuity"]["game_life"], "unproved")

    def test_changed_token_is_an_explicit_boundary(self):
        node = run([observation(1, 1, tokens=token(1)), observation(2, 2, tokens=token(2))])["nodes"][-1]
        self.assertIn("opaque_token_changed", node["continuity"]["reasons"])

    def test_cross_route_equal_words_do_not_prove_same_token_domain(self):
        node = run([observation(1, 1, tokens=token(1)),
                    observation(2, 2, tokens=token(1), route="MulticastNotifyDamage_Point")])["nodes"][-1]
        self.assertIn("opaque_token_gap", node["continuity"]["reasons"])
        self.assertIsNone(node["raw_token_comparisons"][0]["raw_equal"])

    def test_scalar_warning_preserves_zero_and_over_100_states(self):
        item = observation(2, 2, 150.0); item["section_state"]["relation"] = {"matches": False}
        item["section_state"]["eligible_for_state_comparison"] = False
        result = run([observation(1, 1, 0.0), item])
        self.assertEqual([n["life_result"] for n in result["nodes"]], [0.0, 150.0])
        self.assertTrue(result["nodes"][-1]["continuity"]["eligible"])
        self.assertEqual(result["counts"]["scalar_warnings"], 1)

    def test_arithmetic_retains_false_and_overflow_separately(self):
        result = run([observation(1, 1, 75.0), observation(2, 2, 81.0)])
        self.assertFalse(result["nodes"][-1]["route_arithmetic"]["matches"])
        self.assertEqual(result["packet_counts"]["packet_view_arithmetic_false"], 1)
        self.assertEqual(result["packet_counts"]["packet_view_eligible_arithmetic_false"], 1)
        huge = observation(2, 2, 3.4e38); huge["section_state"]["sections"][0]["delta_life"] = 3.4e38
        node = run([observation(1, 1, 3.4e38), huge])["nodes"][-1]
        self.assertEqual(node["route_arithmetic"]["status"], "f32_prediction_overflow")
        self.assertIsNone(node["route_arithmetic"]["predicted_result"])

    def test_zero_inclusive_counters_present(self):
        result = run([observation(1, 1, 75.0), observation(2, 2, 80.0)])
        self.assertEqual(result["packet_counts"]["packet_view_eligible_arithmetic_true"], 1)
        self.assertEqual(result["packet_counts"]["packet_view_eligible_arithmetic_false"], 0)
        self.assertEqual(result["strict_counts_retained"]["continuity_eligible_arithmetic_unknown"], 0)

    def test_distinct_increasing_packets_resolve_same_ms(self):
        result = run([observation(1, 10, 75.0, packet=1), observation(2, 10, packet=2)])
        node = result["nodes"][-1]
        self.assertFalse(node["continuity"]["eligible"])
        self.assertTrue(node["packet_view"]["eligible"])
        self.assertEqual(result["packet_counts"]["resolved_from_strict_ineligible"], 1)

    def test_same_packet_states_remain_unresolved(self):
        nodes = run([observation(1, 10, 75.0, packet=2), observation(2, 11, packet=2)])["nodes"]
        self.assertTrue(all("same_packet_tie" in n["packet_view"]["reasons"] for n in nodes))

    def test_prior_packet_tie_censors_next(self):
        nodes = run([observation(1, 10, packet=2), observation(2, 10, packet=2),
                     observation(3, 11, packet=3)])["nodes"]
        self.assertIn("prior_packet_tie_censor", nodes[-1]["packet_view"]["reasons"])

    def test_same_packet_actor_boundary_unresolved(self):
        node = run([observation(1, 10, packet=2)], [event(0, 0), event(2, 10, "close", packet=2)])["nodes"][0]
        self.assertEqual(node["packet_view"]["actor_lifecycle"]["actor_status"], "same_packet_boundary")

    def test_lifecycle_same_ms_different_packet_resolves(self):
        node = run([observation(2, 10, packet=2)], [event(0, 10, packet=1)])["nodes"][0]
        self.assertEqual(node["packet_view"]["actor_lifecycle"]["status"], "active")

    def test_packet_regression_duplicate_and_close_owner_conflict_rejected(self):
        coordinate = {"packet_id": 3, "actor_net_guid": 1, "channel_index": 1}
        for rows, status in (([event(0, 0, packet=2), event(1, 1, "close", packet=1)],
                              "packet_clock_duplicate_or_regression"),
                             ([event(0, 0, packet=1), event(1, 1, "close", packet=1)],
                              "packet_clock_duplicate_or_regression"),
                             ([event(0, 0), event(1, 1, "close", actor=4)], "close_identity_mismatch")):
            with self.subTest(rows=rows):
                self.assertEqual(tool.active_at_packet(rows, coordinate)[1], status)

    def test_actor_filter_preserves_ordinals_across_batches(self):
        folder = self.tmp()
        path = folder / "actors.parquet"
        rows = [{**event(i, i, "dormant")[1], "class_path": "x"} for i in range(65538)]
        rows[1]["event"] = "open"; rows[-1]["event"] = "close"
        pq.write_table(pa.Table.from_pylist(rows), path)
        actual = list(tool.actor_rows(path))
        self.assertEqual([i for i, _ in actual], [1, 65537])
