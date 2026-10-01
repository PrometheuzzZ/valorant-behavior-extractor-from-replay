import struct
import unittest
import copy

import support  # puts tools/ on sys.path
import extract_section_observations as tool
from wire_fixtures import BitWriter, field_row, packed


def row(name, raw, width, **typed):
    return field_row(dict(time_ms=1, packet_id=2, channel_index=3, actor_net_guid=4, object_net_guid=5,
                          group_path=tool.OUTER_GROUP, handle=6, field_name=name, bit_count=width,
                          raw_bits=raw), **typed)


RULES = {
    "MulticastNotifyHeal": (6, 791426194, "LifeChangeBySection", "HealTaken", 1, [1894010429, 163390906, 432306714, 1180204994, 3211876783, 2136162893]),
    "MulticastNotifyOverhealDecay": (7, 1072798296, "LifeChangeBySection", "DecayApplied", 1, [1863096949, 163390906, 432306714, 1180204994, 3211876783, 2136162893]),
    "MulticastNotifyDamage_Base": (0, 3601293819, "LifeChangeEvents", "DamageTaken", 9, [2316259323, 771135212, 1477578271, 1537455328, 4098809706, 3883502598]),
    "MulticastNotifyDamage_Point": (1, 457657198, "LifeChangeEvents", "DamageTaken", 9, [373546733, 314962236, 1435614478, 3234979, 3709621854, 3155865075]),
    "MulticastSectionLifeChange": (8, 1280092974, "LifeChangeEvents", None, 0, [None, 785272984, 1916404200, 2548634715, 1295195562, 68895389]),
}


def fixture(route="MulticastNotifyHeal", sections=None):
    """Independent bit writer for a complete parent and its emitted children."""
    if sections is None:
        sections = [(1, 50.0, -10.0 if "Damage_" in route else 10.0, True)]
    outer, outer_crc, parent, scalar, parent_handle, checksums = RULES[route]
    member_names = ("ChangedComponent", "LifeResult", "DeltaLife", "bAliveAfterChange")
    manifest_fields = [(parent_handle, parent, checksums[1])]
    manifest_fields += [(parent_handle + i + 1, n, checksums[i + 2]) for i, n in enumerate(member_names)]
    rows = []
    if scalar:
        amount = sum(s[2] for s in sections)
        if "Damage_" in route:
            amount = -amount
        amount = struct.unpack("<f", struct.pack("<f", amount))[0]
        manifest_fields.append((0, scalar, checksums[0]))
        rows.append(row(route + "." + scalar, struct.pack("<f", amount), 32,
                        value_f64=amount, compatible_checksum=checksums[0], handle=outer))
    wire = BitWriter().packed(len(sections))
    for index, (ref, life, delta, alive) in enumerate(sections):
        wire.packed(index + 1)
        for i, (name, value) in enumerate(zip(member_names, (ref, life, delta, alive))):
            if i == 0:
                payload = packed(value); width = len(payload) * 8; typed = {"value_i64": value}
            elif i == 3:
                payload = bytes([int(value)]); width = 1; typed = {"value_bool": value}
            else:
                payload = struct.pack("<f", value); width = 32; typed = {"value_f64": struct.unpack("<f", payload)[0]}
            wire.packed(parent_handle + i + 2).packed(width).raw(payload, width)
            rows.append(row(f"{route}.{parent}[{index}].{name}", payload, width, handle=outer, **typed))
        wire.packed(0)
    payload, width = wire.packed(0).to_bytes()
    rows.append(row(route + "." + parent, payload, width, handle=outer, compatible_checksum=checksums[1]))
    manifest = {"net_field_export_groups": [
        {"path": tool.OUTER_GROUP, "fields": [{"handle": outer, "name": route, "compatible_checksum": outer_crc}]},
        {"path": "/Script/ShooterGame.DamageableComponent:" + route,
         "fields": [{"handle": h, "name": n, "compatible_checksum": c} for h, n, c in manifest_fields]},
    ]}
    return route, list(enumerate(rows)), manifest


