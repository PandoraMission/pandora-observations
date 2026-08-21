# Schemas

These documents are the normative definitions of every file format `pandora-observations` reads or writes.

| Schema | File | Current version |
|---|---|---|
| Calendar record (observation database) | [calendar-record.md](calendar-record.md) | 1 |
| Sequence record | [sequence-record.md](sequence-record.md) | 1 |
| Quality report (input contract) | [quality-report.md](quality-report.md) | 1 |
| Success metrics file | [success-metrics.md](success-metrics.md) | 1 |
| Target index (`target_index.json`) | [targets.md](targets.md) | 1 |

## Conventions shared by all record files

- Every record file is JSON (UTF-8) and carries a top-level integer `schema_version`. Versions bump only on breaking changes; adding optional fields is not a breaking change.
- All timestamps are UTC ISO 8601 strings. Fields are suffixed `_utc`. Transit ephemerides are the one exception: they are BJD_TDB, suffixed `_bjd_tdb`.
- Record files written by ingest also carry `ingested_utc` and a `source` object with the originating file's `path` and `sha256`. Re-ingesting a source whose hash is already on disk is a no-op.
- The record layer (`data/calendars/`, `data/sequences/`, `data/reports/`) is append-only and authoritative. The Parquet cache under `data/cache/` is derived and disposable; its columns are the flattened record fields. The cache format carries its own `cache_version` in `data/cache/index.json`: a cache built under a different version is stale regardless of record hashes and is upgraded by rebuilding from the records.
- The `data/` directory lives in the repo root. The raw record files are gitignored for now, but the Parquet cache under `data/cache/` **is** committed, so a fresh clone has a queryable database without re-ingesting anything. The root is marked by a `pandora_obs_data.json` file so the code can discover it; an explicit path always overrides discovery.

Engineering data files (EDFs) delivered with quality reports do not have a schema document yet; their format is TBD. The quality report schema reserves the `engineering_data` field for them.
