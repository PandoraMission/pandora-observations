# Changelog

## v0.0.1 (Unreleased)

- Added `docs/schemas/` with v1 draft schema definitions for the calendar record, sequence record, quality report, `success_metrics.json`, and `targets.json`. Rewrote `README.md` for `pandora-observations`. Gitignored `data/` (observation records live in-repo but uncommitted for now).
- Added `schema.py` (calendar record dataclasses, observation status) and `database.py` (data directory init/discovery via the `pandora_obs_data.json` marker, record writer), with tests.
- Added `calendars.py`: calendar ingest built on `shortschedule` plus a raw-XML pass for additional parameters that are dropped by the short-term scheduler parser.
- Added `cache.py` and a minimal `rollups.py`
- Cache format is now versioned: `cache_version` lives in `data/cache/index.json`, and a cache built under a different version is stale regardless of record hashes.