# AbilitiesAndBuffs FastArray observations

`AbilitiesAndBuffsComponent` ClassNetCache handle-1 payloads are custom-delta
FastArray bodies. They carry replication bookkeeping and numeric property
windows, not a `PredictionKey`, an ability cast or a named property; nothing
here adds typed Parquet values.

## Body grammar

After the outer framing, a body is a custom-delta support bit (always 1), four
signed little-endian i32 words -- ArrayReplicationKey, BaseReplicationKey,
NumDeletes, NumChanged -- the deleted item IDs, then each changed item's ID and
its packed handle/width property stream. There is **no checksum bit before a
changed item's property stream**. A checksum-present reader also closes some
bodies (28,429 of 3,293,206 with changed items on the main route), so trying
variants and accepting the first per-row success is unsafe; every build but
13.00 has thousands of windows only the no-checksum variant closes. Item IDs
are FastArray IDs, not actor NetGUIDs.

## Extracting the numeric observations

```bash
python tools/extract_fastarray_observations.py --export-dir <export-directory> --out-dir <new-output-directory>
```

The new directory holds `observations.ndjson` and `receipt.json`. Each
observation keeps its source table, physical row ordinal, packet/channel/
actor/object coordinates, the original raw bits, the header, deleted IDs, and
changed IDs with zero-based numeric handles and bit offsets/lengths into the
original window, so raw values can be sliced without guessing types. Each
record carries its `route` and stream (`population`):

| Route | Exported identity (handle 1) | Main `fields` | `checkpoint_fields` |
|---|---|---|---|
| `cnc_h1` | `AbilitiesAndBuffsComponent` / `_cnc_h1` | 22 builds (none on 12.10, 12.11) | none observed, unvalidated |
| `chained_cnc_h1` | `/Script/ShooterGame.AresAbilitySystemComponent` / `__vrfkit_chained_cnc_h1__` | all 24 builds | all 24 builds |

The chained rows are the same handle-1 payload recovered from a RepLayout tail
(`on_rep_layout_tail` in `crates/vrfkit/src/sink/stream.rs`), so one decoder
reads both routes. A row from an unlisted build or an unvalidated stream keeps
its raw bits with a named rejection (`unvalidated_build`,
`unvalidated_checkpoint_route`) and the exit is nonzero. The receipt (schema 2)
counts rows, exact walks, rejections, items and fields for every route and
stream with zeros printed, plus rows carrying a route's field name under any
other group, which are never selected. Output must be a new directory outside
the export.

On all 1,018 exports the extractor produced 4,430,654 observations (`cnc_h1`
main 3,999,493; chained main 250,053; chained checkpoint 181,108) with zero
rejections, and an independent integer-shift reader agreed with every
boundary. Every one of the 3,829,374 changed items carries the same fifteen
zero-based handles in the same order -- 26, 27, 33 to 38, 41, 42, 45 to 48 and
51 -- which checks alignment, not a schema. 13.00's `cnc_h1` entry rests on six
windows in one replay; re-measure it first when more are available.

## Not established

- The changed items' property schemas and value meanings. Do not promote raw
  widths or GUID-number coincidences to semantics; repeated updates are not
  distinct abilities, effects or casts, and parent and inner rows overlap.
- A `_cnc_h1` checkpoint stream (the chained route has both).
- `AresAbilitySystemComponent.OwnerActor` / `AvatarActor` equal the enclosing
  actor GUID on all 629,578 measured values, so they name no distinct owner
  for a heal or ability instigator.

`PatchVolume` windows use the same grammar; their schema is the declared
`GroundVolumeComponent.FragmentInfo` cell, decoded by
[`extract_ground_volumes.py`](GROUND_VOLUMES.md).
