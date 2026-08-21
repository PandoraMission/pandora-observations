# Changelog

## v0.0.1 (Unreleased)

- Added `docs/schemas/` with v1 draft schema definitions for the calendar record, sequence record, quality report, `success_metrics.json`, and `targets.json`. Rewrote `README.md` for `pandora-observations`. Gitignored `data/` (observation records live in-repo but uncommitted for now).
- Added `schema.py` (calendar record dataclasses, observation status) and `database.py` (data directory init/discovery via the `pandora_obs_data.json` marker, record writer), with tests.
- Added `calendars.py`: calendar ingest built on `shortschedule` plus a raw-XML pass for additional parameters that are dropped by the short-term scheduler parser.
- Added `cache.py` and a minimal `rollups.py`
- Cache format is now versioned: `cache_version` lives in `data/cache/index.json`, and a cache built under a different version is stale regardless of record hashes.
- Added `sequences.py`, porting the MOCSeqGen `compare_calendar_sequence.py` with structured output: block detection, greatest-overlap matching, truncation split by cause, payload mismatch checks via the verbatim `INF`/`VIS` param maps, KSAT contact overlays, and telecom cross-checks (a conflict is a command-timestamp collision within 1 s, not a telecom command inside a science window, which is the normal contact-interrupts-science case). `validate_sequence` writes nothing under `data/`; `ingest_sequence` records the final sequence and fills each observation's `scheduled` block and status.
