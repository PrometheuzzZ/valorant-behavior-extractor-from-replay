"""Guards for the compatible-checksum type check, which is worth running only
if four things hold, each breakable without anything else noticing:

  * the formula reproduces checksums the game itself wrote -- the vectors
    below are declared checksums from real replays, not computed values;
  * a different type string does NOT reproduce them, so a match is evidence
    of the type and not of the name alone;
  * `untestable` never reaches the match count, and every counter prints
    even at zero;
  * the expected-mismatch list excuses exactly the shapes it names: any
    other mismatch still fails, and an item that applies to the input and
    covers nothing is STALE and fails.
"""
import json
import random
import unittest
from pathlib import Path
from unittest import mock

from support import TempDirTestCase, run_cli
import check_checksum_types as cct


# A second implementation of the formula, written from UE 5.3 FCrc rather than
# from zlib: a CRC-32 table built here, `StrCrc32` feeding every TCHAR as four
# bytes low byte first, `MemCrc32` over the little-endian static index.

def _table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        table.append(c)
    return table


CRC_TABLE = _table()


def ue_str_crc32(text, crc):
    crc = ~crc & 0xFFFFFFFF
    for ch in text:
        code = ord(ch)
        for _ in range(4):
            crc = (crc >> 8) ^ CRC_TABLE[(crc ^ code) & 0xFF]
            code >>= 8
    return ~crc & 0xFFFFFFFF


def ue_mem_crc32_u32(value, crc):
    crc = ~crc & 0xFFFFFFFF
    for shift in (0, 8, 16, 24):
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ (value >> shift)) & 0xFF]
    return ~crc & 0xFFFFFFFF


def ue_checksum(name, cpp_type, static_index=0, parent=0):
    crc = ue_str_crc32(name.lower(), parent)
    crc = ue_str_crc32(cpp_type.lower(), crc)
    return ue_mem_crc32_u32(static_index, crc)


# Declared checksums from real replays (11.06-13.06), with the parent chain
# that reproduces them. Chains the overlay needs are in PARENT_CHAINS with
# their source; the formula-only ones have theirs beside the constant.
# (wire name, name hashed, C++ type, chain links, declared checksum)

TRANSFORM = (("Transform", "FTransform"),)
SPAWN_TRANSFORM = (("SpawnTransform", "FTransform"),)
HANDLE = (("Handle", "FForceModuleHandle"),)
CORRECT = (("AuthServerCorrectRepVariables", "FInventoryServerCorrectRepVariables"),)
# 13.06 reflection: FBlindManagerState.ActiveBlinds (TArray<FActiveBlind>), FActiveBlind.BlindEffectID
BLIND = (("AuthBlindManagerState", "FBlindManagerState"), ("ActiveBlinds", "TArray"),
         ("ActiveBlinds", "FActiveBlind"))
BLIND_EFFECT = BLIND + (("BlindEffectID", "FEffectID"),)
# 13.06 reflection: UGroundVolumeComponent.FragmentInfo.Items, FGroundVolumeFragment.GridPos
FRAGMENT = (("FragmentInfo", "FGroundVolumeFragmentArray"), ("Items", "TArray"),
            ("Items", "FGroundVolumeFragment"))
GRID = FRAGMENT + (("GridPos", "FIntPoint"),)
ATTACH = (("AttachmentReplication", "FRepAttachment"),)
EFFECT_ID = (("EffectID", "FEffectID"),)
ACTIVE_EFFECT = (("ServerActiveEffects", "TArray"), ("ServerActiveEffects", "FActiveEffectInfo"))

REPLAY_VECTORS = (
    # effect RPCs and TransformTransitionContext: `249` is FTransform.Rotation
    ("249", "Rotation", "FQuat", TRANSFORM, 747197698),
    ("Translation", "Translation", "FVector", TRANSFORM, 2235276067),
    ("Scale3D", "Scale3D", "FVector", TRANSFORM, 2983776962),
    ("249", "Rotation", "FQuat", SPAWN_TRANSFORM, 1874998526),
    ("HandleNumber", "HandleNumber", "uint32", HANDLE, 3336285386),
    ("CorrectionIndex", "CorrectionIndex", "int32", CORRECT, 3198546915),
    ("LastSeenClientCorrectionIndex", "LastSeenClientCorrectionIndex", "int32", CORRECT, 1076231069),
    ("StopMovementTime", "StopMovementTime", "float", (), 244888268),
    ("EffectManagerComponent", "EffectManagerComponent", "UEffectManagerComponent*", (), 1051633025),
    ("EffectID", "EffectID", "int64", EFFECT_ID, 2340855891),
    ("EffectID", "EffectID", "int64", (("CurrentEffectID", "FEffectID"),), 2251343646),
    ("NetTimestamp", "NetTimestamp", "float", (("TimeStamp", "FNetworkedMovementTimestamp"),), 259706372),
    ("StartTimeStamp", "StartTimeStamp", "float", ACTIVE_EFFECT, 3801979459),
    ("EffectID", "EffectID", "int64", ACTIVE_EFFECT + EFFECT_ID, 1129645208),
    # 13.06 reflection: FActiveEffectInfo.Transform
    ("Translation", "Translation", "FVector", ACTIVE_EFFECT + TRANSFORM, 2319708401),
    ("LongestActiveBlindDuration", "LongestActiveBlindDuration", "float", BLIND[:1], 3668710569),
    ("BlindId", "BlindId", "uint32", BLIND, 2836858544),
    ("InitialDuration", "InitialDuration", "float", BLIND, 1370668337),
    ("SourceID", "SourceID", "FName", BLIND_EFFECT, 4130766059),
    ("bLocalEffect", "bLocalEffect", "bool", BLIND_EFFECT, 2802682995),
    ("EffectID", "EffectID", "int64", BLIND_EFFECT, 3321413110),
    ("253", "ID", "int32", FRAGMENT, 1175316786),
    ("bIsActive", "bIsActive", "bool", FRAGMENT, 518428974),
    ("TravelDistance", "TravelDistance", "float", FRAGMENT, 956522941),
    ("X", "X", "int32", GRID, 2123226522),
    ("Y", "Y", "int32", GRID, 2134384775),
    ("ConvexHullPoints", "ConvexHullPoints", "TArray", FRAGMENT, 3039966384),
    # an array's element continues from the array's own declared checksum
    ("ConvexHullPoints", "ConvexHullPoints", "FVector", FRAGMENT + (("ConvexHullPoints", "TArray"),), 2749781999),
    ("AttachParent", "AttachParent", "AActor*", ATTACH, 3846863854),
    ("RelativeScale3D", "RelativeScale3D", "FVector_NetQuantize100", ATTACH, 1992268157),
    # top-level engine properties
    ("PlayerState", "PlayerState", "APlayerState*", (), 3964024390),
    ("SpawnLocation", "SpawnLocation", "FVector", (), 1139493118),
    ("248", "Location", "FVector", (), 598402184),
    # a bitfield bool (`uint8 bIsActive:1`) hashes as its storage type
    ("bIsActive", "bIsActive", "uint8", (), 2967469237),
    # a top-level Blueprint field
    ("BoundToGamePhase", "BoundToGamePhase", "bool", (), 520326154),
)


