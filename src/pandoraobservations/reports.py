"""Quality report ingest and the success metrics registry.

Reports are produced by the analysis tool (likely ``pandora-fits`` reporting, a cron job on
the science server) in the format defined by ``docs/schemas/quality-report.md``. What counts
as scientific success is defined entirely by ``success_metrics.json``
(``docs/schemas/success-metrics.md``): a standalone versioned file so the criteria can be
tracked, traded, and copied downstream without a package release. Every verdict this module
computes is stamped with the ``metrics_version`` that produced it, and record files keep
that stamp forever; re-judging history under new criteria happens in the cache rebuild, not
here.

Report entries match observations on target plus greatest time overlap, never on ``obs_id``
alone (ids are not stable across calendar revisions). Entries matching nothing land in the
record's ``unmatched`` bucket rather than being dropped.
"""

# Standard library
import json
from datetime import datetime, timezone
from pathlib import Path

# First-party/Local
from pandoraobservations import PACKAGEDIR, config, logger
from pandoraobservations.database import ObservationDatabase, sha256_of_file
from pandoraobservations.sequences import overlap_s, to_utc
from pandoraobservations.targets import normalize_target

REPORT_RECORD_VERSION = 1
PACKAGED_METRICS = Path(PACKAGEDIR) / "data" / "success_metrics.json"


def load_success_metrics(path=None) -> dict:
    """Load the success metrics file.

    Resolution order: the ``path`` argument, the ``success_metrics_path`` config entry,
    then the copy packaged with this release.

    Parameters
    ----------
    path : str or Path, optional
        An alternate metrics file, e.g. a team-shared draft.

    Returns
    -------
    dict
        The parsed metrics file.
    """
    location = path or config["SETTINGS"].get("success_metrics_path", "") or PACKAGED_METRICS
    metrics = json.loads(Path(location).read_text(encoding="utf-8"))
    if "metrics_version" not in metrics or "metrics" not in metrics:
        raise ValueError(f"{location} is not a success metrics file (needs metrics_version and metrics).")
    return metrics


def evaluate_metric(name: str, value, metrics: dict):
    """Return ``pass``/``warn``/``fail`` for one metric value, or None if unknown.

    A metric the file does not define contributes nothing to the verdict; it is stored
    verbatim and starts counting once the metrics file learns about it.
    """
    spec = metrics["metrics"].get(name)
    if spec is None or value is None:
        return None
    value = float(value)
    if spec["direction"] == "lower_is_better":
        return "pass" if value <= spec["pass"] else ("warn" if value <= spec["warn"] else "fail")
    return "pass" if value >= spec["pass"] else ("warn" if value >= spec["warn"] else "fail")


def compute_verdict(statuses: dict, metrics: dict):
    """Combine per-metric statuses into a verdict and overall score.

    Version 1 rules (``docs/schemas/success-metrics.md``): a fail on a required metric is
    ``failed``; any warn, or a fail on a non-required metric, is ``partial``; otherwise
    ``success``. The score is the mean of pass=1, warn=0.5, fail=0 over known metrics; it is
    a placeholder until the DPC/SOC define something better.

    Parameters
    ----------
    statuses : dict
        Metric name to status; None entries (unknown metrics) are ignored.
    metrics : dict
        The loaded success metrics file.

    Returns
    -------
    tuple
        ``(verdict, overall_score)``, both None when no known metric was reported.
    """
    known = {name: status for name, status in statuses.items() if status is not None}
    if not known:
        return None, None
    verdict = "success"
    for name, status in known.items():
        if status == "fail" and metrics["metrics"][name].get("required", False):
            verdict = "failed"
            break
        if status in ("warn", "fail"):
            verdict = "partial"
    score = sum({"pass": 1.0, "warn": 0.5, "fail": 0.0}[status] for status in known.values()) / len(known)
    return verdict, round(score, 3)


def _active_observations(db: ObservationDatabase) -> list[tuple[str, dict, dict]]:
    """Every active observation with its record file name and parsed requested window."""
    out = []
    for path, record in db.iter_records("calendars"):
        if record["calendar"]["superseded"]:
            continue
        for obs in record["observations"]:
            if not obs["superseded"]:
                window = (to_utc(obs["requested"]["start_utc"]), to_utc(obs["requested"]["stop_utc"]))
                out.append((path.name, obs, window))
    return out


def _match_entry(entry: dict, candidates: list) -> tuple:
    """Match one report entry to the observation it overlaps most, same target only.

    The entry's ``obs_id`` is a hint: it wins if it names a candidate that overlaps at all,
    but never matches on its own.
    """
    start, stop = to_utc(entry["start_utc"]), to_utc(entry["stop_utc"])
    key = normalize_target(entry["target"])
    overlapping = []
    for record_name, obs, (req_start, req_stop) in candidates:
        if obs["target_key"] != key:
            continue
        ov = overlap_s(start, stop, req_start, req_stop)
        if ov > 0:
            overlapping.append((ov, record_name, obs))
    if not overlapping:
        return None, None
    hinted = [item for item in overlapping if item[2]["obs_id"] == entry.get("obs_id")]
    _, record_name, obs = hinted[0] if hinted else max(overlapping, key=lambda item: item[0])
    return record_name, obs


