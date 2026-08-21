# Sequence record schema

Version: 1.

One file per ingested **final** MOC command sequence, written to `data/sequences/S<year_num>W<week_of_year_num>.seq.json`.

MOC sequence files always follow the naming pattern `S<yy>W<ww>.seq.json`, where `<yy>` is the last two digits of the year and `<ww>` is the week of year (e.g. `examples/S26W35.seq.json`). The MOC also produces telecom sequences named `T<yy>W<ww>.seq.json` in the same command format. Telecom sequences have nothing to do with science observations, but they should correlate with the KSAT contact windows, and no telecom command may ever overlap a science sequence command in time. Supplying one is optional; when supplied it feeds the cross-checks below.

Validation and ingest are separate operations. `validate-sequence` runs the same comparison on draft sequences and emits this structure as a standalone report, but writes nothing under `data/`. Only the final sequence is ingested, which also fills the `scheduled` block and status of each matched observation in the calendar record.

## Top level

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | 1 |
| `ingested_utc` | str | |
| `source` | object | `{path, sha256}` of the `.seq.json` |
| `contacts_source` | object or null | `{path, sha256}` of the `ksat_contacts.json`, when supplied |
| `telecom_source` | object or null | `{path, sha256}` of the `T<yy>W<ww>.seq.json` telecom sequence, when supplied |
| `sequence` | object | see below |
| `observations` | array | one entry per requested observation in the compared calendar |
| `unmatched_blocks` | array | command blocks that matched no requested window |
| `telecom` | object or null | telecom cross-check results, present when a telecom sequence was supplied; see below |

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

## `telecom`

Cross-checks against the optional `T<yy>W<ww>.seq.json` telecom sequence.

| Field | Type | Notes |
|---|---|---|
| `command_count` | int | commands in the telecom sequence |
| `conflicts` | array | `{science_utc, science_command, telecom_utc, telecom_command}` for every science/telecom pair that overlaps in time. Must be empty; any entry is a top-of-report finding since science and telecom commanding must never overlap. |
| `uncorrelated_blocks` | array | telecom command windows that fall outside every KSAT contact window: `{start_utc, stop_utc}`. Telecom activity should correlate with the contacts, so these are flagged for review. Only populated when a contacts file was also supplied. |

## Matching and block boundaries

- An observation block in a sequence starts at `PANDORA GOTO_TARGET` (with
  `VEL_ABER 0, PRI_REF_DIR 8, SEC_REF_DIR 2`) and ends at the closing
  `PANDORA PAYLOAD_READ ... CCSDS_AP_ID HSDR, PL_APID 0, PATH '', PL_PATH ''`, which fires `end_buffer_s` before the true end.
- Blocks are assigned to requested windows by greatest time overlap, tie broken on target name.

## Validation report

Both `validate-sequence` and `ingest-sequence` emit a human-readable summary in two forms: printed to the console, and written to `<seq_name>_validation_report.txt` beside the sequence file (e.g., `S26W35_validation_report.txt`). The machine-readable form is exactly the `observations` array above.

The text report follows the layout of the MOCSeqGen `comparison_report.txt` it replaces:

1. **Header**: requested observation counts (all priorities, and priority >= 1), generated block count, matched block count, and the comparison configuration (`end_buffer_s`, start tolerance, enabled finding types).
2. **Truncation summary**: dropped observations (priority >= 1) with total minutes, split observations (more than one generated block), truncated count out of total, total minutes truncated, and the single worst truncation with its visit/observation/target/priority.
3. **Per-observation truncation list**: one line each with the start/gap/end minute split.
4. **Findings, chronological**: one line per finding with the requested and generated windows and the truncation breakdown.
5. **Telecom and contact findings** when those inputs were supplied: any science/telecom overlap (always a defect) first, then telecom windows uncorrelated with contacts.

Ordering within the summary sections is by priority **descending** (priority 2 is the most important, 0 the least), so a truncation on a priority 2 transit is the first thing seen and a priority 0 filler is a footnote.
