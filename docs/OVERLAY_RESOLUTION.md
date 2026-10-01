# Overlay resolution

Why the overlay resolution code in `crates/vrf-decode/src/overlay/` and
`crates/vrf-decode/src/types.rs` is shaped the way it is. The code keeps a
one-line summary and points here by section title.

## Why a hash index (and not binary search)

Code: `crates/vrf-decode/src/overlay/index.rs`, the hash index over the
overlay entry slices.

Resolving the reference replay's 988,995 offered rows costs about 1.5 million
`(group_path, field_name)` probes plus ~0.5 million `(group_path, handle)`
probes. As a binary search each is ~10 comparisons, and each compares group
paths that share 20 to 40 leading bytes with their neighbours: one lookup pass
cost ~180 ms of a 1.58 s export. The index is open addressing on a 64-bit key
hash with a stored 32-bit tag, so a lookup hashes once, probes once, and a
miss -- 511,881 of the 988,995 rows miss the direct probe -- ends at an empty
slot with zero string comparisons. That miss count is the cost, not the
untyped count: most of those rows are typed by the later `b`-prefix, handle,
alias and checksum steps.

The hash only chooses which entries to compare; every candidate is confirmed
by full string equality, so a collision costs time and never an answer.
`tests::overlay` asserts the index agrees with the binary search
(`lookup_by_binary_search`) on every entry, its `b`-stripped spelling and
synthetic misses. Every entry whose name starts with `b` is also inserted
under its stripped name, so the `b`-prefix fallback reuses the direct probe's
hash instead of allocating a key on every miss; stripped keys are unique
because `(group_path, field_name)` is.

## The b-prefix fallback

Code: `resolve_in_group` in `crates/vrf-decode/src/overlay.rs`, which
resolves a wire field within ONE group, and the name to report it under if
the decode later fails. Order: the declared name, then the `b`-prefixed
spelling of it, then the explicit property handle.

A table entry can spell a boolean `bDeathMontageEffectOverrideIsQueued` while
the replay declares `DeathMontageEffectOverrideIsQueued`; the table is keyed
on the name, so without this step the field stays raw. It fires only after
the direct lookup missed and only hits an entry spelled with the Unreal
boolean prefix. On 02d4d478 it resolves 632 rows, one property name on two RPC
groups: `MulticastNotifyDamage_Point` (581) and `_Base` (51).

## Fail-closed on a handle conflict

Code: `OverlayStats::handle_conflicts_refused` in
`crates/vrf-decode/src/overlay/stats.rs`, the handle fallbacks refused
because the replay declared a DIFFERENT, unresolved field name at that
handle.

The handle fallback is for handles the wire does not name, or names only as a
bare decimal FName index (`"248"`). When the replay declares a different real
name there -- the shape of a game patch moving a property, e.g. handle 7
mapped to `OldField: Int32` while the replay declares `NewField` carrying a
`Float` -- reusing the mapping would report `1.0f32` as
`value_i64 = 1065353216` with `Decode errors` still zero. Such a field stays
untyped and is counted here. It is deliberately not a `decoded_err`: nothing
failed to decode, the overlay declined to claim a type, and counting it would
move the corpus off `Decode errors: 0` for a field never decoded.

## FRepMovement finiteness is enforced

Code: `impl fmt::Display for FRepMovement` in `crates/vrf-decode/src/types.rs`.

Components are finite by construction only on the packed quantized path and
for the rotators. When `componentBitCount == 0` the decoder reads three raw
`f32`s (or `f64`s), which can spell `NaN` or an infinity; neither is a JSON
literal, so the formatter would emit invalid JSON while every counter reported
success. `DecodeError::NonFiniteComponent` rejects such a payload in
`geometry::read_quantized_vector` before it reaches the formatter, the same
call `EffectBlobError::NonFiniteFloat` makes. Anything that constructs an
`FRepMovement` by another route owes the same check.