class FormulaTests(unittest.TestCase):
    def test_the_two_implementations_agree(self):
        """zlib over UTF-32LE against the per-TCHAR FCrc loop, 2,000 inputs."""
        rng = random.Random(20260928)
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_<>*: "
        for _ in range(2000):
            name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40)))
            cpp = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
            index, parent = rng.randrange(64), rng.randrange(2 ** 32)
            self.assertEqual(cct.compatible_checksum(name, cpp, index, parent),
                             ue_checksum(name, cpp, index, parent))

    def test_replay_checksums_reproduce_with_both_implementations(self):
        for wire, name, cpp, links, declared in REPLAY_VECTORS:
            with self.subTest(wire=wire, cpp=cpp):
                seed = cct.chain_checksum(links)
                self.assertEqual(cct.compatible_checksum(name, cpp, 0, seed), declared)
                ue_seed = 0
                for link_name, link_type in links:
                    ue_seed = ue_checksum(link_name, link_type, 0, ue_seed)
                self.assertEqual(ue_checksum(name, cpp, 0, ue_seed), declared)

    def test_a_different_type_string_does_not_reproduce(self):
        """A match must be evidence of the type, not of the name alone."""
        for wire, name, cpp, links, declared in REPLAY_VECTORS:
            seed = cct.chain_checksum(links)
            for other in cct.ALTERNATIVE_TYPES + ("UEffectManagerComponent*", "APlayerState*"):
                if other.lower() == cpp.lower():
                    continue
                with self.subTest(wire=wire, cpp=cpp, other=other):
                    self.assertNotEqual(cct.compatible_checksum(name, other, 0, seed), declared)

    def test_each_step_of_the_formula_is_load_bearing(self):
        """Dropping the lower-casing, the UTF-32 width, the index or the parent
        breaks every vector -- so the vectors pin the formula, not a lookalike."""
        import struct
        import zlib
        variants = {
            "no lower-casing": lambda n, t, i, p: zlib.crc32(
                struct.pack("<I", i), zlib.crc32(t.encode("utf-32-le"), zlib.crc32(n.encode("utf-32-le"), p))),
            "utf-16": lambda n, t, i, p: zlib.crc32(
                struct.pack("<I", i), zlib.crc32(t.lower().encode("utf-16-le"),
                                                 zlib.crc32(n.lower().encode("utf-16-le"), p))),
            "no index": lambda n, t, i, p: zlib.crc32(
                t.lower().encode("utf-32-le"), zlib.crc32(n.lower().encode("utf-32-le"), p)),
            "parent ignored": lambda n, t, i, p: cct.compatible_checksum(n, t, i, 0),
        }
        for label, variant in variants.items():
            reproduced = sum(variant(name, cpp, 0, cct.chain_checksum(links)) == declared
                             for _, name, cpp, links, declared in REPLAY_VECTORS)
            # Top-level vectors survive "parent ignored" by definition, and the
            # all-lower-case ones survive "no lower-casing"; nothing else may.
            if label == "parent ignored":
                self.assertEqual(reproduced, sum(1 for v in REPLAY_VECTORS if not v[3]), label)
            elif label == "no lower-casing":
                self.assertEqual(reproduced, sum(1 for v in REPLAY_VECTORS
                                                 if v[1] == v[1].lower() and v[2] == v[2].lower()
                                                 and all(a == a.lower() and b == b.lower()
                                                         for a, b in v[3])), label)
            else:
                self.assertEqual(reproduced, 0, label)

    def test_non_ascii_is_refused_rather_than_hashed(self):
        with self.assertRaises(ValueError):
            cct.compatible_checksum("Bé", "float")

    def test_every_parent_chain_fact_is_pinned_by_a_replay_vector(self):
        """A chain with a typo would silently reproduce nothing; each one must
        reproduce a declared checksum here, or be a prefix of one that does."""
        pinned = {links for *_, links, _ in REPLAY_VECTORS}
        prefixes = {links[:i] for links in pinned for i in range(1, len(links) + 1)}
        for chain in cct.PARENT_CHAINS:
            with self.subTest(chain=chain.links):
                self.assertIn(chain.links, prefixes, "no replay vector exercises this chain")


class ParseTests(unittest.TestCase):
    def test_field_types_parse_with_their_parameters(self):
        self.assertEqual(cct.parse_field_type("FieldType::Float"), ("Float", {}))
        self.assertEqual(cct.parse_field_type("FieldType::SerializedInt { max: 65536 }"),
                         ("SerializedInt", {"max": 65536}))
        multi = ("FieldType::RepMovement {\n    rotation: RotatorQuantization::ByteComponents,\n"
                 "    location: VectorQuantization::RoundWholeNumber,\n}")
        self.assertEqual(cct.canonical_type(multi),
                         "RepMovement { rotation: ByteComponents, location: RoundWholeNumber }")

    def test_every_field_type_variant_is_classified(self):
        """A new FieldType must be given a C++ spelling (or a reason) before
        this check can be trusted with it; main() refuses to run otherwise."""
        variants = cct.parse_field_type_variants(cct.DECODE_RS.read_text(encoding="utf-8"))
        self.assertIn("ObjectNetGuid", variants)
        self.assertEqual(set(variants) - set(cct.CPP_TYPES) - set(cct.UNTYPED_VARIANTS), set())

    def test_a_field_type_enum_that_does_not_parse_is_refused(self):
        """An empty read would make "every variant is classified" vacuous."""
        with self.assertRaises(cct.TableError):
            cct.parse_field_type_variants("pub enum FieldType {\n}\n")
        with self.assertRaises(cct.TableError):
            cct.parse_field_type_variants("pub enum FieldKind {\n    Raw,\n    Skip,\n}\n")

    def test_the_committed_tables_parse_whole(self):
        resolver = cct.Resolver.from_repo()
        self.assertGreater(len(resolver.entries), 1000)
        self.assertGreater(len(resolver.checksums), 400)
        self.assertIn("Owner", resolver.engine_refs)
        # every alias read out of overlay.rs must lead somewhere the table can
        # type, or the Python resolver would be porting a different order
        self.assertTrue(resolver.aliases)
        tabled_groups = {group for group, _ in resolver.entries}
        for source, target in resolver.aliases.items():
            with self.subTest(alias=source):
                self.assertTrue(source.startswith("/") and "." in source)
                self.assertIn(target, tabled_groups)

    def test_an_entry_the_pattern_misses_is_refused(self):
        src = ('pub static OVERLAY_TABLE: [OverlayEntry; 2] = [\n'
               '    OverlayEntry {\n        group_path: "/G.A",\n        field_name: "X",\n'
               '        field_type: FieldType::Float,\n    },\n'
               '    OverlayEntry {\n        group_path: "/G.A",\n        field_name: "Y",\n'
               '        field_type: Float,\n    },\n];\n'
               'pub static OVERLAY_HANDLE_TABLE: [OverlayHandleEntry; 0] = [];\n')
        with self.assertRaises(cct.TableError):
            cct.parse_overlay_table(src)
        whole = src.replace("field_type: Float", "field_type: FieldType::Float")
        entries, handles = cct.parse_overlay_table(whole)
        self.assertEqual(len(entries), 2)

    def test_a_repeated_entry_is_refused(self):
        """Counting cannot see it -- the literal is declared, held and parsed
        twice -- and a dict would keep one silently."""
        entry = ('    OverlayEntry {\n        group_path: "/G.A",\n        field_name: "X",\n'
                 '        field_type: FieldType::Float,\n    },\n')
        src = ('pub static OVERLAY_TABLE: [OverlayEntry; 2] = [\n' + entry * 2 + '];\n'
               'pub static OVERLAY_HANDLE_TABLE: [OverlayHandleEntry; 0] = [];\n')
        with self.assertRaises(cct.TableError):
            cct.parse_overlay_table(src)

    def test_resolution_constants_are_read_from_overlay_rs(self):
        src = ('const GROUP_ALIASES: &[(&str, &str)] = &[\n    (\n        "/G/A\\\n/B.B_C",\n'
               '        "/G/C.C_C",\n    ),\n];\nconst ENGINE_OBJECT_REFS: [&str; 2] = ["Owner", "Controller"];\n')
        aliases, refs = cct.parse_resolution_constants(src)
        self.assertEqual(aliases, {"/G/A/B.B_C": "/G/C.C_C"})
        self.assertEqual(refs, ("Owner", "Controller"))


