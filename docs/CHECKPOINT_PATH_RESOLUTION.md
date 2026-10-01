# Checkpoint path resolution

Checkpoint GUID entries encode a path either as a literal string or as an
integer. vrfkit resolves the integer against a checkpoint-local literal path
table. The rule is:

1. Start with an empty table for each checkpoint index.
2. Append each literal path when its GUID entry is read.
3. For an indexed entry, use `name_index` as a zero-based position in the
   literals that appeared earlier in that checkpoint.
4. Do not append indexed entries to the literal table.
5. Register the resolved path in the checkpoint GUID cache. A later entry for
   the same GUID replaces its path and outer-GUID association.

The default checkpoint readers and sink path use
`CheckpointPathMode::LiteralPathTable`. A caller that needs the historical
decimal rendering can explicitly select `CheckpointPathMode::LegacyDecimal`.
The export manifest identifies the default as
`checkpoint_path_resolution_mode = "preceding_literal_zero_based"` and reports
literal, indexed, and resolved-index counts. The CLI prints the same three
counts for every checkpoint export.

`checkpoint_guid_entries.parquet` remains the raw declaration record. Its
`path_is_string`, `literal_path`, `name_index`, `outer_net_guid`, `flags`, and
ordinal are unchanged. `checkpoint_net_guids.parquet` records the final cache,
after last-entry-per-GUID replacement and the checkpoint frame walk.
`checkpoint_blocks.parquet` records paths available to each content block.
Join all three using replay identity and `checkpoint_index`; a checkpoint ID
can repeat, and a main-stream GUID with the same number is a different scope.

## Evidence

An independent Python oracle rebuilt the final checkpoint GUID cache from the
raw entries of 714 replays (13.01--13.05; 58,509,199 entries, 17,251,536
blocks) and matched every file, and rejected each of eight negative fixtures
(wrong base, cross-checkpoint leak, a reference appended to the table, and
so on).
An out-of-range index fails checkpoint parsing instead of falling back to
decimal text. If a checkpoint frame adds or exports GUIDs, its final cache
also depends on those frame operations; the manifest counters make that
condition visible.

### Cross-check against the main stream

The oracle above applies the same rule to the raw entries, so it confirms that
the reader follows the rule; it cannot show that the rule is right. The main
replay stream is an independent source. It declares the same server NetGUIDs
through a different reader (frame-level ExportData and export-GUID bunches,
`read_export_guids`) into the replay-wide cache that `net_guids.parquet`
records when the export ends. Each checkpoint is read into a fresh cache, so no
main-stream path passes through the literal-table rule.

`checkpoint_guid_crosscheck` in
[`tools/check_export_baseline.py`](../tools/check_export_baseline.py) compares
the two sources for one export:

1. Order `checkpoint_guid_entries.parquet` by `(checkpoint_index, ordinal)`.
   Every checkpoint must hold ordinals `0..n-1`, and each row's path columns
   must agree with its `path_is_string`; otherwise nothing is compared and the
   check fails.
2. Rebuild each indexed path by the rule above, grouping by `checkpoint_index`.
3. Join every entry, literal or indexed, to `net_guids.parquet` by `net_guid`.
   A repeated main-stream `net_guid` fails the check. An entry whose GUID the
   main stream never declared is counted as unjoined and not compared.
4. Compare the path, and the outer GUID under an explicit rule: a checkpoint
   outer of 0 must meet a null main outer, and any other value must meet the
   same main value. A null is never read as 0, so a main table that started
   writing 0 would fail.

The check fails on any path or outer difference, on an index past the
preceding literals, and when no indexed entry joined at all. It prints every
count, zeros included. `check_export_baseline.py --checkpoints` runs it after
the export and exits non-zero on failure; `verify_build_corpus.py` runs it on
every export and reports the counts per build as `guid_crosscheck_*`.

On all 1,018 exports of the common audit (every unique replay of the 24
builds, `--checkpoints`), the check and a separately written rebuild agreed on
every export, and every build agreed with the main stream:

| Exports | Indexed joined | Indexed path equal | Literal joined | Literal path equal | Literal unjoined |
|---:|---:|---:|---:|---:|---:|
| 1,018 | 19,993,994 | 19,993,994 | 61,035,283 | 61,035,283 | 91,298 |

No indexed entry was unjoined, unresolved or different, and no outer differed
in either kind. Every joined indexed entry has an outer on both sides; the
"no outer" branch of the outer rule was exercised by 26,831,216 literal
entries. No export repeats a main-stream `net_guid`. Of the joined indexed
entries, 16,549,610 are even-numbered (dynamic) GUIDs, and those agree too, so
the agreement is not confined to static objects.

The comparison can fail. Under the same join, rules other than the reader's
disagree with the main stream on the same entries:

| Rule applied to the raw indices | Path equal | Path differs |
|---|---:|---:|
| Zero-based among preceding literals, reset per checkpoint (the reader's rule) | 19,993,994 | 0 |
| One-based position | 0 | 19,993,994 |
| References appended to the table | 1,511,097 | 18,482,897 |
| One table shared by all checkpoints of a replay | 4,422,823 | 15,571,171 |
| Decimal text (`LegacyDecimal`) | 0 | 19,993,994 |

The check's tests encode the three wrong index rules as serializer output, and
edit one main-stream path, and the check rejects each; see
[`tools/tests/test_check_export_baseline.py`](../tools/tests/test_check_export_baseline.py).

This agreement is evidence for the path-index rule, not for actor identity.
The join is by GUID number: it shows that each checkpoint entry and the main
stream associate that number with the same path and outer, not that a
checkpoint GUID denotes a particular actor at a particular time, so the
caution about joining the streams by number still applies. The check also
assumes that a GUID's path does not change within a replay:
`net_guids.parquet` is the main stream's final registry, while each checkpoint
is a snapshot. That held for every compared entry. A future difference
therefore means either a different index rule or a GUID whose path changed
during the replay, and needs investigating before the rule is trusted for that
build.
