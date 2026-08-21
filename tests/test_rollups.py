# Standard library
import json
from datetime import timedelta

# Third-party
import pandas as pd
import pytest

# First-party/Local
from pandoraobservations import config
from pandoraobservations.database import init_data_dir
from pandoraobservations.rollups import target_summary, transit_counts, transit_windows_utc
from pandoraobservations.targets import TargetIndex


def make_observations(rows):
    frame = pd.DataFrame(rows)
    for column in ("start_utc", "stop_utc", "executed_start_utc", "executed_stop_utc"):
        frame[column] = pd.to_datetime(frame[column])
    return frame


def obs_row(target, verdict, start, hours, completeness=1.0, executed=True, **overrides):
    start = pd.Timestamp(start)
    stop = start + timedelta(hours=hours)
    row = {
        "obs_id": f"{target}-{start.isoformat()}",
        "target": target,
        "target_key": target.lower(),
        "priority": 1,
        "status": "SUCCESS" if verdict == "success" else (verdict or "EXECUTED").upper(),
        "superseded": False,
        "start_utc": start,
        "stop_utc": stop,
        "duration_s": hours * 3600.0,
        "executed_start_utc": start if executed else pd.NaT,
        "executed_stop_utc": stop if executed else pd.NaT,
        "data_completeness_frac": completeness,
        "verdict": verdict,
        "overall_score": None,
    }
    row.update(overrides)
    return row


def test_success_counters():
    observations = make_observations(
        [
            obs_row("TARG_A", "success", "2026-08-24T01:00:00", 1.0),
            # Partial with half the data usable: half an observation of credit.
            obs_row("TARG_A", "partial", "2026-08-24T05:00:00", 1.0, completeness=0.5),
            obs_row("TARG_A", "failed", "2026-08-25T01:00:00", 1.0),
            # Judged on a second UTC date, big enough to count as a success day.
            obs_row("TARG_A", "success", "2026-08-26T01:00:00", 2.0),
            obs_row("TARG_B", None, "2026-08-24T09:00:00", 1.0, executed=False),
        ]
    )
    summary = target_summary(observations)

    a = summary.loc["targ_a"]
    assert a["success_observations"] == pytest.approx(2.5)
    assert a["success_hours"] == pytest.approx(1.0 + 0.5 + 2.0)
    assert a["n_success_days"] == 2  # the failed day contributes nothing
    assert a["n_observations"] == 4

    b = summary.loc["targ_b"]
    assert b["success_observations"] == 0.0
    assert b["n_success_days"] == 0


def test_day_threshold():
    # Nine minutes of success in a day stays under the 600 s default threshold... just barely not.
    observations = make_observations([obs_row("TARG_A", "success", "2026-08-24T01:00:00", 599 / 3600)])
    assert target_summary(observations).loc["targ_a", "n_success_days"] == 0
    observations = make_observations([obs_row("TARG_A", "success", "2026-08-24T01:00:00", 601 / 3600)])
    assert target_summary(observations).loc["targ_a", "n_success_days"] == 1


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch):
    monkeypatch.setitem(config["SETTINGS"], "data_dir", "")
    monkeypatch.setitem(config["SETTINGS"], "target_list_dir", "")


@pytest.fixture
def transit_setup(tmp_path):
    # A miniature PandoraTargetList with one exoplanet: period 12 h, 1.2 h transits.
    tree = tmp_path / "PandoraTargetList" / "target_definition_files" / "primary-exoplanet"
    tree.mkdir(parents=True)
    planet = {
        "Star Name": "TESTPLANET",
        "Planet Name": "TESTPLANETb",
        "Version": "1.0.0",
        "RA": 150.0,
        "DEC": -20.0,
        "Period (days)": 0.5,
        "Transit Epoch (BJD_TDB)": 2461276.5,  # ~2026-08-24, so events land inside the week
        "Transit Duration (hrs)": 1.2,
    }
    (tree / "TESTPLANETb_target_definition.json").write_text(json.dumps(planet), encoding="utf-8")
    data_dir = init_data_dir(tmp_path / "data")
    index = TargetIndex(data_dir, tmp_path / "PandoraTargetList")
    ephemeris = {
        "period_days": planet["Period (days)"],
        "epoch_bjd_tdb": planet["Transit Epoch (BJD_TDB)"],
        "duration_hr": planet["Transit Duration (hrs)"],
        "ra_deg": planet["RA"],
        "dec_deg": planet["DEC"],
    }
    return index, ephemeris


def test_transit_windows(transit_setup):
    _, planet = transit_setup
    windows = transit_windows_utc(planet, "2026-08-24T00:00:00", "2026-08-27T00:00:00")
    # Period 0.5 d over a 3 day span: six events, each 1.2 h long.
    assert len(windows) == 6
    for ingress, egress in windows:
        assert (egress - ingress) == timedelta(hours=1.2)
    spacing = windows[1][0] - windows[0][0]
    assert abs(spacing - timedelta(hours=12)).total_seconds() < 60


def test_transit_counts(transit_setup):
    index, planet = transit_setup
    windows = transit_windows_utc(planet, "2026-08-24T00:00:00", "2026-08-27T00:00:00")

    def covering_row(window, verdict="success", lead_hr=1.0, trail_hr=0.0):
        start = pd.Timestamp(window[0]) - timedelta(hours=lead_hr)
        hours = (window[1] - window[0]).total_seconds() / 3600 + lead_hr + trail_hr
        return obs_row("TESTPLANET", verdict, start, hours)

    observations = make_observations(
        [
            covering_row(windows[0]),  # fully covered with a 1 h leading baseline
            covering_row(windows[1], verdict="partial"),  # partial does not count by default
            # Event 2: covered but with no out-of-transit baseline on either side.
            obs_row("TESTPLANET", "success", windows[2][0], 1.2),
        ]
    )
    counts = transit_counts(observations, index)
    assert counts.loc["testplanet"] == 1.0

    # Counting partial coverage too picks up the second event.
    assert transit_counts(observations, index, include_partial=True).loc["testplanet"] == 2.0


def test_transit_counts_without_target_list(tmp_path):
    data_dir = init_data_dir(tmp_path / "data")
    (data_dir / "target_index.json").write_text(
        json.dumps({"schema_version": 1, "generated_utc": "", "targets": {}, "unresolved": []}), encoding="utf-8"
    )
    index = TargetIndex(data_dir)  # loads the empty index; no tree configured anywhere
    observations = make_observations([obs_row("TARG_A", "success", "2026-08-24T01:00:00", 1.0)])
    counts = transit_counts(observations, index)
    assert len(counts) == 0  # degrades to empty rather than raising
