# The replay's checksum as type evidence

`tools/check_checksum_types.py` checks the overlay's types against something no
descriptor, table or decoder in this repo contributed: the `compatible_checksum`
the replay declares for every field. Unreal computes it from the property's name
and its C++ type, so recomputing it from the type vrfkit decodes either
reproduces the declared value or it does not. Figures are from the
1,018-replay declaration corpus.

## The formula

UE 5.3 `RepLayout.cpp`, `GetRepLayoutCmdCompatibleChecksum`:

```text
crc = StrCrc32(lower(name), parent)     every TCHAR fed as 4 bytes, low byte first
crc = StrCrc32(lower(cpp_type), crc)    "int32", "fvector", "aactor*", "tarray", ...
crc = MemCrc32(u32 LE static index, crc)
```

`FCrc::StrCrc32` and `MemCrc32` are the standard reflected CRC-32, so this is
`zlib.crc32` over the UTF-32LE bytes of the lower-cased strings, then the index.
`parent` is 0 for a class property or an RPC parameter. A member of a flattened
(non-NetSerialize) struct continues from its struct property's checksum, and a
`TArray`'s element from the array's -- the element has the array's name.

**Provenance.** The formula was measured on the installed 13.06 build: 194
Blueprint fields declared by eight 13.06 replays reproduce by name and
checksum. Two cross-checks:

- An independent game-file reader (outside the repo) computed 13,046 checksums
  while flattening the 13.06 classes and RPCs; `compatible_checksum` gives the
  same value on every one.
- `tools/tests/test_check_checksum_types.py` builds its own CRC-32 table,
  feeding each character as four bytes like `FCrc::StrCrc32`. It and the tool
  agree on 2,000 random inputs and on 35 checksums declared by real replays
  (top-level fields, `FTransform` members, struct members two to four levels
  deep, an array and its element, a bitfield bool); dropping any step --
  lower-casing, the UTF-32 width, the index, the parent -- breaks every vector
  it touches.

**Across builds.** The formula was measured on 13.06 declarations. On the
1,018-replay corpus it reproduces declared checksums on every one of the 24
builds, 11.06 through 13.06 (per-build counts are printed on every run), and a
property keeps its checksum across all of them -- `249` in the effect RPCs is
747197698 on each build that declares it.

## What the tool does

1. Reads every declared identity -- `(group, field name, handle, checksum)` --
   from each export's `manifest.json` and, when present, its checkpoint
   declaration tables.
2. Resolves the `FieldType` vrfkit gives it by parsing `table.rs`,
   `scoped_types.rs`, `checksum_table.rs` and the resolution constants in
   `overlay.rs`, in `overlay::resolve_entry`'s order.
3. **Tier 1.** Recomputes the checksum from the C++ spellings that `FieldType`
   can stand for (`CPP_TYPES`), under every known parent seed: 0, and the
   struct chains in `PARENT_CHAINS`. Every variant `decode.rs` declares must
   be mapped there -- an unmapped one stops the run, exit 2, before anything
   is checked. Variants that decode the same C++ type share its spelling:
   `FText` and `FTextTree` both hash as `FText`, `RotationShort` and
   `RotationByte` as `FRotator`; the checksum does not say which reader vrfkit
   uses.
4. **Tier 2.** Recovers further parent seeds from each group's own members
   (below) and re-tests what tier 1 left untestable under them.

| verdict | meaning |
|---|---|
| match | vrfkit's type reproduces the declared checksum |
| mismatch | vrfkit's type does not, but another spelling (`ALTERNATIVE_TYPES`, or an object pointer) does under the same seeds -- the checksum names a different type |
| untestable | nothing reproduces it; the checksum says nothing here, and it is never counted as a match |

Every recomputation is a 1-in-2^32 chance of an accidental reproduction, so the
tool counts them and prints `trials / 2^32`, the number of chance reproductions
to expect (0.0031 on the corpus run below).

