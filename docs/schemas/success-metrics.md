# Success metrics file schema

Version: 1.

The file `success_metrics.json` defines every quality metric this package understands and the rules that turn metric results into an observation verdict (`success`, `partial`, `failed`). It is the single place where "was this observation scientifically successful" is defined.

It is a standalone file that:

- Carries its own `metrics_version`, independent of the package version, so verdicts can be traced to the exact criteria that produced them and recomputed under new criteria.
- The team can track and trade this one file without a package release.
- The downstream analysis tool (`pandora-fits` reporting) can copy or import the same file so both ends judge with identical criteria.

The packaged copy lives at `src/pandoraobservations/data/success_metrics.json` and is loaded at import. A different file can be supplied via config or argument.

## Example

```json
{
  "metrics_version": "1.0.0",
  "updated": "2026-08-21",
  "description": "Placeholder thresholds pending DPC/SOC definitions.",

  "metrics": {
    "pointing_rms_arcsec": {
      "units": "arcsec",
      "direction": "lower_is_better",
      "pass": 0.9,
      "warn": 1.25,
      "required": true,
      "description": "RMS pointing error over the executed window."
    },
    "data_completeness_frac": {
      "units": "fraction",
      "direction": "higher_is_better",
      "pass": 0.99,
      "warn": 0.95,
      "required": true,
      "description": "Fraction of expected science frames received."
    },
    "vda_photometric_precision_ppm": {
      "units": "ppm",
      "direction": "lower_is_better",
      "pass": 200.0,
      "warn": 400.0,
      "required": false,
      "description": "VDA photometric precision."
    },
    "nirda_spectral_snr": {
      "units": "dimensionless",
      "direction": "higher_is_better",
      "pass": 30.0,
      "warn": 15.0,
      "required": false,
      "description": "NIRDA spectral signal to noise."
    }
  },

  "verdict_rules": {
    "failed_if": "any required metric is fail",
    "partial_if": "any metric is warn, or any non-required metric is fail",
    "success_if": "otherwise",
    "partial_credit": "usable duration over requested duration"
  }
}
```

## Field rules

| Field | Notes |
|---|---|
| `metrics_version` | version string, bumped on any threshold or rule change. Stamped onto every verdict this package computes. |
| `updated` | date of last edit |
| `metrics.<name>.units` | documentation only |
| `metrics.<name>.direction` | `lower_is_better` or `higher_is_better` |
| `metrics.<name>.pass` / `warn` | for `lower_is_better`: value <= pass is pass, value <= warn is warn, else fail. Mirrored for `higher_is_better`. |
| `metrics.<name>.required` | a fail on a required metric fails the observation outright |
| `verdict_rules` | version 1 hard-codes the simple rule set shown above; the strings are documentation. Richer combination logic is an anticipated v2 change once the DPC/SOC criteria land. |

## Recomputation rules

- At report ingest, missing `verdict`/`overall_score` values are computed with the currently loaded metrics file and stamped with its `metrics_version`. Record files keep that stamp forever.
- Changing the metrics file does **not** rerun previous observations. `rebuild-cache` recomputes every verdict in the Parquet cache with the currently loaded file (record files untouched).
