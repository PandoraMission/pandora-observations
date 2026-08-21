# Standard library
import json
from pathlib import Path

# Third-party
import pandas as pd
import pytest

# First-party/Local
from pandoraobservations.cache import CACHE_VERSION, load_observations, load_target_summary, rebuild_cache
from pandoraobservations.calendars import ingest_calendar
from pandoraobservations.database import init_data_dir

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831-R002.xml"
CALENDAR_ID = "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831"


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    # One ingested example calendar shared by the tests in this module.
    root = init_data_dir(tmp_path_factory.mktemp("cache") / "data")
    ingest_calendar(EXAMPLE, data_dir=root)
    return root


def test_build_and_flat_table(data_dir):
    observations = load_observations(data_dir)
    assert len(observations) == 226
    assert observations["status"].eq("REQUESTED").all()
    assert not observations["superseded"].any()
    # Times are real datetimes, sorted, inside the calendar week.
    assert pd.api.types.is_datetime64_any_dtype(observations["start_utc"])
    assert observations["start_utc"].is_monotonic_increasing
    assert observations["start_utc"].iloc[0] == pd.Timestamp("2026-08-24T00:14:00")
    # Payload parameters are flattened alongside, with numeric types intact.
    assert observations["payload.AcquireInfCamImages.ROI_StartX"].iloc[0] == 1737


def test_noop_when_nothing_changed(data_dir):
    first = rebuild_cache(data_dir)
    mtime = first.stat().st_mtime_ns
    assert rebuild_cache(data_dir) == first
    assert first.stat().st_mtime_ns == mtime  # untouched, not rewritten
    assert rebuild_cache(data_dir, force=True).stat().st_mtime_ns > mtime


def test_rebuilds_when_records_change(data_dir, tmp_path):
    rebuild_cache(data_dir)
    # A lower revision arriving later changes the record set (it lands already superseded).
    text = EXAMPLE.read_text(encoding="utf-8").replace('Delivery_Id="bb2a', 'Delivery_Id="0000')
    copy = tmp_path / f"{CALENDAR_ID}-R001.xml"
    copy.write_text(text, encoding="utf-8")
    ingest_calendar(copy, data_dir=data_dir)

    observations = load_observations(data_dir)
    assert len(observations) == 452
    assert observations["superseded"].sum() == 226
    assert set(observations.loc[observations["superseded"], "revision"]) == {1}


def test_target_summary(data_dir):
    observations = load_observations(data_dir)
    summary = load_target_summary(data_dir)
    # Superseded R001 rows are excluded, so totals match the active calendar only.
    assert summary["n_observations"].sum() == 226
    assert summary.index.name == "target_key"
    top = summary.iloc[0]
    active = observations.loc[~observations["superseded"]]
    per_target_hours = active.groupby("target_key")["duration_s"].sum() / 3600.0
    assert top["requested_hours"] == pytest.approx(per_target_hours.max())
    assert summary["n_requested"].sum() == 226
    assert (summary["n_requested_days"] >= 1).all()


def test_version_bump_rebuilds(tmp_path):
    root = init_data_dir(tmp_path / "data")
    ingest_calendar(EXAMPLE, data_dir=root)
    parquet = rebuild_cache(root)
    mtime = parquet.stat().st_mtime_ns

    # A cache written by an older package version is stale even though no record changed.
    index_path = parquet.with_name("index.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["cache_version"] = 0
    index_path.write_text(json.dumps(index), encoding="utf-8")

    rebuild_cache(root)
    assert parquet.stat().st_mtime_ns > mtime
    assert json.loads(index_path.read_text(encoding="utf-8"))["cache_version"] == CACHE_VERSION


def test_fresh_clone_keeps_committed_cache(tmp_path):
    # A fresh clone has the committed cache but not the gitignored record files. The
    # committed cache must survive an auto-rebuild rather than being replaced with nothing.
    root = init_data_dir(tmp_path / "data")
    ingest_calendar(EXAMPLE, data_dir=root)
    parquet = rebuild_cache(root)
    mtime = parquet.stat().st_mtime_ns

    for record in (root / "calendars").glob("*.json"):
        record.unlink()

    observations = load_observations(root)
    assert len(observations) == 226
    assert parquet.stat().st_mtime_ns == mtime


def test_empty_database_builds_empty_cache(tmp_path):
    root = init_data_dir(tmp_path / "data")
    observations = load_observations(root)
    assert len(observations) == 0
    assert "obs_id" in observations.columns
    assert len(load_target_summary(root)) == 0
