# Changelog

## v0.0.1 (Unreleased)

- Added `docs/schemas/` with v1 draft schema definitions for the calendar record, sequence record, quality report, `success_metrics.json`, and `targets.json`. Rewrote `README.md` for `pandora-observations`. Gitignored `data/` (observation records live in-repo but uncommitted for now).
- Added `schema.py` (calendar record dataclasses, observation status) and `database.py` (data directory init/discovery via the `pandora_obs_data.json` marker, record writer), with tests.
- Added `calendars.py`: calendar ingest built on `shortschedule` plus a raw-XML pass for additional parameters that are dropped by the short-term scheduler parser.