def resolver(entries=None, handles=None, scoped=None, checksums=None, aliases=None,
             refs=("Owner", "Instigator", "AttachParent", "Controller")):
    return cct.Resolver(entries or {}, handles or {}, scoped or {}, checksums or {},
                        aliases or {}, refs)


class ResolverTests(unittest.TestCase):
    """The order of `overlay::resolve_entry`, on synthetic tables."""

    def test_name_then_b_prefix_then_handle(self):
        r = resolver(entries={("/G.A", "X"): "Float", ("/G.A", "bFlag"): "Bool",
                              ("/G.A", "Loc"): "VectorDouble"},
                     handles={("/G.A", 5): "Loc"})
        self.assertEqual(r.resolve("/G.A", "X", 1, 9), ("Float", "name"))
        self.assertEqual(r.resolve("/G.A", "Flag", 1, 9), ("Bool", "b-prefix"))
        self.assertEqual(r.resolve("/G.A", "248", 5, 9), ("VectorDouble", "handle"))

    def test_a_conflicting_wire_name_refuses_the_handle(self):
        r = resolver(entries={("/G.A", "Loc"): "VectorDouble"}, handles={("/G.A", 5): "Loc"})
        self.assertEqual(r.resolve("/G.A", "Other", 5, 9), (None, None))

    def test_alias_scoped_engine_reference_then_checksum_table(self):
        r = resolver(entries={("/G.Bomb", "X"): "Int32"}, aliases={"/G.Swift": "/G.Bomb"},
                     scoped={("B", "/G.P", 7): "Byte"}, checksums={7: "UInt32", 8: "Float"})
        self.assertEqual(r.resolve("/G.Swift", "X", 1, 1), ("Int32", "alias name"))
        self.assertEqual(r.resolve("/G.P", "B", 1, 7), ("Byte", "scoped"))
        self.assertEqual(r.resolve("/G.Q", "B", 1, 7), ("UInt32", "checksum table"))
        self.assertEqual(r.resolve("/G.Q", "Owner", 1, 8), ("ObjectNetGuid", "engine reference"))
        self.assertEqual(r.resolve("/G.Q", "Y", 1, 99), (None, None))


def checker(classes=("Actor", "Pawn"), chains=cct.PARENT_CHAINS):
    return cct.Checker(cct.Seeds(chains), cct.object_spellings(classes))


class ClassifyTests(unittest.TestCase):
    def test_the_right_type_matches_and_a_changed_type_string_mismatches(self):
        """The mutation the check exists for: same declaration, vrfkit's type
        changed from Float to Double -- the checksum now names `float`."""
        declared = cct.compatible_checksum("Health", "float")
        self.assertEqual(checker().classify("Float", "Health", declared).verdict, "match")
        wrong = checker().classify("Double", "Health", declared)
        self.assertEqual((wrong.verdict, wrong.detail), ("mismatch", "float"))

    def test_a_sign_or_a_quaternion_is_a_mismatch(self):
        seed = cct.chain_checksum(HANDLE)
        v = checker().classify("Int32", "HandleNumber", cct.compatible_checksum("HandleNumber", "uint32", 0, seed))
        self.assertEqual((v.verdict, v.detail), ("mismatch", "uint32"))
        v = checker().classify("VectorDouble", "249", 747197698)
        self.assertEqual((v.verdict, v.detail, v.hit.seed), ("mismatch", "FQuat", "Transform:FTransform"))

    def test_a_nested_member_is_untestable_without_its_chain(self):
        declared = 3198546915  # AresInventory.CorrectionIndex
        self.assertEqual(checker().classify("Int32", "CorrectionIndex", declared).verdict, "match")
        v = checker(chains=()).classify("Int32", "CorrectionIndex", declared)
        self.assertEqual(v.verdict, "untestable")

    def test_an_enum_is_untestable_never_a_match(self):
        declared = cct.compatible_checksum("Mode", "TEnumAsByte<EMode>")
        for field_type in ("EnumByte", "EnumRemainingBits", "Byte", "SerializedInt { max: 16 }"):
            with self.subTest(field_type=field_type):
                v = checker().classify(field_type, "Mode", declared)
                self.assertEqual((v.verdict, v.detail), ("untestable", cct.ENUM_CAPABLE))

    def test_a_type_with_no_spelling_is_untestable_whatever_reproduces(self):
        """`ByteArray` names no C++ type, so a `TArray` reproduction cannot
        contradict it."""
        declared = cct.compatible_checksum("Blob", "TArray")
        v = checker().classify("ByteArray { max_bytes: 64 }", "Blob", declared)
        self.assertEqual((v.verdict, v.detail), ("untestable", "no single C++ spelling"))

    def test_an_object_needs_its_class_among_the_candidates(self):
        declared = cct.compatible_checksum("Owner", "AActor*")
        self.assertEqual(checker().classify("ObjectNetGuid", "Owner", declared).detail, "AActor*")
        v = checker(classes=("Pawn",)).classify("ObjectNetGuid", "Owner", declared)
        self.assertEqual(v.verdict, "untestable")

    def test_bool_as_uint8_is_a_match_with_its_own_rule(self):
        v = checker().classify("Bool", "bIsActive", 2967469237)
        self.assertEqual((v.verdict, v.detail), ("match", "uint8"))
        self.assertEqual(cct.match_rule("Bool", v.hit),
                         "Bool as uint8 (bitfield bool or byte; the width decides)")

    def test_fname_indices_checksum_zero_and_non_ascii_are_untestable(self):
        self.assertEqual(checker().classify("VectorDouble", "248", 598402184).verdict, "match")
        for name, checksum, reason in (
                ("108", 615243487, "bare FName index 108 not in HARDCODED_FNAMES"),
                # a group that declares no checksum (NetworkGameplayTagNodeIndex
                # declares `248` with 0) is not a failed reproduction
                ("Health", 0, "declared checksum is 0"),
                ("Hé", 5, "non-ASCII name")):
            with self.subTest(name=name):
                v = checker().classify("Float", name, checksum)
                self.assertEqual((v.verdict, v.detail), ("untestable", reason))

    def test_every_recomputation_is_counted(self):
        c = checker()
        c.classify("Float", "Health", cct.compatible_checksum("Health", "float"))
        self.assertGreater(c.trials, 0)


