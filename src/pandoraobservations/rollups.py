"""Per-target rollups, computed during the cache build and never stored in the records."""

# Standard library
import json
from datetime import timedelta

# Third-party
import astropy.units as u
import numpy as np
import pandas as pd
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time

# First-party/Local
from pandoraobservations import logger
from pandoraobservations.targets import find_target_list_dir

SUMMARY_COLUMNS = [
    "target", "n_observations", "requested_hours", "n_requested_days",
    "max_priority", "first_start_utc", "last_stop_utc",
]

# Rollup thresholds. All placeholders pending DPC/SOC definitions, configurable per call.
MIN_DAY_SUCCESS_S = 600.0  # successful seconds a UTC date needs to count as a success day
TRANSIT_COVERAGE_MIN = 0.8  # fraction of ingress-to-egress that must be covered
BASELINE_HR = 1.0  # out-of-transit baseline block checked on each side
BASELINE_COVERAGE_MIN = 0.5  # fraction of the baseline block that must be covered


def target_summary(observations: pd.DataFrame, min_day_success_s=MIN_DAY_SUCCESS_S) -> pd.DataFrame:
    """Summarize active observations per target.

    Superseded observations are excluded: they describe requests that were replaced by a
    later calendar revision, so counting them would double-book rescheduled windows.

    Parameters
    ----------
    observations : pd.DataFrame
        The flat observation table built by `pandoraobservations.cache.rebuild_cache`.
    min_day_success_s : float
        Successful observing seconds a UTC date must accumulate to count as a success day.

    Returns
    -------
    pd.DataFrame
        One row per ``target_key``: request counts and durations, one ``n_<status>`` count
        column per status present, and (when verdicts exist) the success counters:
        ``success_observations`` (a partial counts as its usable fraction of the requested
        duration), ``success_hours``, and ``n_success_days``.
    """
    active = observations.loc[~observations["superseded"]] if len(observations) else observations
    if not len(active):
        return pd.DataFrame(columns=SUMMARY_COLUMNS).rename_axis("target_key")

    grouped = active.groupby("target_key")
    summary = pd.DataFrame(
        {
            "target": grouped["target"].first(),
            "n_observations": grouped.size(),
            "requested_hours": grouped["duration_s"].sum() / 3600.0,
            "n_requested_days": grouped["start_utc"].apply(lambda times: times.dt.date.nunique()),
            "max_priority": grouped["priority"].max(),
            "first_start_utc": grouped["start_utc"].min(),
            "last_stop_utc": grouped["stop_utc"].max(),
        }
    )

    if "verdict" in active.columns:
        if "data_completeness_frac" in active.columns:
            completeness = pd.to_numeric(active["data_completeness_frac"], errors="coerce").clip(0, 1).fillna(1.0)
        else:
            completeness = pd.Series(1.0, index=active.index)
        executed_s = (active["executed_stop_utc"] - active["executed_start_utc"]).dt.total_seconds()
        # Usable science time: the executed span degraded by data completeness. Failed and
        # unjudged observations contribute nothing.
        usable = (executed_s * completeness).where(active["verdict"].isin(("success", "partial")), 0.0).fillna(0.0)

        credit = pd.Series(0.0, index=active.index)
        credit[active["verdict"] == "success"] = 1.0
        partial = active["verdict"] == "partial"
        credit[partial] = (usable[partial] / active.loc[partial, "duration_s"]).clip(upper=1.0).fillna(0.0)

        by_target = active["target_key"]
        summary["success_observations"] = credit.groupby(by_target).sum()
        summary["success_hours"] = usable.groupby(by_target).sum() / 3600.0
        daily = usable.groupby([by_target, active["executed_start_utc"].dt.date]).sum()
        summary["n_success_days"] = daily[daily >= min_day_success_s].groupby(level=0).size()
        summary[["success_observations", "success_hours"]] = summary[
            ["success_observations", "success_hours"]
        ].fillna(0.0)
        summary["n_success_days"] = summary["n_success_days"].fillna(0).astype(int)

    # One count column per status seen, e.g. n_requested / n_scheduled / n_success.
    counts = active.pivot_table(index="target_key", columns="status", aggfunc="size", fill_value=0)
    counts.columns = [f"n_{status.lower()}" for status in counts.columns]
    return summary.join(counts).sort_values("requested_hours", ascending=False)


