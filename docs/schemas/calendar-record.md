# Calendar record schema

Version: 1.

One file per ingested science calendar delivery, written to `data/calendars/<calendar_id>-R<revision>.json`. This builds out the observation database: every observation the mission has ever requested exists as an entry in exactly one of these files.

## Top level

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | 1 |
| `ingested_utc` | str | when this record was written |
| `source` | object | `{path, sha256}` of the delivered XML |
| `calendar` | object | see below |
| `observations` | array | one entry per `Observation_Sequence`, see below |

## `calendar`

Parsed convenience fields plus the raw `<Meta>` elements from the calendar.

| Field | Type | Notes |
|---|---|---|
| `calendar_id` | str | delivery name without revision, e.g. `PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831` |
| `revision` | int | from the `R###` suffix of the delivered filename |
| `valid_from`, `expires`, `created` | str | UTC |
| `delivery_id` | str | scheduler UUID |
| `calendar_status` | str | e.g. `VALID`, `INVALID`. Recorded verbatim and **ignored by ingest**: it never gates or warns. |
| `scheduler_version` | str | `Short_Term_Scheduler_Version` |
| `tle_line1`, `tle_line2` | str | orbit prediction the week was planned against |
| `claimed_visits`, `claimed_sequences` | int | the scheduler's `Total_Visits` / `Total_Sequences` claim |
| `parsed_visits`, `parsed_sequences` | int | what we actually found in the file; may disagree with the claim |
| `superseded` | bool | true once a higher revision for the same `calendar_id` has been ingested |
| `meta_raw` | object | **every** attribute of the `<Meta>` element, verbatim, as strings. Scheduler 1.3.0 adds the full keepout configuration (`Sun_Min_Deg`, `Moon_Min_Deg`, earthlimb limits, gap tolerances, `Min_Power_Frac`, ...); new attributes in future scheduler versions land here without a schema change. |

## `observations[]`

| Field | Type | Notes |
|---|---|---|
| `obs_id` | str | `<calendar_id>:R<revision>:V<visit_id>:S<sequence_id>`. Full provenance chain; unique forever. |
| `calendar_id`, `revision`, `visit_id`, `sequence_id` | str/int | the same, split out |
| `target` | str | verbatim from the calendar, e.g. `G4476152832143994112` |
| `target_key` | str | normalized name, joins to `target_index.json` |
| `priority` | int | 0 is lowest |
| `requested` | object | `start_utc`, `stop_utc`, `duration_s`, `ra_deg`, `dec_deg`, `roll_deg`, `pri_cmd_dir` |
| `payload` | object | all payload parameters flattened to dot notation (`AcquireInfCamImages.SC_Integrations`) |
| `status` | str | lifecycle state, see below |
| `superseded` | bool | mirrors the calendar-level flag |
| `scheduled` | object or null | written by sequence ingest; shape defined in [sequence-record.md](sequence-record.md) |
| `executed` | object or null | written by report ingest: actual `start_utc`, `stop_utc`, `data_products` |
| `quality` | object or null | written by report ingest: `metrics`, `overall_score`, `verdict`, `verdict_source` (`producer` or `local`), `metrics_version` |

## Status lifecycle

```
REQUESTED --> SCHEDULED --> EXECUTED --> SUCCESS
    |             |                      PARTIAL
    |             +-> TRUNCATED          FAILED
    |             +-> DROPPED
    +-> SUPERSEDED
```

Every downstream stage is optional and can arrive late. A half-analyzed week is fine; observations with no report stay `EXECUTED`.

## Identity rules

- `obs_id` is not a matching key. Calendar revisions reschedule observations, so start times and ids are not stable across revisions.
- Sequences and reports match observations on **target plus greatest time overlap** (tie break on target name), never on `obs_id` alone.
- Supersession is calendar-level: ingesting R002 marks all of R001's observations `superseded`. Only the highest revision per `calendar_id` is active; all revisions stay on disk (but currently are not git commited).
