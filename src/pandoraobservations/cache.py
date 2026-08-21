"""The derived Parquet cache: one flat row per observation, rebuilt from the JSON records.

The JSON record layer is authoritative; everything under ``data/cache/`` is disposable and
reconstructible. ``index.json`` stores the content hash of every record file that went into
the last build, so an unchanged database makes ``rebuild_cache`` a no-op. Any record change
triggers a full rebuild, which is seconds even for a multi-year record set. Unlike the raw
records, the cache is committed to git so a fresh clone is queryable immediately.
"""

# Standard library
import json
from datetime import datetime, timezone
from pathlib import Path

# Third-party
import pandas as pd

# First-party/Local
from pandoraobservations import logger
from pandoraobservations.database import ObservationDatabase, sha256_of_file
from pandoraobservations.reports import compute_verdict, evaluate_metric, load_success_metrics
from pandoraobservations.rollups import target_summary, transit_counts
from pandoraobservations.targets import TargetIndex

# Bump when the flat table's columns or their meanings change. A cache built under a
# different version is stale regardless of record hashes and is upgraded by rebuilding
# from the records, if present.
# v2: added executed_start_utc / executed_stop_utc / data_completeness_frac columns.
CACHE_VERSION = 2

# Flat observation columns, in display order. Payload columns follow, prefixed "payload.".
BASE_COLUMNS = [
    "obs_id", "calendar_id", "revision", "visit_id", "sequence_id",
    "target", "target_key", "priority", "status", "superseded",
    "start_utc", "stop_utc", "duration_s", "ra_deg", "dec_deg", "roll_deg", "pri_cmd_dir",
    "calendar_status", "executed_start_utc", "executed_stop_utc", "data_completeness_frac",
    "verdict", "overall_score",
]


def _record_hashes(db: ObservationDatabase) -> dict:
    """Content hash per calendar record file. Hashes the record files themselves (not their
    sources), so supersession flags and future scheduled/quality merges register as changes."""
    return {path.name: sha256_of_file(path) for path in sorted((db.root / "calendars").glob("*.json"))}


