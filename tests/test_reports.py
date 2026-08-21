# Standard library
import json
from pathlib import Path

# Third-party
import pytest

# First-party/Local
from pandoraobservations import config
from pandoraobservations.cache import load_observations
from pandoraobservations.calendars import ingest_calendar
from pandoraobservations.database import ObservationDatabase, init_data_dir
from pandoraobservations.reports import compute_verdict, evaluate_metric, ingest_report, load_success_metrics

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CALENDAR_XML = EXAMPLES / "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831-R002.xml"
CALENDAR_ID = "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831"


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch):
    monkeypatch.setitem(config["SETTINGS"], "success_metrics_path", "")


def test_packaged_metrics_load():
    metrics = load_success_metrics()
    assert metrics["metrics_version"] == "1.0.0"
    assert set(metrics["metrics"]) == {
        "pointing_rms_arcsec", "data_completeness_frac", "vda_photometric_precision_ppm", "nirda_spectral_snr",
    }


def test_evaluate_metric_directions():
    metrics = load_success_metrics()
    # lower_is_better: pointing pass <= 0.9, warn <= 1.25
    assert evaluate_metric("pointing_rms_arcsec", 0.5, metrics) == "pass"
    assert evaluate_metric("pointing_rms_arcsec", 1.0, metrics) == "warn"
    assert evaluate_metric("pointing_rms_arcsec", 2.0, metrics) == "fail"
    # higher_is_better: completeness pass >= 0.99, warn >= 0.95
    assert evaluate_metric("data_completeness_frac", 0.995, metrics) == "pass"
    assert evaluate_metric("data_completeness_frac", 0.96, metrics) == "warn"
    assert evaluate_metric("data_completeness_frac", 0.5, metrics) == "fail"
    assert evaluate_metric("some_future_metric", 1.0, metrics) is None


def test_compute_verdict_rules():
    metrics = load_success_metrics()
    assert compute_verdict({"pointing_rms_arcsec": "pass", "data_completeness_frac": "pass"}, metrics)[0] == "success"
    assert compute_verdict({"pointing_rms_arcsec": "warn"}, metrics)[0] == "partial"
    # A required metric failing is fatal; a non-required one failing is only partial.
    assert compute_verdict({"pointing_rms_arcsec": "fail"}, metrics)[0] == "failed"
    assert compute_verdict({"nirda_spectral_snr": "fail", "pointing_rms_arcsec": "pass"}, metrics)[0] == "partial"
    assert compute_verdict({"some_future_metric": None}, metrics) == (None, None)
    verdict, score = compute_verdict({"pointing_rms_arcsec": "pass", "nirda_spectral_snr": "warn"}, metrics)
    assert (verdict, score) == ("partial", 0.75)


def report_entry(target, start_utc, stop_utc, metrics_values, **extra):
    entry = {
        "target": target,
        "start_utc": start_utc,
        "stop_utc": stop_utc,
        "metrics": {name: {"value": value} for name, value in metrics_values.items()},
    }
    entry.update(extra)
    return entry


def write_report(path, entries, report_id="quality-test-001", generated_utc="2026-09-01T12:00:00Z", **extra):
    report = {
        "schema_version": 1,
        "report_id": report_id,
        "generated_utc": generated_utc,
        "producer": {"name": "pandora-fits", "version": "0.6.0"},
        "coverage": {"start_utc": "2026-08-24T00:00:00Z", "stop_utc": "2026-08-31T00:00:00Z"},
        "complete": False,
        "observations": entries,
    }
    report.update(extra)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path):
    root = init_data_dir(tmp_path / "data")
    ingest_calendar(CALENDAR_XML, data_dir=root)
    return root


