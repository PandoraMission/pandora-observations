<a href="https://github.com/PandoraMission/pandora-observations/actions/workflows/tests.yml"><img src="https://github.com/PandoraMission/pandora-observations/workflows/tests/badge.svg" alt="Test status"/></a> <a href="https://github.com/PandoraMission/pandora-observations/actions/workflows/black.yml"><img src="https://github.com/PandoraMission/pandora-observations/workflows/black/badge.svg" alt="black status"/></a> <a href="https://github.com/PandoraMission/pandora-observations/actions/workflows/flake8.yml"><img src="https://github.com/PandoraMission/pandora-observations/workflows/flake8/badge.svg" alt="flake8 status"/></a>

# `pandora-observations`

Software and database to determine what the Pandora SmallSat spacecraft was asked to observe, what it actually observed, and whether those observations were _scientifically_ successful.

## What it does

The short-term scheduler and SOC deliver regular science calendars of requested observations. The mission operations center turns these into command sequences (trimming science around ground contacts and maintenance). These are executed and quality reports arrive from the science server. This package ingests each of those as they arrive and stores and/or calculates the answer to: which targets have how many good observations, days, and (for exoplanets) transits, so the science team knows what data is worth pulling off the science server.

Inputs:
- **science calendar (XML)**, parsed via `shortschedule`: what was requested.
- **MOC command sequence (`.seq.json`)**: what was actually scheduled. Draft sequences can be *validated* (report of truncated or dropped high-priority observations); the final sequence is *ingested*. An optional `ksat_contacts.json` shows which ground contact caused a drop/truncation.
- **Quality reports (JSON)** with per-metric scores, judged against a versioned `success_metrics.json`, plus per-observation engineering data files containing telemetry.

Outputs: a queryable observation database (append-only JSON records with a Parquet cache), notebooks for the science team, and a feed for the Pandora observing website.

Examples of the input files are in [examples/](examples/).

## Structure

```
src/pandoraobservations/   the package: ingest, matching, verdicts, rollups, export
docs/schemas/              versioned schema definitions for every file read or written
examples/                  sample calendar, MOC sequence, and contact list
notebooks/                 ingest, validation, and target-progress workflows
data/                      observation records (gitignored) + committed Parquet cache
                           (auto-discovered, overridable with --data-dir)
```

The schema documents in [docs/schemas/](docs/schemas/) describe the current format for every record and report, including the quality report schema that the downstream analysis tool(s) write to and the `success_metrics.json` format that defines observation success.
