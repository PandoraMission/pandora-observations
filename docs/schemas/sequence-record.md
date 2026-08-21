# Sequence record schema

Version: 1.

One file per ingested **final** MOC command sequence, written to `data/sequences/S<year_num>W<week_of_year_num>.seq.json`.

Validation and ingest are separate operations. `validate-sequence` runs the same comparison on draft sequences and emits this structure as a standalone report, but writes nothing under `data/`. Only the final sequence is ingested, which also fills the `scheduled` block and status of each matched observation in the calendar record.

## Top level

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | 1 |
| `ingested_utc` | str | |
| `source` | object | `{path, sha256}` of the `.seq.json` |
| `contacts_source` | object or null | `{path, sha256}` of the `ksat_contacts.json`, when supplied |
| `sequence` | object | see below |
| `observations` | array | one entry per requested observation in the compared calendar |
| `unmatched_blocks` | array | command blocks that matched no requested window |

## `sequence`

| Field | Type | Notes |
|---|---|---|
| `sequence_file_id` | str | e.g. `S26W35`, from the filename |
| `calendar_id`, `revision` | str, int | the calendar this sequence was compared against |
| `command_count` | int | total commands in the file |
| `use_cosmos` | bool | passed through from the file |
| `end_buffer_s` | float | the `END_BUFFER_SEC` value used in the comparison (default 45) |

## `observations[]`

One entry per requested observation, including dropped ones.

| Field | Type | Notes |
|---|---|---|
| `obs_id` | str | the matched calendar observation |
| `target` | str | |
| `scheduled_status` | str | `scheduled`, `truncated`, or `dropped` |
| `requested_s` | float | requested window duration |
| `coverage_s` | float | seconds of the requested window actually covered by commands |
| `matched_blocks` | array | `{start_utc, stop_utc, science_file}` per generated block; one requested window may be satisfied by several blocks. `science_file` is the payload path from the closing `PAYLOAD_READ`. |
| `truncation` | object | `{start_late_s, mid_gap_s, end_early_s}`; zero when fully covered |
| `payload_mismatches` | array | `{parameter, requested, scheduled}` where the sequence's `PLD_*` value disagrees with the calendar (mapped via the `INF_PARAM_MAP`/`VIS_PARAM_MAP` tables) |
| `contact_overlaps` | array | contacts overlapping this observation's requested window: `{ground_station, antenna, start_utc, stop_utc, overlap_s}`. Empty when no contacts file was supplied. Answers "was this science cut for a downlink, and which one?" |

## `unmatched_blocks[]`

| Field | Type | Notes |
|---|---|---|
| `start_utc`, `stop_utc` | str | block window found in the sequence |
| `target` | str or null | target claimed by the block's commands, if any |
| `science_file` | str or null | |

## Matching and block boundaries

- An observation block in a sequence starts at `PANDORA GOTO_TARGET` (with
  `VEL_ABER 0, PRI_REF_DIR 8, SEC_REF_DIR 2`) and ends at the closing
  `PANDORA PAYLOAD_READ ... CCSDS_AP_ID HSDR, PL_APID 0, PATH '', PL_PATH ''`, which fires `end_buffer_s` before the true end.
- Blocks are assigned to requested windows by greatest time overlap, tie broken on target name.

## Validation report

Both `validate-sequence` and `ingest-sequence` also emit a human-readable summary ordered by priority: truncations and drops on priority 2 targets first, priority-0 targets last. The machine-readable form is exactly the `observations` array above.