def identity(group, name, handle, checksum, build="13.06"):
    return cct.Identity(group, name, handle, checksum, {build})


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.resolver = resolver(entries={("/G.A", "Health"): "Float", ("/G.A", "Mode"): "EnumByte",
                                          ("/G.A", "Raw"): "Raw"},
                                 checksums={cct.compatible_checksum("Count", "uint32"): "Int32",
                                            cct.compatible_checksum("Speed", "float"): "Float",
                                            123: "Float"})

    def test_untestable_is_never_counted_as_a_match(self):
        ids = {("/G.A", "Mode", 1, 7): identity("/G.A", "Mode", 1, 7)}
        report = cct.check_identities(ids, self.resolver, checker())
        self.assertEqual((report.by_verdict["match"], report.by_verdict["untestable"]), (0, 1))

    def test_verdicts_partition_the_typed_identities(self):
        ok = cct.compatible_checksum("Health", "float")
        ids = {
            ("/G.A", "Health", 1, ok): identity("/G.A", "Health", 1, ok),
            ("/G.A", "Mode", 2, 7): identity("/G.A", "Mode", 2, 7),
            ("/G.A", "Raw", 3, 8): identity("/G.A", "Raw", 3, 8),
            ("/G.A_ClassNetCache", "Fn", 1, 9): identity("/G.A_ClassNetCache", "Fn", 1, 9),
            ("/G.A", "Unknown", 4, 10): identity("/G.A", "Unknown", 4, 10),
        }
        report = cct.check_identities(ids, self.resolver, checker())
        self.assertEqual(report.identities, 2)
        self.assertEqual(sum(report.by_verdict.values()), 2)
        self.assertEqual(report.not_checked["not typed by vrfkit (Raw)"], 1)
        self.assertEqual(report.not_checked["function slots (ClassNetCache groups)"], 1)
        self.assertEqual(report.not_checked["not typed by vrfkit (no resolution)"], 1)

    def test_checksum_table_entries_are_checked_under_their_carriers(self):
        count = cct.compatible_checksum("Count", "uint32")
        speed = cct.compatible_checksum("Speed", "float")
        ids = {("/G.B", "Count", 1, count): identity("/G.B", "Count", 1, count),
               ("/G.B", "Speed", 2, speed): identity("/G.B", "Speed", 2, speed)}
        counts, bad = cct.check_checksum_table(ids, self.resolver, checker())
        self.assertEqual((counts["match"], counts["mismatch"], counts["no carrier in the input"]), (1, 1, 1))
        self.assertEqual([(c, n, s) for c, _, n, s, _ in bad], [(count, "Count", "uint32")])


def siblings_for(checker_=None):
    objects = checker_.objects if checker_ is not None else ()
    return cct.SiblingSeeds(cct.spelling_universe(objects))


class SiblingSeedTests(unittest.TestCase):
    """Tier 2: a parent's checksum recovered from the members that share it."""

    PARENT = 0x5EED1234

    def member(self, name, truth, parent=None):
        return cct.compatible_checksum(name, truth, 0, self.PARENT if parent is None else parent)

    def test_implied_parent_inverts_the_formula(self):
        rng = random.Random(7)
        for _ in range(300):
            name = "".join(rng.choice("abcXYZ_0123") for _ in range(rng.randint(1, 30)))
            cpp = rng.choice(cct.ALTERNATIVE_TYPES)
            index, parent = rng.randrange(8), rng.randrange(2 ** 32)
            declared = cct.compatible_checksum(name, cpp, index, parent)
            self.assertEqual(cct.implied_parent(declared, name, cpp, index), parent)

    def test_the_transform_seed_is_recovered_from_its_members(self):
        """Translation and Scale3D alone give back `Transform:FTransform`."""
        found = siblings_for().establish([("Translation", 2235276067, ("FVector",)),
                                          ("Scale3D", 2983776962, ("FVector",))])
        self.assertEqual(list(found.established), [cct.chain_checksum(TRANSFORM)])

    def test_right_types_with_different_name_lengths_establish_a_seed(self):
        s = siblings_for()
        found = s.establish([("Health", self.member("Health", "float"), ("float",)),
                             ("SourceID", self.member("SourceID", "FName"), ("FName",))])
        self.assertEqual(list(found.established), [self.PARENT])
        self.assertGreater(s.comparisons, 0)
        # top-level members agree on 0, which is known already, not recovered
        top = s.establish([("Health", cct.compatible_checksum("Health", "float"), ("float",)),
                           ("SourceID", cct.compatible_checksum("SourceID", "FName"), ("FName",))])
        self.assertEqual((top.established, top.refused), ({}, {}))

    def test_equal_name_lengths_under_one_wrong_spelling_are_refused(self):
        """The systematic false agreement: `A` and `B` are int32, both read as
        uint8 -- their implied parents agree, at a wrong seed."""
        members = [(n, self.member(n, "int32"), ("uint8",)) for n in ("A", "B")]
        s = siblings_for()
        found = s.establish(members)
        self.assertEqual(found.established, {})
        self.assertEqual(len(found.refused), 1)
        self.assertNotIn(self.PARENT, found.refused)  # it agreed on a WRONG parent
        self.assertEqual(s.stats["refused: same name length, same spelling"], 1)

    def test_rule_a_does_not_depend_on_the_truth_being_known(self):
        """The same shape with a truth no universe holds is refused too."""
        members = [(n, self.member(n, "Zzzzz"), ("uint8",)) for n in ("A", "B")]
        found = siblings_for().establish(members)
        self.assertEqual((found.established, len(found.refused)), ({}, 1))

    def test_different_name_lengths_under_one_wrong_spelling_do_not_agree(self):
        members = [(n, self.member(n, "int32"), ("uint8",)) for n in ("A", "Bb")]
        s = siblings_for()
        found = s.establish(members)
        self.assertEqual((found.established, found.refused), ({}, {}))
        self.assertEqual(s.stats["candidate agreements"], 0)

    def test_a_compensating_misplacement_is_refused(self):
        """`uint32` read as `uint64` beside `int32` read as `int64`, names one
        character apart: wrong in the same place and the same way."""
        members = [("Abc", self.member("Abc", "uint32"), ("uint64",)),
                   ("Defg", self.member("Defg", "int32"), ("int64",))]
        s = siblings_for()
        found = s.establish(members)
        self.assertEqual(found.established, {})
        self.assertEqual(s.stats["refused: an alternative pair deviates identically"], 1)

    def test_a_sibling_seed_decides_what_the_known_seeds_could_not(self):
        """No chains: 249 is untestable in tier 1, and a mismatch once its
        siblings give back the parent."""
        c = checker(chains=())
        s = siblings_for(c)
        ids = {("/S.T:Fn", n, h, ck): identity("/S.T:Fn", n, h, ck) for n, h, ck in (
            ("249", 0, 747197698), ("Translation", 1, 2235276067), ("Scale3D", 2, 2983776962))}
        r = resolver(entries={("/S.T:Fn", n): "VectorDouble" for n in ("249", "Translation", "Scale3D")})
        tier1 = cct.check_identities(ids, r, checker(chains=()))
        self.assertEqual(tier1.by_verdict["untestable"], 3)
        report = cct.check_identities(ids, r, c, s)
        self.assertEqual((report.by_verdict["match"], report.by_verdict["mismatch"]), (2, 1))
        (bad,) = report.mismatches
        self.assertEqual((bad["name"], bad["detail"]), ("249", "FQuat"))
        self.assertTrue(bad["seed"].startswith(f"sibling seed {cct.chain_checksum(TRANSFORM)} "))

    def test_a_sibling_seed_never_turns_untestable_into_a_match(self):
        c = checker(chains=())
        ids = {("/S.T", n, h, ck): identity("/S.T", n, h, ck) for n, h, ck in (
            ("Health", 1, self.member("Health", "float")),
            ("SourceID", 2, self.member("SourceID", "FName")),
            ("Mode", 3, self.member("Mode", "TEnumAsByte<EMode>")))}
        r = resolver(entries={("/S.T", "Health"): "Float", ("/S.T", "SourceID"): "FName",
                              ("/S.T", "Mode"): "EnumByte"})
        report = cct.check_identities(ids, r, c, siblings_for(c))
        self.assertEqual((report.by_verdict["match"], report.by_verdict["untestable"]), (2, 1))

    def test_the_chains_are_re_derived_and_a_disagreement_is_counted(self):
        """With the chains known, the sibling tier -- which never sees them --
        must find the same parent. Then plant a pair that makes a chain
        member imply ANOTHER established parent: that must be counted."""
        c = checker()
        ids = {("/S.T:Fn", n, h, ck): identity("/S.T:Fn", n, h, ck) for n, h, ck in (
            ("Translation", 1, 2235276067), ("Scale3D", 2, 2983776962))}
        r = resolver(entries={("/S.T:Fn", n): "VectorDouble" for n in ("Translation", "Scale3D")})
        report = cct.check_identities(ids, r, c, siblings_for(c))
        self.assertEqual(report.chain_rederived["re-derived"], 1)
        # the chain decided them first, and keeps them
        self.assertEqual({row["seed"] for row in report.rows}, {"Transform:FTransform"})

        # `bFlag` is a bool under the Transform chain; read as uint8 it implies
        # parent `other`, and two unrelated floats are made to agree on it.
        seed = cct.chain_checksum(TRANSFORM)
        flag = cct.compatible_checksum("bFlag", "bool", 0, seed)
        other = cct.implied_parent(flag, "bFlag", "uint8")
        ids = {("/S.U", n, h, ck): identity("/S.U", n, h, ck) for n, h, ck in (
            ("bFlag", 1, flag),
            ("Health", 2, cct.compatible_checksum("Health", "float", 0, other)),
            ("SourceID", 3, cct.compatible_checksum("SourceID", "float", 0, other)))}
        r = resolver(entries={("/S.U", "bFlag"): "Bool", ("/S.U", "Health"): "Float",
                              ("/S.U", "SourceID"): "Float"})
        report = cct.check_identities(ids, r, c, siblings_for(c))
        self.assertEqual(report.chain_rederived["DISAGREE: a sibling seed other than the chain's"], 1)

    def test_one_property_declared_twice_is_one_witness(self):
        """`bFlag` as a bool in one build and a uint8 bitfield in another,
        under one parent: both declarations imply it, but they are one
        property, not two members, so they establish nothing."""
        members = [("bFlag", self.member("bFlag", "bool"), ("bool", "uint8")),
                   ("bFlag", self.member("bFlag", "uint8"), ("bool", "uint8"))]
        found = siblings_for().establish(members)
        self.assertEqual((found.established, found.refused), ({}, {}))

        # Beside a second member, its two declarations still never pair with
        # each other: here every two-name pair is refused, so nothing is left.
        class RefuseAcrossNames(cct.SiblingSeeds):
            def ambiguous(self, first, second):
                return "refused for the test" if first[0] != second[0] else None

        members.append(("Health", self.member("Health", "float"), ("float",)))
        found = RefuseAcrossNames(()).establish(members)
        self.assertEqual(found.established, {})
        self.assertEqual(list(found.refused), [self.PARENT])

    def test_a_disagreement_between_the_tiers_fails_the_run(self):
        report = cct.Report(identities=1)
        self.assertEqual(cct.exit_status(report, [])[0], 0)
        report.chain_rederived[cct.DISAGREE] = 1
        code, message = cct.exit_status(report, [])
        self.assertEqual(code, 1)
        self.assertIn("disagree", message)
        self.assertEqual(cct.exit_status(cct.Report(), [])[0], 1)

    def test_the_ambiguity_universe_holds_every_spelling_the_tool_can_claim(self):
        """An agreement is weighed against every spelling the mismatch search
        itself could report; a smaller universe would let an agreement stand
        that one of the tool's own alternatives explains."""
        objects = cct.object_spellings(("Actor", "Foo_C"))
        universe = set(cct.spelling_universe(objects))
        claimable = set(cct.ALTERNATIVE_TYPES) | set(objects)
        for spec in cct.CPP_TYPES.values():
            claimable |= set(spec.expected)
        claimable |= set(cct.QUANTIZE_SPELLINGS.values()) | set(cct.SERIALIZED_INT_SPELLINGS.values())
        self.assertEqual(claimable - universe, set())
        s = cct.SiblingSeeds(cct.spelling_universe(objects))
        self.assertIn("afoo_c*", s.universe)

    def test_deviation_names_where_and_how(self):
        self.assertEqual(cct.deviation(3, "uint64", "uint32"), (7, (ord("6") ^ ord("3"), ord("4") ^ ord("2"))))
        self.assertEqual(cct.deviation(4, "int64", "int32"), (7, (ord("6") ^ ord("3"), ord("4") ^ ord("2"))))
        self.assertIsNone(cct.deviation(3, "int32", "uint32"))
        self.assertIsNone(cct.deviation(3, "FVector", "fvector"))