def _quality_block(entry: dict, metrics: dict, generated_utc: str) -> dict:
    """Evaluate one report entry against the metrics file and build the quality block."""
    evaluated = {}
    statuses = {}
    for name, envelope in entry.get("metrics", {}).items():
        value = envelope.get("value")
        local = evaluate_metric(name, value, metrics)
        statuses[name] = local
        stored = {"value": value, "status": local if local is not None else envelope.get("status")}
        if local is not None and envelope.get("status") not in (None, local):
            stored["producer_status"] = envelope["status"]
        evaluated[name] = stored

    verdict, score = compute_verdict(statuses, metrics)
    source = "local"
    if verdict is None and entry.get("verdict"):
        # No metric we know how to judge: the producer's call is better than nothing.
        verdict, score, source = entry["verdict"], entry.get("overall_score"), "producer"

    block = {
        "metrics": evaluated,
        "overall_score": score,
        "verdict": verdict,
        "verdict_source": source,
        "metrics_version": metrics["metrics_version"],
        "report_generated_utc": generated_utc,
    }
    if entry.get("verdict") and entry["verdict"] != verdict:
        block["producer_verdict"] = entry["verdict"]
    if entry.get("notes"):
        block["notes"] = entry["notes"]
    return block


def ingest_report(report_path, data_dir=None, metrics_path=None) -> Path:
    """Ingest one quality report: record it and update the matched observations.

    Matched observations gain ``executed`` and ``quality`` blocks and a status of
    ``SUCCESS``/``PARTIAL``/``FAILED`` (or ``EXECUTED`` when no verdict is computable). A
    report entry only overwrites an observation's existing quality when it comes from a
    report generated at or after the one already applied, so a stale partial report arriving
    late changes nothing. Unmatched entries are stored in the record's ``unmatched`` bucket.
    Re-ingesting an unchanged report file is a no-op.

    Parameters
    ----------
    report_path : str or Path
        The quality report JSON (``docs/schemas/quality-report.md``).
    data_dir : str or Path, optional
        Explicit data directory; discovered when omitted.
    metrics_path : str or Path, optional
        Alternate success metrics file; see `load_success_metrics`.

    Returns
    -------
    Path
        The report record file (the existing one on a no-op).
    """
    report_path = Path(report_path)
    db = ObservationDatabase(data_dir)

    digest = sha256_of_file(report_path)
    for path, record in db.iter_records("reports"):
        if record["source"]["sha256"] == digest:
            logger.info(f"{report_path.name} already ingested (unchanged content); nothing to do.")
            return path

    report = json.loads(report_path.read_text(encoding="utf-8"))
    for field in ("report_id", "generated_utc", "coverage", "observations"):
        if field not in report:
            raise ValueError(f"{report_path.name} is missing required report field {field!r}.")
    metrics = load_success_metrics(metrics_path)

    candidates = _active_observations(db)
    touched: dict[str, dict] = {}
    matches, unmatched = [], []
    stale = 0
    for entry in report["observations"]:
        record_name, obs = _match_entry(entry, candidates)
        if obs is None:
            unmatched.append(entry)
            continue
        matches.append({"obs_id": obs["obs_id"], "target": entry["target"], "start_utc": entry["start_utc"]})

        existing = obs.get("quality")
        if existing and existing.get("report_generated_utc", "") > report["generated_utc"]:
            stale += 1
            continue

        obs["executed"] = {
            "start_utc": entry["start_utc"],
            "stop_utc": entry["stop_utc"],
            "data_products": entry.get("data_products", []),
            "engineering_data": entry.get("engineering_data"),
            "report_id": report["report_id"],
        }
        obs["quality"] = _quality_block(entry, metrics, report["generated_utc"])
        obs["status"] = obs["quality"]["verdict"].upper() if obs["quality"]["verdict"] else "EXECUTED"
        touched[record_name] = None

    # The edits above live on the candidate obs dicts; splice them into a fresh read of each
    # touched record by obs_id, then write the record back.
    for record_name in touched:
        record = db.read_record("calendars", record_name)
        edited = {o["obs_id"]: o for name, o, _ in candidates if name == record_name}
        record["observations"] = [edited.get(o["obs_id"], o) for o in record["observations"]]
        db.write_record("calendars", record_name, record)

    record = {
        "schema_version": REPORT_RECORD_VERSION,
        "ingested_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"path": str(report_path.resolve()), "sha256": digest},
        "report": report,
        "matched": matches,
        "unmatched": unmatched,
    }
    record_file = db.write_record("reports", f"{report['report_id']}.json", record)
    logger.info(
        f"Ingested {report_path.name}: {len(matches)} matched ({stale} stale, skipped), "
        f"{len(unmatched)} unmatched, {len(report.get('unanalyzed', []))} unanalyzed."
    )
    return record_file
