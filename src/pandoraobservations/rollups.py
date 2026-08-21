"""Per-target rollups, computed during the cache build and never stored in the records.

"""

# Third-party
import pandas as pd

SUMMARY_COLUMNS = [
    "target", "n_observations", "requested_hours", "n_requested_days",
    "max_priority", "first_start_utc", "last_stop_utc",
]


def target_summary(observations: pd.DataFrame) -> pd.DataFrame:
    """Summarize active observations per target.

    Superseded observations are excluded: they describe requests that were replaced by a
    later calendar revision, so counting them would double-book rescheduled windows.

    Parameters
    ----------
    observations : pd.DataFrame
        The flat observation table built by `pandoraobservations.cache.rebuild_cache`.

    Returns
    -------
    pd.DataFrame
        One row per ``target_key``: observation count, total requested hours, number of
        distinct UTC dates with a request, highest priority, first and last window, plus
        one ``n_<status>`` count column per status present.
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
    # One count column per status seen, e.g. n_requested / n_scheduled / n_success.
    counts = active.pivot_table(index="target_key", columns="status", aggfunc="size", fill_value=0)
    counts.columns = [f"n_{status.lower()}" for status in counts.columns]
    return summary.join(counts).sort_values("requested_hours", ascending=False)