def test_ingest_report_end_to_end(data_dir, tmp_path):
    # Windows below are the requested windows of real observations in the example calendar.
    entries = [
        # V0002:S001 G4476152832143994112 00:14-00:30: everything passes -> SUCCESS.
        report_entry(
            "G4476152832143994112", "2026-08-24T00:16:00Z", "2026-08-24T00:30:00Z",
            {"pointing_rms_arcsec": 0.4, "data_completeness_frac": 0.999},
            data_products=["pan_vda_x.fits"],
        ),
        # V0002:S022 TOI-181b 17:07-18:00: completeness warns; the producer disagrees.
        report_entry(
            "TOI-181b", "2026-08-24T17:07:00Z", "2026-08-24T17:43:00Z",
            {"data_completeness_frac": 0.96},
            verdict="success",
        ),
        # A required metric fails -> FAILED, whatever else says.
        report_entry(
            "TRAPPIST-1", "2026-08-26T19:13:00Z", "2026-08-26T20:00:00Z",
            {"pointing_rms_arcsec": 3.0, "nirda_spectral_snr": 50.0},
        ),
        # A target the calendar never requested -> unmatched bucket.
        report_entry("GHOST_STAR", "2026-08-25T00:00:00Z", "2026-08-25T00:30:00Z", {}),
    ]
    report_file = write_report(
        tmp_path / "quality-test-001.json", entries,
        unanalyzed=[{"target": "GJ_876", "start_utc": "2026-08-30T02:31:00Z", "reason": "no data downlinked"}],
    )

    record_path = ingest_report(report_file, data_dir=data_dir)
    db = ObservationDatabase(data_dir)
    record = db.read_record("reports", record_path.name)
    assert len(record["matched"]) == 3
    assert [entry["target"] for entry in record["unmatched"]] == ["GHOST_STAR"]

    stored = db.read_record("calendars", f"{CALENDAR_ID}-R002.json")
    by_target = {}
    for obs in stored["observations"]:
        if obs["quality"]:
            by_target[obs["target"]] = obs

    success = by_target["G4476152832143994112"]
    assert success["status"] == "SUCCESS"
    assert success["quality"]["verdict_source"] == "local"
    assert success["quality"]["metrics_version"] == "1.0.0"
    assert success["executed"]["data_products"] == ["pan_vda_x.fits"]

    partial = by_target["TOI-181b"]
    assert partial["status"] == "PARTIAL"
    assert partial["quality"]["producer_verdict"] == "success"  # disagreement kept visible

    assert by_target["TRAPPIST-1"]["status"] == "FAILED"

    # Unchanged re-ingest is a no-op.
    assert ingest_report(report_file, data_dir=data_dir) == record_path
    assert len(list(db.iter_records("reports"))) == 1


def test_stale_report_never_downgrades(data_dir, tmp_path):
    fresh = write_report(
        tmp_path / "r2.json",
        [report_entry("G4476152832143994112", "2026-08-24T00:14:00Z", "2026-08-24T00:30:00Z",
                      {"pointing_rms_arcsec": 0.4, "data_completeness_frac": 0.999})],
        report_id="quality-test-002", generated_utc="2026-09-02T00:00:00Z",
    )
    stale = write_report(
        tmp_path / "r1.json",
        [report_entry("G4476152832143994112", "2026-08-24T00:14:00Z", "2026-08-24T00:30:00Z",
                      {"pointing_rms_arcsec": 3.0, "data_completeness_frac": 0.2})],
        report_id="quality-test-001", generated_utc="2026-09-01T00:00:00Z",
    )
    ingest_report(fresh, data_dir=data_dir)
    ingest_report(stale, data_dir=data_dir)  # arrives late, generated earlier: must not apply

    db = ObservationDatabase(data_dir)
    stored = db.read_record("calendars", f"{CALENDAR_ID}-R002.json")
    obs = next(o for o in stored["observations"] if o["quality"])
    assert obs["status"] == "SUCCESS"
    assert obs["quality"]["report_generated_utc"] == "2026-09-02T00:00:00Z"


def test_cache_recomputes_verdicts_under_new_metrics(data_dir, tmp_path, monkeypatch):
    report_file = write_report(
        tmp_path / "r.json",
        [report_entry("G4476152832143994112", "2026-08-24T00:14:00Z", "2026-08-24T00:30:00Z",
                      {"pointing_rms_arcsec": 1.0, "data_completeness_frac": 0.999})],
    )
    ingest_report(report_file, data_dir=data_dir)

    observations = load_observations(data_dir)
    judged = observations[observations["verdict"].notna()]
    assert len(judged) == 1
    assert judged["verdict"].iloc[0] == "partial"  # pointing 1.0 warns under v1.0.0
    index = json.loads((data_dir / "cache" / "index.json").read_text(encoding="utf-8"))
    assert index["metrics_version"] == "1.0.0"

    # Loosen the pointing threshold in a v2 metrics file: the cache re-judges history, the
    # record file keeps the verdict computed at ingest time.
    relaxed = json.loads((Path(__file__).resolve().parents[1] / "src" / "pandoraobservations" / "data"
                          / "success_metrics.json").read_text(encoding="utf-8"))
    relaxed["metrics_version"] = "2.0.0"
    relaxed["metrics"]["pointing_rms_arcsec"]["pass"] = 1.5
    metrics_file = tmp_path / "metrics_v2.json"
    metrics_file.write_text(json.dumps(relaxed), encoding="utf-8")
    monkeypatch.setitem(config["SETTINGS"], "success_metrics_path", str(metrics_file))

    observations = load_observations(data_dir)  # stale via metrics_version, rebuilds
    assert observations[observations["verdict"].notna()]["verdict"].iloc[0] == "success"
    index = json.loads((data_dir / "cache" / "index.json").read_text(encoding="utf-8"))
    assert index["metrics_version"] == "2.0.0"

    db = ObservationDatabase(data_dir)
    stored = db.read_record("calendars", f"{CALENDAR_ID}-R002.json")
    obs = next(o for o in stored["observations"] if o["quality"])
    assert obs["quality"]["verdict"] == "partial"  # ingest-time verdict untouched
    assert obs["quality"]["metrics_version"] == "1.0.0"
