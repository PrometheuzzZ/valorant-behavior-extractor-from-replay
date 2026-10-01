# Section, healing and timeline observations

Three commands derive views of the five measured `DamageableComponent` routes
from one export:

```powershell
python tools/extract_section_observations.py --export out/replay --out out/sections.json
python tools/extract_healing_observations.py --export out/replay --out out/healing.json
python tools/section_timeline.py --export out/replay --out out/timeline.json
```

| Route | Array | Serialized amount |
|---|---|---|
| MulticastNotifyDamage_Point | LifeChangeEvents | DamageTaken |
| MulticastNotifyDamage_Base | LifeChangeEvents | DamageTaken |
| MulticastNotifyHeal | LifeChangeBySection | HealTaken |
| MulticastNotifyOverhealDecay | LifeChangeBySection | DecayApplied |
| MulticastSectionLifeChange | LifeChangeEvents | No amount relation assigned |

## Shared contract

Each section records `ChangedComponent`, `LifeResult`, `DeltaLife` and
`bAliveAfterChange`; the tools check the complete raw array window and compare
every emitted member with its raw parent bits and wire order, against the
measured handle/name/checksum declarations. An observation groups rows by
time, packet, channel, actor, object, export group and outer RPC handle. That
is not an RPC invocation ID, so group counts are not unique gameplay actions.

`LifeResult` is reported section state. The amount comparison sums section
deltas in f64, rounds once to f32 and compares exact f32 bits, signed zero
included (iterative f32 accumulation is reported beside it, never
substituted): DamageTaken against the negative sum, HealTaken and
DecayApplied against the positive sum. A missing value is never filled with
100, zero or a prior state; values above 100 and zero with a true alive flag
stay as sent. None of this is effective HP, armour absorption, death or
healing credit. Checkpoint rows stay separate state evidence, never appended
to main observations or counted as actions.

Every tool hashes its input tables before and after reading and fails if one
changed; `provenance.implementation_sha256` records its own sources once.
Raw/typed integrity conflicts fail before any output; other malformed or
unsupported observations stay labelled with their source rows. The JSON write
is atomic, and `--out` is refused when it names an export table, the manifest
or one of the tool's sources.

## Sections

`extract_section_observations.py` writes `route_declarations`,
`observations`, `checkpoint_observations`, `counts` and `provenance` (schema
1). `source_rows` keeps every selected row, raw window, typed columns and
physical ordinal. Check `section_state.eligible_for_state_comparison`,
`schema_errors` and `ambiguity_reasons` before using the interpreted members;
`section_state.relation` shows a disagreeing amount rather than zeroing it.
Parentless RPCs, orphan children, non-health sections and unresolved section
references are distinguished, and `RespawnNumber`, `VictimRespawnNumber` and
`LifeChangeEventIndex` stay raw tokens. Counts include every route and zero
bucket.

## Healing

`extract_healing_observations.py` reads `MulticastNotifyHeal` through the
section parser and adds the declaration and heal-name gates, source and
recipient corroboration, and summaries of validated, unambiguous main
observations only (check `amount.status` and `ambiguity_reasons`). A missing
`HealCauser` does not invalidate a verified amount. `EventInstigatorPawn` names
an open character pawn; `EventInstigator` is that pawn's PlayerController and
never joins to `actors.parquet` -- join through the pawn's `Controller` or
`Owner`. Each of `HealCauser`, `EventInstigator` and `EventInstigatorPawn`
must carry a typed `value_i64` equal to its raw window, so an export older than
that typing fails with `untyped reference`: re-export it.
`counts.source_edge_status` tallies every edge status with zeros, because an
`invalid` edge keeps its amount validated. Recipient membership covers every
pawn a manifest player's `SpawnedCharacter` named (`tools/player_identity.py`).

## Timeline

`section_timeline.py` writes one document of main-stream `nodes`, `barriers`,
`scalar_warnings` and counts. Each node keeps its source observation, section
index, identity, values and its observed same-reference predecessor with the
numerical difference. `scalar_relation` is the extractor's amount comparison;
`route_arithmetic` tests f32(previous + DeltaLife) for damage and healing and
f32(previous - DeltaLife) for overheal decay, and resets have no incoming
edge. Mismatches and missing predecessors stay visible.

Two ordering policies decide `continuity.eligible` (time order) and
`packet_view.eligible` (packet order); `continuity.game_life` and
`component_life` are always `unproved`. Both need matching active actor and
channel open records (dormancy is not destruction); ties, regressed clocks,
actor/object and scope changes, resets, malformed observations and missing or
changed opaque tokens censor a link. Packet order also orders distinct
packets within one millisecond, but same-packet observations stay tied, and
checkpoint packet IDs are a separate counter never spliced in
(`provenance.population` is `main_only`).

## Measured corpus

All 714 exports of builds 13.01, 13.02, 13.04 and 13.05 were compared with
independent readers:

| Population | Count |
|---|---:|
| Coordinate groups / sections / source rows | 2,883,168 / 4,153,928 / 49,626,044 |
| Observations: Damage Base / Point / Heal / Overheal Decay / Section Life Change | 220,823 / 447,688 / 1,526,040 / 387,660 / 300,957 |
| Parentless observations (Damage Base / Point) | 1,536 / 99 |
| Heal groups without `HealCauser` | 7,833 |
| Timeline barriers / scalar warnings | 1,635 / 46 |
| Eligible comparisons, time / packet order | 1,874,166 / 1,892,315 |
| Eligible comparisons disagreeing with the route arithmetic, time / packet | 13 / 15 |

No checkpoint rows matched these routes. The 46 scalar warnings are all Damage
Point, each one positive f32 ULP; they agree with iterative accumulation,
which disagreed on 1,297 observations of an 11-file pilot, so neither rule is
a game algorithm. The 13 time-order disagreements are OverhealDecay ending at
a `LifeResult` of zero with a small positive one-step residual; the packet
view adds two more of the same shape. The values stay as sent. The heal edge
typing came later: on three fresh exports (13.01, 13.05, 13.06) all 7,711
heal observations validated, with zero `invalid`, `null` or `duplicate`
edges.
