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
from pandoraobservations.rollups import target_summary

# Bump when the flat table's columns or their meanings change. A cache built under a
# different version is stale regardless of record hashes and is upgraded by rebuilding
# from the records, if present.
CACHE_VERSION = 1

# Flat observation columns, in display order. Payload columns follow, prefixed "payload.".
BASE_COLUMNS = [
    "obs_id", "calendar_id", "revision", "visit_id", "sequence_id",
    "target", "target_key", "priority", "status", "superseded",
    "start_utc", "stop_utc", "duration_s", "ra_deg", "dec_deg", "roll_deg", "pri_cmd_dir",
    "calendar_status",
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
        if stored_version == CACHE_VERSION and index.get("calendar_record_hashes") == hashes:
            logger.info("Cache is up to date; nothing to rebuild.")
            return observations_path
        if stored_version != CACHE_VERSION:
            logger.info(f"Cache format is v{stored_version}, expected v{CACHE_VERSION}; rebuilding from records.")

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
            }
            row.update({f"payload.{key}": value for key, value in obs["payload"].items()})
            rows.append(row)

    observations = pd.DataFrame(rows, columns=None if rows else BASE_COLUMNS)
    if len(observations):
        observations["start_utc"] = pd.to_datetime(observations["start_utc"])
        observations["stop_utc"] = pd.to_datetime(observations["stop_utc"])
        observations = observations.sort_values(["start_utc", "obs_id"]).reset_index(drop=True)

    observations.to_parquet(observations_path, index=False)
    target_summary(observations).to_parquet(cache_dir / "targets.parquet")

    index_path.write_text(
        json.dumps(
            {
                "cache_version": CACHE_VERSION,
                "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "n_observations": len(observations),
                "metrics_version": None,  # populated once report ingest lands
                "calendar_record_hashes": hashes,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info(f"Cache rebuilt: {len(observations)} observations from {len(hashes)} calendar records.")
    return observations_path


def load_observations(data_dir=None) -> pd.DataFrame:
    """Load the flat observation table, rebuilding the cache first if it is stale."""
    return pd.read_parquet(rebuild_cache(data_dir))


def load_target_summary(data_dir=None) -> pd.DataFrame:
    """Load the per-target summary table, rebuilding the cache first if it is stale."""
    return pd.read_parquet(Path(rebuild_cache(data_dir)).with_name("targets.parquet"))