def rebuild_cache(data_dir=None, force=False) -> Path:
    """Rebuild ``data/cache/observations.parquet`` and ``targets.parquet`` if stale.

    Parameters
    ----------
    data_dir : str or Path, optional
        Explicit data directory; discovered when omitted.
    force : bool
        Rebuild even when no record file changed.

    Returns
    -------
    Path
        The observations parquet file.
    """
    db = ObservationDatabase(data_dir)
    cache_dir = db.root / "cache"
    observations_path = cache_dir / "observations.parquet"
    index_path = cache_dir / "index.json"

    metrics = load_success_metrics()
    hashes = _record_hashes(db)
    if not force and observations_path.exists() and index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        stored_version = index.get("cache_version")
        if not hashes and index.get("calendar_record_hashes"):
            # Fresh clone: the committed cache is present but the gitignored record files are
            # not. There is nothing to rebuild from, so the committed cache stands.
            if stored_version != CACHE_VERSION:
                logger.warning(
                    f"Committed cache has format v{stored_version} but this package expects v{CACHE_VERSION}; "
                    "it cannot be upgraded without the record files."
                )
            return observations_path
        if (
            stored_version == CACHE_VERSION
            and index.get("calendar_record_hashes") == hashes
            and index.get("metrics_version") == metrics["metrics_version"]
        ):
            logger.info("Cache is up to date; nothing to rebuild.")
            return observations_path
        if stored_version != CACHE_VERSION:
            logger.info(f"Cache format is v{stored_version}, expected v{CACHE_VERSION}; rebuilding from records.")
        elif index.get("metrics_version") not in (None, metrics["metrics_version"]):
            logger.info(
                f"Success metrics changed ({index.get('metrics_version')} -> {metrics['metrics_version']}); "
                "recomputing all verdicts."
            )

    rows = _flatten_records(db, metrics)
    observations = pd.DataFrame(rows, columns=None if rows else BASE_COLUMNS)
    if len(observations):
        for column in ("start_utc", "stop_utc", "executed_start_utc", "executed_stop_utc"):
            observations[column] = pd.to_datetime(observations[column], format="ISO8601", utc=True).dt.tz_localize(None)
        observations = observations.sort_values(["start_utc", "obs_id"]).reset_index(drop=True)

    observations.to_parquet(observations_path, index=False)

    summary = target_summary(observations)
    if len(observations) and observations["verdict"].notna().any():
        # Transit counting needs the PandoraTargetList ephemerides; skip quietly on machines
        # without a checkout (e.g. CI) rather than failing the whole build.
        try:
            transits = transit_counts(observations, TargetIndex(db.root))
            if len(transits):
                summary = summary.join(transits)
        except FileNotFoundError:
            logger.info("PandoraTargetList unavailable; transit counts skipped.")
    summary.to_parquet(cache_dir / "targets.parquet")

    index_path.write_text(
        json.dumps(
            {
                "cache_version": CACHE_VERSION,
                "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "n_observations": len(observations),
                "metrics_version": metrics["metrics_version"],
                "calendar_record_hashes": hashes,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info(f"Cache rebuilt: {len(observations)} observations from {len(hashes)} calendar records.")
    return observations_path


def _flatten_records(db: ObservationDatabase, metrics: dict) -> list[dict]:
    """One flat row per observation across every calendar record, verdicts re-judged."""
    rows = []
    for _, record in db.iter_records("calendars"):
        calendar_status = record["calendar"]["calendar_status"]
        for obs in record["observations"]:
            row = {
                "obs_id": obs["obs_id"],
                "calendar_id": obs["calendar_id"],
                "revision": obs["revision"],
                "visit_id": obs["visit_id"],
                "sequence_id": obs["sequence_id"],
                "target": obs["target"],
                "target_key": obs["target_key"],
                "priority": obs["priority"],
                "status": obs["status"],
                "superseded": obs["superseded"],
                "start_utc": obs["requested"]["start_utc"],
                "stop_utc": obs["requested"]["stop_utc"],
                "duration_s": obs["requested"]["duration_s"],
                "ra_deg": obs["requested"]["ra_deg"],
                "dec_deg": obs["requested"]["dec_deg"],
                "roll_deg": obs["requested"]["roll_deg"],
                "pri_cmd_dir": obs["requested"]["pri_cmd_dir"],
                "calendar_status": calendar_status,
                "executed_start_utc": (obs.get("executed") or {}).get("start_utc"),
                "executed_stop_utc": (obs.get("executed") or {}).get("stop_utc"),
                "data_completeness_frac": None,
                "verdict": None,
                "overall_score": None,
            }
            quality = obs.get("quality")
            if quality:
                row["data_completeness_frac"] = quality.get("metrics", {}).get("data_completeness_frac", {}).get("value")
                # Re-judge with the currently loaded metrics file; the record keeps the
                # verdict computed at ingest time, the cache always reflects current criteria.
                statuses = {
                    name: evaluate_metric(name, envelope.get("value"), metrics)
                    for name, envelope in quality.get("metrics", {}).items()
                }
                verdict, score = compute_verdict(statuses, metrics)
                row["verdict"] = verdict if verdict is not None else quality.get("verdict")
                row["overall_score"] = score if verdict is not None else quality.get("overall_score")
            row.update({f"payload.{key}": value for key, value in obs["payload"].items()})
            rows.append(row)
    return rows


def load_observations(data_dir=None) -> pd.DataFrame:
    """Load the flat observation table, rebuilding the cache first if it is stale."""
    return pd.read_parquet(rebuild_cache(data_dir))


def load_target_summary(data_dir=None) -> pd.DataFrame:
    """Load the per-target summary table, rebuilding the cache first if it is stale."""
    return pd.read_parquet(Path(rebuild_cache(data_dir)).with_name("targets.parquet"))
