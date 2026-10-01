<!-- Thanks! Please confirm the invariant checklist below. -->

## Summary

<!-- What does this change do, and why? -->

## Verification

- [ ] The pre-PR sweep in `CONTRIBUTING.md`, every command exit 0 (not `check_docs.py --fast`: the full run checks the test counts)

## Replay validation (parser changes)

<!-- Required for parsing, payload transforms, decoding, export behavior, and
new-build support. For unrelated changes, write "Not applicable".
If no replay is available, write "Not run", explain what input is needed,
and leave validation for the maintainer to complete before merging.
CI or a skipped corpus check does not replace a real replay run.
See CONTRIBUTING.md, "Replay evidence for parser changes". -->

- Game build/branch and replay count per build:
- Tested commit and exact commands (include checkpoint coverage if affected):
- Results: successful/failed replays, transform/framing/decode failures, and oracle pass rates where available:
- Before/after comparison on the same replays (include an older supported build for shared parser changes):
- Output differences, remaining failures, and scope or limitations of validation:

## Invariant checklist (skip none that apply)

- [ ] No field's `raw_bits` is dropped because its type is unknown (no skip path).
- [ ] Output is **byte-identical by committed SHA-256** on valid replays (or the measured baseline change is explained line by line).
- [ ] No `unsafe` added.
- [ ] No non-ASCII in Rust code or comments.
- [ ] No generated file (`checksum_table.rs`, `scoped_types.rs`, `native_vectors.rs`) hand-edited. `scoped_types.rs` comes from `tools/generate_scoped_types.py`; `native_vectors.rs` from `tools/capture_native_transforms.py`.
- [ ] No new hardcoded display names in a Rust crate.