def parse_fixture(data, paths=None, segments=1):
    route, rows, manifest = data
    r = rows[0][1]
    key = tuple(r[k] for k in ("time_ms", "packet_id", "channel_index", "actor_net_guid", "object_net_guid", "group_path", "handle"))
    return tool.parse_group(route, key, rows, {1: "HealthDamageSection"} if paths is None else paths,
                            segments, tool.declarations(manifest))


class SectionObservationTests(unittest.TestCase):
    def test_all_five_complete_raw_arrays_match(self):
        for route in RULES:
            with self.subTest(route=route):
                result = parse_fixture(fixture(route))
                self.assertEqual(result["schema_errors"], [])
                self.assertEqual(result["ambiguity_reasons"], [])
                self.assertTrue(result["section_state"]["eligible_for_state_comparison"])
                self.assertEqual(result["section_state"]["sections"][0]["life_result"], 50.0)

    def test_changed_member_crc_does_not_authorize_state(self):
        data = fixture()
        data[2]["net_field_export_groups"][1]["fields"][2]["compatible_checksum"] ^= 1
        result = parse_fixture(data)
        self.assertTrue(result["schema_errors"])
        self.assertFalse(result["section_state"]["eligible_for_state_comparison"])

    def test_matching_forged_parent_crc_and_declaration_are_rejected(self):
        data = fixture()
        data[1][-1][1]["compatible_checksum"] ^= 1
        data[2]["net_field_export_groups"][1]["fields"][0]["compatible_checksum"] ^= 1
        self.assertFalse(parse_fixture(data)["section_state"]["eligible_for_state_comparison"])

    def test_unknown_child_is_retained_invalid(self):
        data = fixture()
        data[1][1][1]["field_name"] = "MulticastNotifyHeal.LifeChangeBySection[0].Unknown"
        result = parse_fixture(data)
        self.assertEqual(result["section_state"]["error"], "unknown section child name")

    def test_actual_parent_child_raw_mismatch_is_rejected(self):
        data = fixture()
        data[1][2][1].update(raw_bits=struct.pack("<f", 51.0), value_f64=51.0)
        result = parse_fixture(data)
        self.assertEqual(result["section_state"]["status"], "invalid")
        self.assertEqual(result["section_state"]["error"], "parent/child raw mismatch")

    def test_orphan_children_are_not_parentless_rpc(self):
        route, rows, manifest = fixture()
        result = parse_fixture((route, rows[:-1], manifest))
        self.assertEqual(result["section_state"]["error"], "orphan section children without parent")

    def test_strict_bool_raw_typed_mismatch(self):
        data = fixture()
        data[1][4][1]["value_bool"] = 1
        self.assertEqual(parse_fixture(data)["section_state"]["error"], "bool typed/raw mismatch")

    def test_reordering_children_changes_wire_order(self):
        route, rows, manifest = fixture()
        rows[1], rows[2] = (1, rows[2][1]), (2, rows[1][1])
        result = parse_fixture((route, rows, manifest))
        self.assertEqual(result["section_state"]["error"], "child physical order differs from parent wire order")

    def test_physical_gap_preserves_wire_order_but_rejects_contiguity(self):
        route, rows, manifest = fixture()
        rows = [(o + (1 if o >= 3 else 0), r) for o, r in rows]
        result = parse_fixture((route, rows, manifest))
        self.assertEqual(result["section_state"]["error"], "children are not contiguous immediately before parent")

    def test_changed_coordinates_do_not_authorize_state(self):
        data = fixture()
        data[1][2][1]["object_net_guid"] = 999
        result = parse_fixture(data)
        self.assertFalse(result["section_state"]["eligible_for_state_comparison"])

    def test_unknown_and_known_nonhealth_and_ambiguous_health(self):
        data = fixture()
        self.assertEqual(parse_fixture(data, {})["section_state"]["health_section_status"], "unknown_section_identity")
        self.assertEqual(parse_fixture(data, {1: "NotHealthDamageSection"})["section_state"]["health_section_status"], "array_without_health_section")
        two = fixture(sections=[(1, 10.0, 1.0, True), (2, 20.0, 1.0, True)])
        self.assertEqual(parse_fixture(two, {1: "HealthDamageSection", 2: "HealthDamageSection"})["section_state"]["health_section_status"], "multiple_exact_health_sections")

    def test_duplicate_outer_handle_is_rejected(self):
        data = fixture()
        fields = data[2]["net_field_export_groups"][0]["fields"]
        fields.append(copy.deepcopy(fields[0]))
        with self.assertRaisesRegex(tool.InputError, "duplicate outer declaration handle"):
            tool.declarations(data[2])

    def test_f32_rejects_conflicting_typed_column_and_preserves_signed_zero(self):
        wire = struct.pack("<f", -0.0)
        self.assertEqual(struct.pack("<f", tool.f32(row("x", wire, 32, value_f64=-0.0))), wire)
        with self.assertRaises(tool.IntegrityError):
            tool.f32(row("x", wire, 32, value_f64=0.0))
        with self.assertRaises(tool.InputError):
            tool.f32(row("x", wire, 32, value_f64=-0.0, value_i64=0))
        # A typed value that rounds to the wire f32 is still not that value.
        with self.assertRaises(tool.IntegrityError):
            tool.f32(row("x", struct.pack("<f", 1.0), 32, value_f64=1.0 + 1e-9))

    def test_a_typed_raw_conflict_in_a_group_stops_the_parse(self):
        data = fixture()
        data[1][0][1]["value_f64"] = 2.0
        with self.assertRaises(tool.IntegrityError):
            parse_fixture(data)

    def test_parentless_rpc_is_visible_not_missing_health(self):
        scalar = row("MulticastNotifyHeal.HealTaken", struct.pack("<f", 5.0), 32,
                     compatible_checksum=1894010429, value_f64=5.0)
        declared = {route: {"_fields": {}, "outer": {"name": route, "compatible_checksum": tool.ROUTES[route][1]}} for route in tool.ROUTES}
        declared["MulticastNotifyHeal"]["_fields"] = {0: ("HealTaken", 1894010429)}
        got = tool.parse_group("MulticastNotifyHeal", (1,2,3,4,5,tool.OUTER_GROUP,6), [(9, scalar)], {}, 1, declared)
        self.assertEqual(got["section_state"]["status"], "parentless_rpc")
        self.assertEqual(got["section_state"]["scalar"], 5.0)
        self.assertEqual(got["source_rows"][0]["physical_row_ordinal"], 9)

    def test_parent_child_payload_mismatch_is_retained_invalid(self):
        # A valid-looking parent with no emitted flattened leaf cannot silently
        # become a zero section array.
        parent = row("MulticastNotifyHeal.LifeChangeBySection", packed(1, 1, 3, 8, 2, 0, 0), 56,
                     compatible_checksum=163390906)
        declared = {route: {"_fields": {}, "outer": {"name": route, "compatible_checksum": tool.ROUTES[route][1]}} for route in tool.ROUTES}
        declared["MulticastNotifyHeal"]["_fields"] = {1: ("LifeChangeBySection", 163390906)}
        got = tool.parse_group("MulticastNotifyHeal", (1,2,3,4,5,tool.OUTER_GROUP,6), [(4, parent)], {}, 1, declared)
        self.assertEqual(got["section_state"]["status"], "invalid")
        self.assertIn("array_validation_failed", got["ambiguity_reasons"])

    def test_packed_reference_requires_exact_typed_value(self):
        payload = packed(60)
        with self.assertRaises(tool.IntegrityError):
            tool.reference(row("x", payload, 8, value_i64=61))