def write_export(root: Path, name: str, groups, build="++Ares-Core+release-13.06"):
    d = root / name
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "replay_build": build,
        "net_field_export_groups": [
            {"path": path, "fields": [{"handle": h, "name": n, "compatible_checksum": c} for h, n, c in fields]}
            for path, fields in groups.items()]}), encoding="utf-8")
    return d


def run_main(*argv):
    return run_cli(cct.main, *argv)


# Identities typed by the committed tables in ways no pending change touches:
# `AresInventory.CorrectionIndex` (Int32, by name) and the engine reference
# `Owner` (ObjectNetGuid).
INVENTORY = "/Script/ShooterGame.AresInventory"
CORRECTION_INDEX = 3198546915
OWNER = cct.compatible_checksum("Owner", "AActor*")


def write_checkpoint(d: Path, groups=((0, INVENTORY),), **fields):
    """Checkpoint 0's declaration tables: `groups` are (ordinal, path), and
    `fields` the field table's other columns."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.table({"checkpoint_index": pa.array([0] * len(groups), pa.uint32()),
                             "ordinal": pa.array([o for o, _ in groups], pa.uint32()),
                             "group_path": [path for _, path in groups]}),
                   d / "checkpoint_export_groups.parquet")
    rows = len(fields["handle"])
    pq.write_table(pa.table({"checkpoint_index": pa.array([0] * rows, pa.uint32()), **fields}),
                   d / "checkpoint_export_fields.parquet")


class MainTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()

    def test_a_clean_export_exits_0_and_prints_its_zeros(self):
        d = write_export(self.root, "e", {INVENTORY: [(30, "CorrectionIndex", CORRECTION_INDEX),
                                                      (2, "Owner", OWNER)]})
        code, out, _ = run_main("--export", str(d))
        self.assertEqual(code, 0, out)
        self.assertIn("      2  match", out)
        self.assertIn("      0  untestable", out)
        self.assertIn("      0  static-array element (index > 0)", out)
        self.assertIn(f"      0  {cct.ENUM_CAPABLE}", out)
        # the identity partition and the checksum_table.rs partition both
        # print their zero mismatch line
        self.assertEqual(out.count("      0  mismatch"), 2, out)
        self.assertIn("mismatches: 0 identities, 0 checksum_table.rs carriers", out)
        # the expected list, none of whose checksums this export declares
        self.assertIn("      0 / 0     unexpected", out)
        self.assertIn("      0 / 0     expected", out)
        n = len(committed_items())
        self.assertIn(f"expected mismatches (tools/fixtures/checksum_types_expected.json): {n} item(s)",
                      out)
        self.assertIn("      0  matched", out)
        self.assertIn("      0  STALE", out)
        self.assertIn(f"{n:>7}  not applicable", out)
        empty = self.root / "empty.json"
        empty.write_text(json.dumps({"expected": []}), encoding="utf-8")
        code, out, _ = run_main("--export", str(d), "--expected", str(empty))
        self.assertEqual(code, 0, out)
        self.assertIn("0 item(s)", out)
        for state in cct.EXPECTED_STATES:
            self.assertIn(f"      0  {state}:", out)

    def test_an_unclassified_field_type_variant_stops_the_run(self):
        """A `FieldType` variant `CPP_TYPES` does not map would otherwise be
        silently untestable: main() refuses the whole run, exit 2, and names
        the variant. Removing `FTextTree` stands in for a new variant."""
        d = write_export(self.root, "e", {INVENTORY: [(30, "CorrectionIndex", CORRECTION_INDEX)]})
        trimmed = {k: v for k, v in cct.CPP_TYPES.items() if k != "FTextTree"}
        self.assertNotEqual(len(trimmed), len(cct.CPP_TYPES), "the fixture must remove a variant")
        with mock.patch.object(cct, "CPP_TYPES", trimmed):
            code, out, err = run_main("--export", str(d))
        self.assertEqual(code, 2, out + err)
        self.assertIn("FieldType variant(s) ['FTextTree'] are not classified in CPP_TYPES", err)
        self.assertNotIn("OK:", out)

    def test_nothing_typed_is_a_failure_not_a_pass(self):
        d = write_export(self.root, "e", {"/Game/Nothing.Nothing_C": [(1, "NoSuchField", 5)]})
        code, _, err = run_main("--export", str(d))
        self.assertEqual(code, 1)
        self.assertIn("nothing checked", err)

    def test_unreadable_input_exits_2(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(run_main("--export", str(empty))[0], 2)
        self.assertEqual(run_main()[0], 2)

    def assert_unreadable(self, d: Path):
        """Exit 2 with a FAILED line: the code the docstring gives an input
        that cannot be read, never a traceback with a mismatch's 1."""
        try:
            code, out, err = run_main("--export", str(d))
        except Exception as exc:  # noqa: BLE001 -- escaping is the failure
            self.fail(f"escaped as a traceback: {exc!r}")
        self.assertEqual(code, 2, out + err)
        self.assertIn("FAILED:", err)

    def test_a_malformed_manifest_exits_2(self):
        good = {"handle": 30, "name": "CorrectionIndex", "compatible_checksum": CORRECTION_INDEX}

        def manifest(*fields):
            return json.dumps({"replay_build": "++Ares-Core+release-13.06",
                               "net_field_export_groups": [
                                   {"path": INVENTORY, "fields": list(fields)}]})

        cases = {
            "not json": "{not json",
            "not an object": "[1, 2]",
            "a field without a handle": manifest({k: v for k, v in good.items() if k != "handle"}),
            "a field that is not an object": manifest("CorrectionIndex"),
            "a null name": manifest(dict(good, name=None)),
            "a checksum as text": manifest(dict(good, compatible_checksum=str(CORRECTION_INDEX))),
            "a handle past u32": manifest(dict(good, handle=1 << 32)),
        }
        for label, text in cases.items():
            with self.subTest(label):
                d = self.root / label.replace(" ", "_")
                d.mkdir()
                (d / "manifest.json").write_text(text, encoding="utf-8")
                self.assert_unreadable(d)

    def test_an_unreadable_checkpoint_table_exits_2(self):
        d = write_export(self.root, "e", {INVENTORY: [(30, "CorrectionIndex", CORRECTION_INDEX)]})
        (d / "checkpoint_export_groups.parquet").write_bytes(b"not a parquet file")
        (d / "checkpoint_export_fields.parquet").write_bytes(b"not a parquet file")
        self.assert_unreadable(d)

    def test_a_malformed_checkpoint_declaration_exits_2(self):
        """The checkpoint tables are held to the manifest's shape: each case
        differs from the control in one column, on a row that joins its
        group (an orphan never reaches the check)."""
        import pyarrow as pa

        def export(name, handle=pa.array([30], pa.uint32()),
                   checksum=pa.array([CORRECTION_INDEX], pa.uint32()),
                   rendered_name=pa.array(["CorrectionIndex"])):
            d = write_export(self.root, name, {})
            write_checkpoint(d, group_ordinal=pa.array([0], pa.uint32()), handle=handle,
                             compatible_checksum=checksum, rendered_name=rendered_name)
            return d

        code, out, err = run_main("--export", str(export("control")))
        self.assertEqual(code, 0, out + err)
        cases = {
            "a checksum past u32": dict(checksum=pa.array([1 << 32], pa.int64())),
            "a handle past u32": dict(handle=pa.array([1 << 32], pa.int64())),
            "a checksum as text": dict(checksum=pa.array([str(CORRECTION_INDEX)])),
            "a null name": dict(rendered_name=pa.array([None], pa.string())),
        }
        for label, column in cases.items():
            with self.subTest(label):
                self.assert_unreadable(export(label.replace(" ", "_"), **column))

    def test_corpus_children_and_generated_siblings(self):
        write_export(self.root, "a", {INVENTORY: [(30, "CorrectionIndex", CORRECTION_INDEX)]})
        write_export(self.root, ".a.vrfkit-staging-1-2", {INVENTORY: [(30, "CorrectionIndex", 5)]})
        code, out, _ = run_main("--corpus", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn("inputs: 1 export(s)", out)
        self.assertIn("1 generated sibling dir(s) skipped", out)

    def test_checkpoint_declarations_are_read_and_a_duplicate_group_key_refused(self):
        import pyarrow as pa
        d = write_export(self.root, "e", {})
        fields = {"group_ordinal": pa.array([0, 1], pa.uint32()), "handle": pa.array([30, 31], pa.uint32()),
                  "compatible_checksum": pa.array([CORRECTION_INDEX, 1076231069], pa.uint32()),
                  "rendered_name": ["CorrectionIndex", "LastSeenClientCorrectionIndex"]}
        write_checkpoint(d, **fields)
        code, out, _ = run_main("--export", str(d))
        self.assertEqual(code, 0, out)
        self.assertIn("2 checkpoint declarations (1 exports carry them, 1 without a group)", out)
        self.assertIn("      1  match", out)
        write_checkpoint(d, ((0, INVENTORY), (0, INVENTORY)), **fields)
        self.assertEqual(run_main("--export", str(d))[0], 2)

    def test_json_lists_every_classified_identity(self):
        d = write_export(self.root, "e", {INVENTORY: [(30, "CorrectionIndex", CORRECTION_INDEX)]})
        out_json = self.root / "report.json"
        self.assertEqual(run_main("--export", str(d), "--json", str(out_json))[0], 0)
        rows = json.loads(out_json.read_text(encoding="utf-8"))["identities"]
        self.assertEqual([(r["name"], r["verdict"], r["seed"]) for r in rows],
                         [("CorrectionIndex", "match",
                           "AuthServerCorrectRepVariables:FInventoryServerCorrectRepVariables")])


#: `249` (Rotation) in two FTransforms: the committed list's two items.
ROTATION = 747197698        # under Transform: FTransform
SPAWN_ROTATION = 1874998526  # under SpawnTransform: FTransform
EFFECT_RPC = "/Script/ShooterGame.EffectManagerComponent:MulticastPlayContinuousEffect"
RESPAWN = "/Script/ShooterGame.AresGameStateBase:MulticastResetForRespawn"
QUAT_CARRIER = (ROTATION, "VectorDouble", "249", "FQuat", "Transform:FTransform")


def listed(**changes):
    """One item of the list as JSON holds it; `changes` replace its keys."""
    item = {"checksum": ROTATION, "name": "249", "parent": "Transform:FTransform",
            "vrfkit_type": "VectorDouble", "declared_type": "FQuat",
            "reason": "FQuat X/Y/Z decoded with VectorDouble on purpose", "evidence": "test"}
    item.update(changes)
    return item


def committed_items():
    """The committed list, read the way `main` reads it. Tests derive counts
    from it rather than pinning its size, so adding a reasoned item breaks
    nothing that is still true."""
    return cct.load_expected(cct.EXPECTED_JSON, {label for label, _ in cct.Seeds().named})


def expected_item(**changes):
    item = listed(**changes)
    return cct.ExpectedItem(**{k: item[k] for k in cct.ExpectedItem._fields})


def quat_row(group="/G.A:Fn", **changes):
    """A mismatch row as `check_identities` writes it: `249` read as VectorDouble."""
    row = {"group": group, "name": "249", "checksum": ROTATION, "handle": 0,
           "field_type": "VectorDouble", "source": "name", "verdict": "mismatch",
           "detail": "FQuat", "seed": "Transform:FTransform", "static_index": 0,
           "builds": {"13.06"}}
    row.update(changes)
    return row


class ExpectedShapeTests(unittest.TestCase):
    """`apply_expected` on synthetic rows: the committed tables play no part."""

    def test_a_listed_shape_is_expected_in_every_group_and_carrier(self):
        rows = [quat_row("/G.A:Fn"), quat_row("/G.B")]
        outcome = cct.apply_expected([expected_item()], rows, rows, [QUAT_CARRIER], {ROTATION})
        self.assertEqual((outcome.expected_rows, outcome.unexpected_rows), (rows, []))
        self.assertEqual((outcome.expected_carriers, outcome.unexpected_carriers),
                         ([QUAT_CARRIER], []))
        (result,) = outcome.results
        self.assertEqual((result.state, len(result.identities), len(result.carriers)),
                         ("matched", 2, 1))
        report = cct.Report(identities=2, mismatches=rows)
        code, message = cct.exit_status(report, [QUAT_CARRIER], outcome)
        self.assertEqual(code, 0, message)
        self.assertIn("2 identities and 1 checksum_table.rs carriers reproduce a different type "
                      "exactly as an item", message)

    def test_every_part_of_the_shape_is_load_bearing(self):
        """Change any one of the five keys and the same mismatch is unlisted,
        and the item -- whose checksum the input still declares -- is STALE."""
        other = {"checksum": ROTATION + 1, "name": "248", "parent": "SpawnTransform:FTransform",
                 "vrfkit_type": "VectorFloat", "declared_type": "FRotator"}
        self.assertEqual(set(other), set(cct.EXPECTED_SHAPE))
        row = quat_row()
        for key, value in other.items():
            with self.subTest(key=key):
                outcome = cct.apply_expected([expected_item(**{key: value})], [row], [row],
                                             [QUAT_CARRIER], {ROTATION, ROTATION + 1})
                self.assertEqual(outcome.unexpected_rows, [row])
                self.assertEqual(outcome.unexpected_carriers, [QUAT_CARRIER])
                self.assertEqual([r.state for r in outcome.results], ["STALE"])
                code, message = cct.exit_status(cct.Report(identities=1, mismatches=[row]),
                                                [QUAT_CARRIER], outcome)
                self.assertEqual(code, 1)
                self.assertIn("no item of", message)
                self.assertIn("STALE", message)

    def test_a_declared_checksum_with_no_mismatch_left_is_stale(self):
        """The mismatch went away -- the type was changed, or the name no longer
        reproduces -- while the input still declares the checksum."""
        untestable = quat_row(verdict="untestable", detail=cct.NOT_REPRODUCED, seed=None)
        outcome = cct.apply_expected([expected_item()], [untestable], [], [], {ROTATION})
        (result,) = outcome.results
        self.assertEqual(result.state, "STALE")
        self.assertIn("1 untestable as VectorDouble", result.why)
        code, message = cct.exit_status(cct.Report(identities=1), [], outcome)
        self.assertEqual(code, 1)
        self.assertIn("1 item(s) of checksum_types_expected.json are STALE", message)
        # and when vrfkit types none of its declarations at all
        outcome = cct.apply_expected([expected_item()], [], [], [], {ROTATION})
        self.assertEqual(outcome.results[0].why, "vrfkit types none of its declarations")

    def test_a_checksum_the_input_does_not_declare_leaves_the_item_not_applicable(self):
        outcome = cct.apply_expected([expected_item()], [], [], [], {ROTATION + 1})
        self.assertEqual([r.state for r in outcome.results], ["not applicable"])
        code, message = cct.exit_status(cct.Report(identities=1), [], outcome)
        self.assertEqual(code, 0, message)
        self.assertIn("(0 item(s) matched, 1 not applicable)", message)

    def test_declared_checksums_skip_function_slots(self):
        ids = {k: identity(*k) for k in (("/G.A", "X", 1, 5), ("/G.A_ClassNetCache", "Fn", 2, 6))}
        self.assertEqual(cct.declared_checksums(ids), {5})


class ExpectedListLoadTests(TempDirTestCase):
    PARENTS = {label for label, _ in cct.Seeds().named}

    def load(self, items):
        tmp = self.tmp()
        path = tmp / "expected.json"
        path.write_text(json.dumps({"expected": items}), encoding="utf-8")
        return cct.load_expected(path, self.PARENTS)

    def test_an_item_loads_with_its_type_canonical(self):
        (item,) = self.load([listed(vrfkit_type="FieldType::VectorDouble")])
        self.assertEqual(item.shape, (ROTATION, "249", "Transform:FTransform", "VectorDouble", "FQuat"))

    def test_a_malformed_item_refuses_the_whole_list(self):
        no_reason = listed()
        del no_reason["reason"]
        bad = {
            "a key missing": no_reason,
            "an extra key": listed(groups=7),
            "a blank reason": listed(reason="  "),
            "a blank evidence": listed(evidence=""),
            "a checksum as text": listed(checksum=str(ROTATION)),
            "a checksum that is a bool": listed(checksum=True),
            "a checksum of 0": listed(checksum=0),
            "a checksum past 32 bits": listed(checksum=2 ** 32),
            "a sibling-seed parent": listed(parent="sibling seed 5 (A:int32 + Bb:int32)"),
            "an unknown chain": listed(parent="Transform:FRotator"),
            "an unparsable type": listed(vrfkit_type="Vector Double"),
            "an untyped FieldType": listed(vrfkit_type="Raw"),
            "an empty name": listed(name=""),
        }
        for label, item in bad.items():
            with self.subTest(label):
                with self.assertRaises(cct.ExpectedListError):
                    self.load([item])
        with self.assertRaises(cct.ExpectedListError):
            self.load([listed(), listed(reason="the same shape again")])

    def test_a_file_that_is_not_a_list_is_refused(self):
        tmp = self.tmp()
        path = tmp / "expected.json"
        for text in ("not json", json.dumps({"items": []}), json.dumps([listed()])):
            with self.subTest(text=text[:20]):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(cct.ExpectedListError):
                    cct.load_expected(path, self.PARENTS)
        with self.assertRaises(cct.ExpectedListError):
            cct.load_expected(tmp / "absent.json", self.PARENTS)

    def test_every_committed_item_is_a_real_mismatch(self):
        """Each committed item's arithmetic, recomputed with the tool and with
        the FCrc re-implementation above: the declared type reproduces the
        checksum under the named parent, and no spelling of vrfkit's type
        does at any static index. A typo would otherwise read STALE on every
        corpus instead of saying what is wrong."""
        seeds = dict(cct.Seeds().named)
        chains = {" > ".join(f"{n}:{t}" for n, t in c.links): c.links for c in cct.PARENT_CHAINS}
        items = cct.load_expected(cct.EXPECTED_JSON, set(seeds))
        self.assertTrue(items, "the committed list is empty: nothing below would run")
        for item in items:
            with self.subTest(item=item.describe()):
                hashed = cct.HARDCODED_FNAMES.get(item.name, item.name)
                seed = seeds[item.parent]
                self.assertEqual(cct.compatible_checksum(hashed, item.declared_type, 0, seed),
                                 item.checksum)
                ue_seed = 0
                for link_name, link_type in chains.get(item.parent, ()):
                    ue_seed = ue_checksum(link_name, link_type, 0, ue_seed)
                self.assertEqual(ue_checksum(hashed, item.declared_type, 0, ue_seed), item.checksum)
                own = cct.spec_for(item.vrfkit_type).expected
                self.assertNotIn(item.declared_type, own)
                for spelling in own:
                    for index in range(cct.MAX_STATIC_INDEX + 1):
                        self.assertNotEqual(
                            cct.compatible_checksum(hashed, spelling, index, seed), item.checksum)
                self.assertTrue(item.reason.isascii() and item.evidence.isascii())


class ExpectedMainTests(TempDirTestCase):
    """End to end, through `main` and the committed tables."""

    def setUp(self):
        self.root = self.tmp()

    def write_list(self, *items) -> Path:
        path = self.root / "expected.json"
        path.write_text(json.dumps({"expected": list(items)}), encoding="utf-8")
        return path

    def rotations(self, **extra):
        return write_export(self.root, "e", {EFFECT_RPC: [(5, "249", ROTATION)],
                                             RESPAWN: [(1, "249", SPAWN_ROTATION)], **extra})

    def test_the_committed_list_covers_both_transform_rotations(self):
        d = self.rotations()
        out_json = self.root / "report.json"
        code, out, err = run_main("--export", str(d), "--json", str(out_json))
        self.assertEqual(code, 0, out + err)
        self.assertIn("mismatches: 2 identities, 2 checksum_table.rs carriers", out)
        self.assertIn("      0 / 0     unexpected", out)
        self.assertIn("      2 / 2     expected", out)
        self.assertIn("      2  matched", out)
        self.assertIn("      0  STALE", out)
        others = len(committed_items()) - 2  # items for checksums this export lacks
        self.assertIn(f"{others:>7}  not applicable", out)
        self.assertIn(f"EXPECTED {EFFECT_RPC} | 249 | {ROTATION}", out)
        self.assertIn(f"EXPECTED {RESPAWN} | 249 | {SPAWN_ROTATION}", out)
        self.assertIn(f"EXPECTED checksum_table.rs {SPAWN_ROTATION} -> VectorDouble", out)
        self.assertNotIn("MISMATCH", out)
        self.assertIn("OK:", out)
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(sorted((r["name"], r["checksum"], r["expected"]) for r in data["identities"]),
                         [("249", ROTATION, True), ("249", SPAWN_ROTATION, True)])
        self.assertEqual([c["expected"] for c in data["checksum_table_mismatches"]], [True, True])
        self.assertEqual({i["checksum"]: i["state"] for i in data["expected_items"]
                          if i["checksum"] in (ROTATION, SPAWN_ROTATION)},
                         {ROTATION: "matched", SPAWN_ROTATION: "matched"})

    def test_a_mismatch_the_list_does_not_name_still_fails(self):
        """Beside a listed mismatch, one the list lacks fails the run -- with
        the list given explicitly, and with the committed list."""
        d = self.rotations()
        code, out, err = run_main("--export", str(d), "--expected", str(self.write_list(listed())))
        self.assertEqual(code, 1)
        self.assertIn(f"MISMATCH {RESPAWN} | 249 | {SPAWN_ROTATION}", out)
        self.assertIn(f"EXPECTED {EFFECT_RPC} | 249 | {ROTATION}", out)
        self.assertIn("      1 / 1     unexpected", out)
        self.assertIn("FAILED: 1 typed identity and 1 checksum_table.rs carrier(s)", err)
        self.assertNotIn("STALE:", err)

        seed = cct.chain_checksum(CORRECT)
        wrong = cct.compatible_checksum("CorrectionIndex", "uint32", 0, seed)
        d = write_export(self.root, "f", {EFFECT_RPC: [(5, "249", ROTATION)],
                                          INVENTORY: [(30, "CorrectionIndex", wrong)]})
        code, out, err = run_main("--export", str(d))
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH /Script/ShooterGame.AresInventory | CorrectionIndex", out)
        self.assertIn("reproduces uint32 under AuthServerCorrectRepVariables", out)
        self.assertIn(f"EXPECTED {EFFECT_RPC} | 249 | {ROTATION}", out)
        self.assertIn("FAILED", err)

    def test_a_changed_shape_is_unlisted_and_leaves_its_item_stale(self):
        d = self.rotations()
        path = self.write_list(listed(parent="SpawnTransform:FTransform"),
                               listed(checksum=SPAWN_ROTATION, parent="SpawnTransform:FTransform"))
        code, out, err = run_main("--export", str(d), "--expected", str(path))
        self.assertEqual(code, 1)
        self.assertIn(f"MISMATCH {EFFECT_RPC} | 249 | {ROTATION}", out)
        self.assertIn("      1  STALE", out)
        self.assertIn("reproduces FQuat under Transform:FTransform)", out)  # the STALE line's why
        self.assertIn("no item of expected.json lists that shape", err)
        self.assertIn("1 item(s) of expected.json are STALE", err)

    def test_a_declared_checksum_that_no_longer_mismatches_fails_as_stale(self):
        """`Foo` carries 747197698: the checksum table types it VectorDouble,
        nothing reproduces it, so it is untestable -- no mismatch. The item
        applies (the checksum is declared) and covers nothing: STALE."""
        d = write_export(self.root, "e", {"/Game/X/Foo.Foo_C": [(1, "Foo", ROTATION)]})
        code, out, err = run_main("--export", str(d))
        self.assertEqual(code, 1, out)
        self.assertIn("      0  matched", out)
        self.assertIn("      1  STALE", out)
        self.assertIn(f"{len(committed_items()) - 1:>7}  not applicable", out)
        self.assertIn("the input declares 747197698; its typed identities: 1 untestable as "
                      "VectorDouble", out)
        self.assertIn("STALE", err)
        # the same export with an empty list passes: the failure was the item's
        empty = self.write_list()
        self.assertEqual(run_main("--export", str(d), "--expected", str(empty))[0], 0)

    def test_an_unreadable_list_exits_2(self):
        d = self.rotations()
        no_reason = listed()
        del no_reason["reason"]
        for path in (self.write_list(no_reason), self.root / "absent.json"):
            with self.subTest(path=path.name):
                code, _, err = run_main("--export", str(d), "--expected", str(path))
                self.assertEqual(code, 2)
                self.assertIn("FAILED: expected list", err)