def transit_windows_utc(planet: dict, span_start, span_stop) -> list:
    """UTC ``(ingress, egress)`` windows of a planet's transits within a time span.

    Epochs in the PandoraTargetList definition files are BJD_TDB. The light travel time to
    the solar system barycenter is removed (when the target's coordinates are available) for
    a geocentric UTC event time; Pandora's LEO offset from the geocenter is negligible at
    these scales.

    Parameters
    ----------
    planet : dict
        ``period_days``, ``epoch_bjd_tdb``, ``duration_hr``, optionally ``ra_deg``/``dec_deg``.
    span_start, span_stop : datetime-like
        UTC span to enumerate events over.
    """
    period = float(planet["period_days"])
    epoch = Time(float(planet["epoch_bjd_tdb"]), format="jd", scale="tdb")
    start = Time(pd.Timestamp(span_start).to_pydatetime(), scale="utc")
    stop = Time(pd.Timestamp(span_stop).to_pydatetime(), scale="utc")
    first = int(np.ceil((start.tdb.jd - epoch.jd) / period))
    last = int(np.floor((stop.tdb.jd - epoch.jd) / period))
    if last < first:
        return []

    mids = epoch + np.arange(first, last + 1) * period * u.day
    if planet.get("ra_deg") is not None and planet.get("dec_deg") is not None:
        mids = Time(mids, location=EarthLocation.from_geocentric(0, 0, 0, unit="m"))
        mids = mids - mids.light_travel_time(SkyCoord(planet["ra_deg"], planet["dec_deg"], unit="deg"))
    half = timedelta(hours=float(planet["duration_hr"]) / 2.0)
    return [(mid - half, mid + half) for mid in np.atleast_1d(mids.utc.datetime)]


def _planet_ephemerides(target_index, name: str) -> list[dict]:
    """Transit ephemerides for a target, read live from its definition files."""
    planets = {}
    for path in target_index.definition_paths(name):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        keys = ("Planet Name", "Period (days)", "Transit Epoch (BJD_TDB)", "Transit Duration (hrs)")
        if all(info.get(key) is not None for key in keys):
            planets[info["Planet Name"]] = {
                "planet_name": info["Planet Name"],
                "period_days": info["Period (days)"],
                "epoch_bjd_tdb": info["Transit Epoch (BJD_TDB)"],
                "duration_hr": info["Transit Duration (hrs)"],
                "ra_deg": info.get("RA"),
                "dec_deg": info.get("DEC"),
            }
    return list(planets.values())


def _merged_intervals(starts, stops) -> list:
    intervals = sorted(zip(starts, stops))
    merged = []
    for start, stop in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _covered_fraction(intervals, start, stop) -> float:
    total = (stop - start).total_seconds()
    if total <= 0:
        return 0.0
    covered = 0.0
    for i_start, i_stop in intervals:
        lo, hi = max(i_start, start), min(i_stop, stop)
        if hi > lo:
            covered += (hi - lo).total_seconds()
    return covered / total


def transit_counts(
    observations: pd.DataFrame,
    target_index,
    coverage_min=TRANSIT_COVERAGE_MIN,
    baseline_hr=BASELINE_HR,
    baseline_coverage_min=BASELINE_COVERAGE_MIN,
    include_partial=False,
) -> pd.Series:
    """Observed transit events per exoplanet target.

    An event counts when the union of successful executed windows covers at least
    ``coverage_min`` of ingress to egress and at least ``baseline_coverage_min`` of a
    ``baseline_hr`` out-of-transit block on one side. Fractional coverage rather than the
    design's literal "full span" because low Earth orbit imposes occultation gaps every
    orbit; the thresholds are placeholders pending DPC/SOC criteria.

    Parameters
    ----------
    observations : pd.DataFrame
        The flat observation table.
    target_index : pandoraobservations.targets.TargetIndex
        Resolves targets to their PandoraTargetList definition files.
    include_partial : bool
        Also count coverage from ``partial`` observations (default: ``success`` only).

    Returns
    -------
    pd.Series
        ``n_transits_observed`` indexed by ``target_key``; only targets with a transit
        ephemeris appear. Empty when no PandoraTargetList checkout is available.
    """
    try:
        find_target_list_dir(target_index.target_list_dir)
    except FileNotFoundError:
        logger.info("PandoraTargetList unavailable; transit counting skipped.")
        return pd.Series(dtype="float64", name="n_transits_observed").rename_axis("target_key")

    verdicts = ("success", "partial") if include_partial else ("success",)
    rows = observations[observations["verdict"].isin(verdicts) & observations["executed_start_utc"].notna()]
    baseline = timedelta(hours=baseline_hr)

    counts = {}
    for target_key, group in rows.groupby("target_key"):
        planets = _planet_ephemerides(target_index, target_key)
        if not planets:
            continue
        intervals = _merged_intervals(group["executed_start_utc"], group["executed_stop_utc"])
        observed = 0
        for planet in planets:
            for ingress, egress in transit_windows_utc(planet, intervals[0][0], intervals[-1][1]):
                if _covered_fraction(intervals, ingress, egress) < coverage_min:
                    continue
                before = _covered_fraction(intervals, ingress - baseline, ingress)
                after = _covered_fraction(intervals, egress, egress + baseline)
                if max(before, after) >= baseline_coverage_min:
                    observed += 1
        counts[target_key] = float(observed)
    return pd.Series(counts, dtype="float64", name="n_transits_observed").rename_axis("target_key")
