# Ground-area volumes: GroundVolumeComponent cells

The item schema of the `PatchVolume` windows, and a separate tool that decodes
them; Parquet output is unchanged. Figures are from the 1,018 exports of the
common audit (builds 11.06 to 13.06, checkpoints on).

```bash
python tools/extract_ground_volumes.py --export-dir <export-directory> --out-dir <new-output-directory>
```

The output directory holds `items.ndjson` (one record per decoded cell
update), `windows.ndjson` (every selected source window with its raw bits and
a status) and `receipt.json` (counts including zeros, rejection reasons, the
replay's resolved declarations and the [member names](#names) they resolve
to, input hashes before/after, and the hashes of the tool and of
`wire_bits.py`). The exit status is nonzero if any window is rejected.

## What was established

A ground-area patch actor -- the `Patch_*` classes below: molotov, slow,
net-toss and barbed-wire patches and one circular `X` patch, going by their
class names -- owns a `/Script/DynamicVolume.GroundVolumeComponent`. The
component's cells travel as the FastArray property `FragmentInfo`, handle 0 of
the replay's `GroundVolumeComponent_ClassNetCache` group. Each changed item is
one cell: an integer grid position `X`/`Y`, a polygon of world-space points
(`ConvexHullPoints`), per-point ceilings and travel distances, a floor, a
ceiling, a mean travel distance, a 3-bit `Status`, a `bIsActive` bit, the
item value the replay names `253`, and `ExteriorSegments` index pairs into the
polygon. Every name here is the one the replay itself declares. Two need a
note, both shown by the declared checksums ([Types](#types)): `253` is the
engine's name index for the member `ID`, and `X`/`Y` are the members of
`GridPos`, not of the cell itself.

On the whole corpus:

| Population | Count |
|---|---:|
| Exports / exports with windows | 1,018 / 915 |
| Selected windows, all in the main stream | 36,661 |
| -- bare `PatchVolume`, whole ClassNetCache payload | 26,632 |
| -- bare `PatchVolume`, RepLayout tail | 9,967 |
| -- declared `GroundVolumeComponent`, RepLayout tail | 62 |
| Windows decoded exactly (0 leftover bits) | 36,661 |
| Windows rejected | 0 |
| Changed items (cells) / deleted item IDs | 346,186 / 0 |
| Items carrying every member their replay declares | 346,186 |
| Array elements decoded | 4,867,849 |
| Owner actor class resolved from `actors.parquet` | 346,186 |
| Component object's outer is the owning actor (`net_guids.parquet`) | 346,186 |
| `TJunctions` members kept raw (all 16 zero bits) | 7,232 |

Neither route has a checkpoint row.

## The two routes

The parser does not decode custom-delta properties, so it preserves these
windows raw. Both kinds of window start at the first ClassNetCache field
header.

| Route | `group_path` | Windows | Why the parser leaves it raw |
|---|---|---|---|
| `bare_patch_volume` | `PatchVolume` | whole payloads (`__vrfkit_unresolved_class_net_cache_payload__`, handle u32::MAX) and tails (`__vrfkit_unparsed_rep_layout_tail__`, handle 0) | The subobject `PatchVolume` is stably named under its patch actor; the parser cannot resolve its class, groups its rows by the object name, and its RepLayout prefix rows carry no names |
| `declared_class` | `/Script/DynamicVolume.GroundVolumeComponent` | tails only | The class resolves, but the ClassNetCache tail is a custom-delta property |

`PatchVolume` is a GroundVolumeComponent by the replay's own evidence, not by
its name:

- every one of the 36,599 bare windows decodes exactly under its replay's
  GroundVolumeComponent declaration, and every member handle in them maps to a
  declared GroundVolumeComponent identity;
- every one of the 77,055 bare RepLayout prefix rows carries a handle the same
  replay declares for GroundVolumeComponent, at a width that fits the declared
  member: 1 bit for `bIsActive`/`bShouldBeAttached`, 16 or 24 for
  `AttachParent` (the widths the declared-class rows show), 64 for
  `AttachChildren`, 192 for `RelativeScale3D` (and `Translation`/`Scale3D` in
  11.06-11.09), 16 for `VolumeMaxExtentShape`, 32 for `FinalCount`;
- the decoded cells lie on a grid anchored at the owning actor's spawn
  position (below).

`tools/extract_component_classes` reached the same class from the game's
IoStore containers ([DATA.md](DATA.md#reading-component-classes-out-of-the-game),
"Not added, and why"). The same strict decoder, run on every other preserved
ClassNetCache window of all 1,018 exports (5,241,978 tried), decodes none.

Owners, by the class of the actor that owns the object:

| Owner class | Route | Objects | Cell updates |
|---|---|---:|---:|
| `Patch_Deadeye_E_Slow_Large_C` | bare | 3,150 | 132,781 |
| `Patch_Phoenix_MolotovFire_C` | bare | 3,447 | 91,131 |
| `Patch_Cable_4_NetToss_C` | bare | 775 | 36,639 |
| `Patch_Aggrobot_C_ExplodeyPatch_C` | bare | 645 | 30,246 |
| `Patch_Nox_BarbedWire_C` | bare | 486 | 23,090 |
| `Patch_Sarge_Q_Molotov_Production_C` | bare | 618 | 16,180 |
| `Patch_Pandemic_AcidMolotov_NewMolotov_C` | bare | 274 | 7,544 |
| `Patch_NetToss_C` | bare | 71 | 3,383 |
| `Patch_Pandemic_X_Circular_C` | declared | 62 | 5,192 |

Not every PatchVolume sends cells: all 3,457 `PatchVolume` objects of
`Patch_Thorne_4_SlowField_Production_C`, and 37 of
`Patch_Deadeye_E_Slow_Large_C`, replicate only the RepLayout prefix.

## Wire grammar

```text
window  := entry+                          -- until the window's last bit
entry   := handle:SerializeInt(max(slots, 2)) width:packed body[width]
body    := support:1(=1) array_key:i32 base_key:i32 deletes:i32 changed:i32
           deleted_id:i32 * deletes
           (item_id:i32 members) * changed
members := (handle+1:packed width:packed payload[width])* 0:packed
array   := count:packed (index+1:packed members)* 0:packed
```

- `slots` is the ClassNetCache group's declared slot count. The manifest does
  not record it for the main stream, so it is read from the checkpoint
  declarations: every checkpoint declaration of
  `GroundVolumeComponent_ClassNetCache` in the corpus says 2, including one in
  each of the 915 exports with windows. `SerializeInt(2)` is one bit. Handle 0
  is declared as `FragmentInfo` with checksum 2225407835 in every build.
- `body` is the FastArray grammar the investigation measured: no per-item
  checksum bit. Every window here holds exactly one entry, none deletes an
  item, and every entry changes at least one.
- `members` is Unreal's backwards-compatible property stream. The member
  handle is a GroundVolumeComponent RepLayout command index, so array element
  members have handles of their own and each array's command range ends with
  an undeclared return command.
- Every array in the corpus is sent whole: element indices run 0..count-1 in
  order, every element carries all of its members, and nothing follows the
  terminator.

## Item schema per build, from the replays' declarations

Method: for each export, the union of the main-stream declaration
(`manifest.json` `net_field_export_groups`) and every checkpoint declaration
(`checkpoint_export_groups` joined to `checkpoint_export_fields` on
`checkpoint_index` and `path_name_index`) of both groups; then, per build, the
set of (name, compatible_checksum) seen at each handle. No handle carries two
identities in any export, in any build, or between the main stream and the
checkpoints.

Member handles, element member handles in parentheses:

| Member (name, compatible_checksum) | 11.06-11.09 | 11.10-12.02 | 12.03-12.04 | 12.05-12.07 | 12.08-13.06 |
|---|---:|---:|---:|---:|---:|
| `253` (1175316786) | -- | -- | -- | -- | 23 |
| `bIsActive` (518428974) | 24 | 22 | 22 | 23 | 24 |
| `Status` (2380676387) | 25 | 23 | 23 | 24 | 25 |
| `ExteriorSegments` (3326329067); `Begin` (3658211664), `End` (1988330146) | 26 (27, 28) | 24 (25, 26) | 24 (25, 26) | 25 (26, 27) | 26 (27, 28) |
| `ConvexHullPoints` (3039966384); element (2749781999) | 30 (31) | 28 (29) | 28 (29) | 29 (30) | 30 (31) |
| `ConvexHullCeilings` (3975907906); element (1547370894) | -- | 31 (empty) | 31 (empty) | 32 (33 from 12.06) | 33 (34) |
| `ConvexHullTravelDistances` (1031017464); element (1566128181) | -- | 34 (empty) | 34 (empty) | 35 (36 from 12.06) | 36 (37) |
| `TJunctions` (1038854951) | -- | 37 | -- | -- | -- |
| `X` (2123226522) | 33 | 41 | 37 | 38 | 39 |
| `Y` (2134384775) | 34 | 42 | 38 | 39 | 40 |
| `TravelDistance` (956522941) | 35 | 43 | 39 | 40 | 41 |
| `Ceiling` (1959526051) | 36 | 44 | 40 | 41 | 42 |
| `Floor` (3454040167) | 37 | 45 | 41 | 42 | 43 |
| Declared class-group slots | 40 | 48 | 44 | 45 | 46 |

"Empty" means the array is declared and sent, but with no element in any
window of those builds, so its element identity is never declared there.
That is the only difference between 12.05 and 12.06-12.07, so the corpus has
six distinct declared maps over these five member layouts. 12.10, 12.11 and
13.00 carry no GroundVolumeComponent declaration at all.

The same member keeps its name and checksum across every build while its
handle moves by up to eight. The RepLayout prefix changes too: 11.06-11.09
declare `Translation` (20), `Scale3D` (21) and `FinalCount` (22);
`VolumeMaxExtentShape` appears at 19 from 11.10, with `FinalCount` at 20 or 21.
The tool therefore maps each handle through its own replay's declaration and
types the member by (name, checksum), never by handle.

## Types

| Member | Width | Read as | Checksum reproduces as | Evidence beyond exact consumption |
|---|---:|---|---|---|
| `253` | 32 | signed 32-bit | `ID : int32` | small integers 0..134 |
| `bIsActive` | 1 | bool | `bool` | -- |
| `Status` | 3 | unsigned | not reproduced (an enum) | values 0..3; re-sent items change only this member (below) |
| `ExteriorSegments` | array | `{Begin, End}`, 8-bit unsigned each | `TArray`; `Begin`, `End` not reproduced | every pair indexes the item's own polygon |
| `ConvexHullPoints` | array | 3 x f64 (192 bits) | `TArray` of `FVector` | grid, spawn and floor relations below |
| `ConvexHullCeilings`, `ConvexHullTravelDistances` | array | f32 each | `TArray` of `float` | one per polygon point; max / mean equal `Ceiling` / `TravelDistance` |
| `X`, `Y` | 32 | signed 32-bit | `int32` members of `GridPos : FIntPoint` | cell indices of the polygon on a regular grid |
| `TravelDistance`, `Ceiling`, `Floor` | 32 | f32 | `float` | exact relations below |
| `TJunctions` | 16 | raw, untyped | `TArray` | always 16 zero bits (7,232 of 7,232) |

The compatible checksums are type evidence. Unreal's RepLayout compatible
checksum is CRC-32 (zlib's) over the UTF-32LE lower-cased member name, then
its lower-cased C++ type, then its static array index as a little-endian
32-bit word, seeded with the checksum of the struct or array that contains
it. Seeded along the item's real parents -- `FragmentInfo :
FGroundVolumeFragmentArray` -> `Items : TArray` -> `Items :
FGroundVolumeFragment`, one step more (`GridPos : FIntPoint`) for `X`/`Y`,
an array element repeating its array's name -- it reproduces 14 of the 17
item and element identities a 13.06 replay declares, and `TJunctions` too.
Each encodes the type the tool already read, except that `TJunctions` is an
array (below). Seeding with 0 or with the array's checksum reproduces
nothing: the struct levels in between are needed.

- Not reproduced: `Status`, an enum -- no C++ spelling of an enum type
  reproduces, the failure a survey of the game's Blueprint fields also met --
  and `Begin`/`End`, byte members of `FGroundVolumeExteriorLineSegment`,
  under `uint8`, `int8`, `uint16`, `int32` and `byte`, with and without the
  struct's `F` prefix. Their types rest on the widths and relations alone.
- The ClassNetCache field's checksum, `FragmentInfo` 2225407835, is computed
  differently and does not reproduce this way. The levels above the members
  -- `FragmentInfo`, `Items`, `GridPos` -- are declared in none of the 1,018
  exports, so their own checksums never appear.
- The struct and member names are from the 13.06 game executable's
  reflection data; no game file or extract of one is in this repository.
  Every identity has the same checksum in every build, so the result is not
  specific to 13.06, and `tools/tests/test_extract_ground_volumes.py`
  recomputes each value from the names alone.

`TJunctions` stays raw: its checksum says `TArray`, and 16 zero bits are
exactly an empty array's packed count and terminator, but no element was ever
sent, so the element's identity and type are unknown.

### Names

Item `fields` keep the declared names, `253` included. The receipt's
`declarations.resolved_names` adds the game's member path wherever a replay
declares exactly these (name, checksum) pairs: `253` -> `ID`, `X` ->
`GridPos.X`, `Y` -> `GridPos.Y`. It is keyed by the pair, never by the name,
so a build whose checksum differed would not be relabelled. `ID` is not the
FastArray item ID: the two are equal on only 15 of 280 sampled items.

`Status` names come from one build. In the 13.06 executable the member's
enum is `EGroundVolumeFragmentStatus`: `AllInside` 0, `PartiallyOutside` 1,
`PartiallyBlocked` 2, `Invalid` 3, `Count` 4. The declared 3-bit width fits
a largest value of 4; two bits would stop at 3. A checksum does not encode an
enum's values, and this one does not reproduce at all, so an item's
`status_name` is set only when the replay is 13.06 and declares the 13.06
identity (`Status`, 2380676387). Every other build keeps the integer with a
null name, even under the same checksum. `Count` is the enum's count
sentinel, not a state: a 4, like a 5-7, is left unnamed. The receipt counts
`status_named`, `status_unnamed_declaration` and `status_unnamed_value`,
zeros included. These are names, not measured behaviour.

## Validation independent of the decoder

- **A second reader agrees on every value.** A separately written reader
  sharing no code with the tool agrees on all 1,018 exports: the physical
  ordinal and status of all 36,661 windows, every entry header, all 346,186
  item identities and 4,129,661 member values, including the 4,867,849 array
  elements, with 0 mismatches.
- **Separately serialized members agree.** On 12.06--13.06 `Floor` is the
  lowest polygon Z rounded to f32 on every cell (on 11.06--12.05, before the
  per-point arrays carried data, 17,872 of 17,954); `Ceiling` is the largest
  per-point ceiling and `TravelDistance` the mean per-point travel distance
  (within 1.35e-6) on every cell with points; every `ExteriorSegments` pair
  indexes its polygon.
- **The polygons sit on a grid at the owner's spawn.** Fitting a cell size
  (160 units bare, 200 declared), origin and axis-aligned frame per object,
  340,780 of 340,842 bare and 4,924 of 5,192 declared items lie inside the cell
  their own `X`/`Y` name; the grid origin is a median 0.04 units from the
  owner's spawn XY, and the declared-class frame is the owner's spawn yaw
  rounded to 90 degrees on 62 of 62 objects.
- **Time and identity.** No cell update precedes its owner's first `open` or
  follows its last `close`. The 120 updates that re-send an item ID change
  only `Status`; `253`, `X`, `Y` and `TravelDistance` never change for an ID.

`tools/tests/test_extract_ground_volumes.py` builds every fixture bit by bit
from the grammar above (no replay bytes), and each test is named for the
property it breaks when the tool is mutated.

## What is not established

- What the `Status` states do in play -- only their 13.06 names are known
  ([Names](#names)) -- and the meaning of `bIsActive` (true on 994 of 340,994
  bare cells but 5,176 of 5,192 declared ones), of `ID` (declared `253`)
  beyond its name, of the per-point travel distance, and of the elements
  `TJunctions` never sent.
- Which player or ability cast a volume belongs to, beyond its owner actor's
  class, and whether a cell's presence means an effect is applied there.
- Anything in the checkpoint stream: neither route has a checkpoint row.
- Builds outside the measured list. `ACCEPTED_BUILDS` in the tool is exactly
  the builds with windows in this corpus; 12.10, 12.11 and 13.00 have none,
  and the declared-class route has none before 12.09 or in 13.06. Rows from
  any other build are kept raw and rejected as `unvalidated_build`.
- The RepLayout prefix of the bare rows (`FinalCount`,
  `VolumeMaxExtentShape`, attachment and scale) stays unnamed in the export;
  this tool reads only the ClassNetCache windows.

Parquet is unchanged. Two parser changes would move this into the export: a
component remap of the `PatchVolume` subobject to
`/Script/DynamicVolume.GroundVolumeComponent`, which would name the prefix
rows; and a custom-delta reader for `FragmentInfo` -- the FastArray body in
`crates/vrf-decode/src/fastarray.rs` with no per-item checksum, then the
member grammar above -- emitting item members as rows typed by declared
(name, checksum). Neither is attempted here.