It checks `checksum_table.rs` too. The table maps a checksum to a type with no
name, so each entry is recomputed under every name that declares its checksum.
Last, it sorts every mismatch by the [expected-mismatch list](#expected-mismatches):
one that no item names by its exact shape fails the run.

### The resolver is vrfkit's

The resolution is a Python port. Against `resolve_field_type_with_checksum`
itself, all 12,937 distinct identities the corpus declares resolve to the same
`FieldType`. On 7 real exports (11.06--13.06, main and checkpoint), every
identity it calls typed carries a `value_*` on every row and every one it
calls untyped carries none, apart from the effect-blob children the effect
decoder fills after the overlay declines them. ClassNetCache function rows
carry neither a checksum nor a value, so their fields count as function slots.

## Parent chains (tier 1)

A member of a flattened struct cannot be recomputed without its parent's
checksum. `PARENT_CHAINS` names the struct properties above the members vrfkit
types -- a property name and a C++ type each, nothing more -- with where the
fact came from. Every chain is pinned by a declared checksum in the unit tests,
and the tool prints how many typed identities each one decided.

| chain | source |
|---|---|
| `Transform: FTransform` | UE 5.3 `FTransform`; the parameter name from the 13.06 executable's reflection data |
| `SpawnTransform: FTransform` | 13.06 reflection: `AAresGameStateBase::MulticastResetForRespawn` |
| `AttachmentReplication: FRepAttachment` | UE 5.3 `AActor` |
| `Handle: FForceModuleHandle`, `TimeStamp: FNetworkedMovementTimestamp` | 13.06 reflection: `UForceModuleManagerComponent` RPCs |
| `AuthServerCorrectRepVariables: FInventoryServerCorrectRepVariables` | 13.06 reflection: `UAresInventory` |
| `EffectID` / `CurrentEffectID: FEffectID` | 13.06 reflection: the effect RPCs |
| `ServerActiveEffects: TArray > FActiveEffectInfo` (and its `EffectID`) | 13.06 reflection: `UEffectManagerComponent` |
| `AuthBlindManagerState: FBlindManagerState` | 13.06 reflection: `UBlindManagerComponent` |

Three deeper chains are formula vectors only, not `PARENT_CHAINS`:
`FBlindManagerState.ActiveBlinds > FActiveBlind.BlindEffectID`,
`UGroundVolumeComponent.FragmentInfo.Items > FGroundVolumeFragment.GridPos`
and `FActiveEffectInfo.Transform` (13.06 reflection), pinned as `BLIND`,
`FRAGMENT` and `ACTIVE_EFFECT` in `tools/tests/test_check_checksum_types.py`.

`HARDCODED_FNAMES` maps the bare FName indices the replay writes (`249` is
`Rotation`) the same way: UE 5.3 `UnrealNames.inl`, indices read back from the
13.06 executable.

## Sibling seeds (tier 2)

CRC-32 runs backwards: given the hashed suffix, the state it started from is
unique. So each member's (name, checksum) and a type for it imply exactly one
parent checksum (`implied_parent`). Members of one struct, each typed right,
imply the same parent; two differently named members implying the same one by
accident is a 1-in-2^32 event. That recovers a parent without knowing its name
or type, from the replay alone.

**The systematic false agreement.** A wrong type of the right length shifts the
implied parent by an amount fixed by where in the hashed string it is wrong and
how -- not by the name, not by the true parent. Two members wrong in the same
place and the same way still agree, at a wrong parent. The corpus has the
textbook case: `BombPlayerState`'s four GUID words `A`, `B`, `C`, `D` imply one
common parent under `int32`, `uint8`, `float` and `FName` alike -- every
five-character spelling -- because the names are all one character long. So
`SiblingSeeds` lets an agreement establish a parent only through a pair of
members that is

- **not** two names of one length given one spelling (refused whatever the
  truth is, since any truth of that length would agree the same way), and
- **not** wrong in the same place and the same way under any two spellings
  the tool knows -- every alternative, every `FieldType` spelling and every
  object pointer candidate (`deviation` keys may not intersect). This catches
  the second shape, `uint32` read as `uint64` beside `int32` read as `int64`
  with names one character apart.

A spelling of a different length cannot agree systematically at all: its shift
depends on the name's own CRC. An object reference offers only its `UClass*`
spelling towards a parent: its thousands of `A<Class>*` / `U<Class>*`
candidates would make an agreement a lottery, so they are tested at the parents
others establish and never set one. The residual blind spot is two
truths outside the tool's spelling universe that deviate identically from
their hypotheses at aligned offsets.

Tier 2 never sees `PARENT_CHAINS`, so it is also a cross-check of them. On the
corpus it re-derives 17 of the 247 chain seeds in use (one per group); 229 have
fewer than two members that agree -- `AttachmentReplication`'s members are
mostly `AttachParent` alone, an object reference -- and 1 group has fewer than
two hashable members. **0 disagree**: no member a chain decided implies a
different parent that siblings established. A disagreement fails the run,
because one of the two tiers would then be wrong about that member.

## Corpus result (1,018 replays, 24 builds)

`python tools/check_checksum_types.py --corpus <declaration corpus>`, where each
child holds one export's `manifest.json` and checkpoint declaration tables:

| | identities |
|---|---:|
| declared (group, name, handle, checksum) | 12,937 |
| ClassNetCache function slots | 1,084 |
| not typed by vrfkit (no resolution / Raw / Skip) | 6,465 / 172 / 4 |
| **typed (group, name, checksum, FieldType)** | **4,346** |
| match (top level 1,745, parent chain 292, sibling seed 388) | 2,425 |
| mismatch (all under a parent chain) | 8 |
| untestable | 1,913 |

Matches by rule: 822 exact spellings, 106 `Bool` as `uint8`, 1,494 object
references with their class, 3 as `UClass*`. Untestable by reason: 1,597 enum-capable types,
252 object references whose class is not among the candidates (or nested
without a seed), 64 with no known parent seed. Tier 2 examined 793 groups:
124 candidate agreements, 124 established, 0 refused, from 48,537 implied-parent
comparisons (0.0000 chance agreements expected). `checksum_table.rs`: 459
checksums -- 349 match, 2 mismatch, 108 untestable, 0 without a carrier.
The run made 13,140,716 recomputations: 0.0031 chance reproductions expected.

**The mismatches** -- each on every build that declares it:

| identity | vrfkit | the checksum says | listed as expected |
|---|---|---|---|
| `249` in the effect RPCs, `TransformTransitionContext`, `TransitionContext_Sequoia_X_TeleportInfo_C`, `StateContext_ActorTrailTargetingResult_C` (747197698), `MulticastResetForRespawn` (1874998526) | `VectorDouble` | `FQuat`: the X, Y, Z of `FTransform.Rotation`, not a vector or a rotator | yes: 8 identities |

The same property shows up as the 2 `checksum_table.rs` entries 747197698 and
1874998526. It changes no decoded bit: the quaternion's components are the three
doubles already decoded. What differs is the label a consumer reads them by, and
for `249` the difference is kept on purpose and listed as expected (next
section). So **the tool exits 0 on the corpus**: 0 identities and 0
`checksum_table.rs` carriers mismatch unexpectedly, beside the 8 identities and
2 carriers the list expects (both of its items matched, none STALE).

## Expected mismatches

`tools/fixtures/checksum_types_expected.json` lists the mismatches vrfkit keeps
on purpose, each with its `reason` and `evidence`. It holds one property today:
`249`, the `FQuat` of an `FTransform`, whose X, Y and Z travel as three doubles
-- `FVector`'s 192-bit layout -- and are decoded with `VectorDouble` rather
than by a quaternion type that would read the same bits (the DATA.md section
"RPC transforms: `249` is a rotation quaternion, not a rotator" documents the
member; the items' `evidence` has the measurements). One item per parent:
747197698 under `Transform: FTransform`, 1874998526 under `SpawnTransform:
FTransform`.

A list that let the run pass whatever it held would be a check that cannot
fail, so it is keyed exactly and can go stale:

- An item names one mismatch **shape**: the declared checksum, the wire name,
  the parent it reproduces under (`top level` or a `PARENT_CHAINS` label), the
  `FieldType` vrfkit decodes it with and the C++ type the checksum names. It
  covers every typed identity and `checksum_table.rs` carrier of exactly that
  shape, in any group; every other mismatch still fails the run.
- An item **applies** wherever the input declares its checksum outside the
  ClassNetCache groups. Applying and covering nothing is **STALE** and fails
  the run: the mismatch went away, or changed shape -- which then also fails
  as unlisted. An item whose checksum the input does not declare is counted as
  not applicable, the way `compare_rpc_params.py` counts an expected difference
  for another replay, so one export can still be checked on its own.
- A malformed item -- a key missing or extra, a blank `reason` or `evidence`, a
  parent that is not a chain label, a type the tool does not check, a repeated
  shape -- refuses the whole list, exit 2.
- Every run prints the unexpected and expected counts (identities / carriers)
  and the matched / STALE / not applicable item counts, zeros included, plus
  an `EXPECTED` line for each identity and carrier an item covers.

A mismatch a sibling seed decided cannot be listed: its seed label embeds the
pair of members that established it, which is no stable key, so name its chain
in `PARENT_CHAINS` first. A mismatch that is simply not fixed yet does not
belong in the list either: retype it. The tool needs a declaration corpus,
which is private, so it is not in the CI sweep; run it by hand after changing
a table or the list.

## What it cannot tell

- **Enums.** No spelling of a C++ enum (`TEnumAsByte<E>`, `E`, `E::Type`)
  reproduces, so `EnumByte`, `EnumRemainingBits`, a `Byte` that is an enum
  and a `SerializedInt` with an enum's bound are untestable. A plain `uint8`
  still matches, and a different type that reproduces is still a mismatch.
- **Object classes.** An object reference hashes as `A<Class>*` / `U<Class>*`.
  The candidates are the classes the input declares groups for plus
  `ENGINE_CLASSES`; a reference to anything else (a data asset) is untestable.
- **Nested members without a seed.** No chain, and fewer than two siblings
  that agree unambiguously: the parent is unknown. `A`/`B`/`C`/`D` above are
  this case -- under vrfkit's `UInt32` they imply four different parents,
  which rules `uint32` out for the four together, but the five-character
  truth they share cannot be told apart, so they stay untestable rather
  than becoming a mismatch with no named alternative.
- **`Bool` as `uint8`.** A bitfield bool hashes as its storage type, the same
  string as a byte; only the wire width separates them.
- **Format, scale and meaning.** A match says the declaration hashed this name
  and C++ type. It does not check how a NetSerialize type packs its bits, a
  quantization the type does not name, or what a value means.
- **Other typing paths.** Struct-blob decoders (`sink/blobs.rs`), array
  schemas and the effect-blob decoder are not overlay tables and are not
  checked. Array leaves are typed by the walker without their checksum; the
  tool resolves every declared identity with it, as the property and RPC
  paths do.
- **ClassNetCache checksums** are a different hash (functions, not
  properties) and are not recomputed.
