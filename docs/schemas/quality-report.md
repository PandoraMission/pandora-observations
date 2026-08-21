# Quality report schema

Version: 1.

This defines the format for the observation quality reports, produced by to to-be-built analysis tool (most likely the reporting side of `pandora-fits` run as a CRON job on the science server) and delivered roughly daily or weekly. Ingest stores each report under `data/reports/` and merges its results into the matched observations' `executed` and `quality` blocks.

The design goal: adding a new metric must never require a schema change here or a release of either package.

## Example

```json
{
  "schema_version": 1,
  "report_id": "quality-20260824-20260831-20260907T1200",
  "generated_utc": "2026-09-07T12:00:00Z",
  "producer": {"name": "pandora-fits", "version": "0.6.0"},
  "coverage": {"start_utc": "2026-08-24T00:00:00Z", "stop_utc": "2026-08-31T00:00:00Z"},
  "complete": false,

  "observations": [
    {
      "target": "G4476152832143994112",
      "start_utc": "2026-08-24T00:20:22Z",
      "stop_utc":  "2026-08-24T00:30:00Z",
      "obs_id": null,
      "data_products": ["pan_vda_20260824T002022.fits"],
      "engineering_data": null,

      "metrics": {
        "pointing_rms_arcsec":           {"value": 0.42,  "status": "pass"},
        "guide_star_lock_frac":          {"value": 0.998, "status": "pass"},
        "data_completeness_frac":        {"value": 0.94,  "status": "pass"},
        "vda_photometric_precision_ppm": {"value": 210.0, "status": "warn", "threshold": 200.0},
        "nirda_spectral_snr":            {"value": 44.1,  "status": "pass"}
      },

      "overall_score": 0.91,
      "verdict": "success",
      "notes": "Slight jitter during first 90 s."
    }
  ],
  "unanalyzed": [
    {"target": "GJ_876", "start_utc": "2026-08-25T04:00:00Z", "reason": "no data downlinked"}
  ]
}
```

## Field rules

| Field | Required | Notes |
|---|---|---|
| `schema_version` | yes | 1 |
| `report_id` | yes | unique per delivery |
| `generated_utc` | yes | later reports for the same coverage supersede earlier ones by this timestamp |
| `producer` | yes | `{name, version}` |
| `coverage` | yes | the window this report attempted to analyze |
| `complete` | yes | true when every observation in coverage was analyzed |
| `observations[].target` | yes | |
| `observations[].start_utc` / `stop_utc` | yes | the **actual executed** window, not the requested one |
| `observations[].obs_id` | no | a hint only; matching is always target plus greatest time overlap |
| `observations[].data_products` | no | filenames on the science server; how the team finds the data to download |
| `observations[].engineering_data` | no | reserved for the per-observation engineering data file (EDF): a path beside the report or an inline payload. EDF format is TBD (likely JSON, or Parquet if large); this field absorbs either. |
| `observations[].metrics` | yes | open dictionary, envelope below |
| `observations[].overall_score` | no | computed locally from `success_metrics.json` if absent |
| `observations[].verdict` | no | `success`, `partial`, or `failed`; computed locally if absent |
| `observations[].notes` | no | free text |
| `unanalyzed` | no | observations in coverage that were deliberately not analyzed, with a reason. Explicit is better than missing. |

## Metric envelope

Each entry in `metrics` is:

```json
{"value": 210.0, "status": "warn", "threshold": 200.0}
```

- `value` is required and numeric.
- `status` (`pass`/`warn`/`fail`) and `threshold` are optional. The quality analyzer can supply them, but this package re-evaluates every metric against its own `success_metrics.json` (see [success-metrics.md](success-metrics.md)) and stores both opinions when they disagree.
- Metric names are keys in `success_metrics.json`. Unknown metrics are stored verbatim and simply do not contribute to the verdict until the metrics file learns about them.
- The metrics shown above are placeholders; real thresholds are still being defined by the DPC and SOC.

## Ingest behavior

- Report entries that do not match known observation are stored in an `unmatched` bucket in the report record, never silently dropped.
- Partial reports are normal (`complete: false`); re-ingesting a fuller report for the same coverage supersedes the earlier one by `generated_utc`.
- Locally computed verdicts are stamped with the `metrics_version` that produced them. Existing verdicts are not recomputed when the metrics file changes. However, a cache rebuild recomputes all verdicts with the currently loaded metrics file.
