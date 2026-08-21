"""Calendar ingest: Science calendar XML deliveries into calendar records.

``shortschedule`` does the structural parsing. This module adds the pieces it drops (the
full ``<Meta>`` element and the Boresight ``PRI_CMD_DIR``), casts payload values, and
handles revision supersession. ``Calendar_Status`` is recorded verbatim and never gates
ingest; the scheduler's claimed totals are cross-checked against what was actually parsed
and any disagreement is recorded, not raised.
"""

# Standard library
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# Third-party
from shortschedule import parse_science_calendar

# First-party/Local
from pandoraobservations import logger
from pandoraobservations.database import ObservationDatabase, sha256_of_file
from pandoraobservations.schema import CalendarInfo, CalendarRecord, Observation, RequestedWindow, Source
from pandoraobservations.targets import normalize_target

# The calendar XML namespace, as written by the short-term scheduler.
NS = "{/pandora/calendar/}"
# Delivered file names look like PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831-R002.xml.
DELIVERY_RE = re.compile(r"(?P<calendar_id>.+)-R(?P<revision>\d+)\.xml$", re.IGNORECASE)


def _cast(value):
    """Cast a payload parameter string to int or float where it parses as one."""
    if not isinstance(value, str):
        return value
    for kind in (int, float):
        try:
            return kind(value)
        except ValueError:
            pass
    return value


def parse_calendar(xml_path) -> CalendarRecord:
    """Parse one delivered calendar XML into a `CalendarRecord`. Writes nothing.

    Parameters
    ----------
    xml_path : str or Path
        A delivered ``<calendar_id>-R<nnn>.xml`` file.

    Returns
    -------
    CalendarRecord
        The record, with every observation in status ``REQUESTED``.
    """
    xml_path = Path(xml_path)
    match = DELIVERY_RE.match(xml_path.name)
    if match is None:
        raise ValueError(f"{xml_path.name} does not look like a calendar delivery (expected <name>-R<nnn>.xml).")
    calendar_id = match["calendar_id"]
    revision = int(match["revision"])

    parsed = parse_science_calendar(str(xml_path))

    # Second pass over the raw XML for what shortschedule drops: the full <Meta> attribute
    # set (kept verbatim so new scheduler versions never force a schema change) and each
    # sequence's Boresight PRI_CMD_DIR.
    root = ET.parse(xml_path).getroot()
    meta_raw = dict(root.find(f"{NS}Meta").attrib)
    pri_cmd_dirs = {}
    for visit_el in root.iter(f"{NS}Visit"):
        visit_id = visit_el.findtext(f"{NS}ID")
        for seq_el in visit_el.iter(f"{NS}Observation_Sequence"):
            text = seq_el.findtext(f"{NS}Observational_Parameters/{NS}Boresight/{NS}PRI_CMD_DIR")
            if text is not None:
                pri_cmd_dirs[(visit_id, seq_el.findtext(f"{NS}ID"))] = int(text)

    observations = []
    for visit in parsed.visits:
        for seq in visit.sequences:
            observations.append(
                Observation(
                    obs_id=f"{calendar_id}:R{revision:03d}:V{visit.id}:S{seq.id}",
                    calendar_id=calendar_id,
                    revision=revision,
                    visit_id=visit.id,
                    sequence_id=seq.id,
                    target=seq.target,
                    target_key=normalize_target(seq.target),
                    priority=int(seq.priority),
                    requested=RequestedWindow(
                        start_utc=seq.start_time.isot,
                        stop_utc=seq.stop_time.isot,
                        duration_s=round((seq.stop_time - seq.start_time).sec, 3),
                        ra_deg=float(seq.ra),
                        dec_deg=float(seq.dec),
                        roll_deg=float(seq.roll),
                        pri_cmd_dir=pri_cmd_dirs.get((visit.id, seq.id)),
                    ),
                    payload={key: _cast(value) for key, value in seq.get_flat_payload_parameters().items()},
                )
            )

    calendar = CalendarInfo(
        calendar_id=calendar_id,
        revision=revision,
        valid_from=meta_raw.get("Valid_From", ""),
        expires=meta_raw.get("Expires", ""),
        created=meta_raw.get("Created", ""),
        delivery_id=meta_raw.get("Delivery_Id", ""),
        calendar_status=meta_raw.get("Calendar_Status", ""),
        scheduler_version=meta_raw.get("Short_Term_Scheduler_Version", ""),
        tle_line1=meta_raw.get("TLE_Line1", ""),
        tle_line2=meta_raw.get("TLE_Line2", ""),
        claimed_visits=int(meta_raw["Total_Visits"]) if "Total_Visits" in meta_raw else None,
        claimed_sequences=int(meta_raw["Total_Sequences"]) if "Total_Sequences" in meta_raw else None,
        parsed_visits=len(parsed.visits),
        parsed_sequences=len(observations),
        meta_raw=meta_raw,
    )
    if (calendar.claimed_visits, calendar.claimed_sequences) != (calendar.parsed_visits, calendar.parsed_sequences):
        logger.warning(
            f"{xml_path.name} claims {calendar.claimed_visits} visits / {calendar.claimed_sequences} sequences "
            f"but contains {calendar.parsed_visits} / {calendar.parsed_sequences}; recording both."
        )

    return CalendarRecord(
        ingested_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source=Source(path=str(xml_path.resolve()), sha256=sha256_of_file(xml_path)),
        calendar=calendar,
        observations=observations,
    )


def ingest_calendar(xml_path, data_dir=None) -> Path:
    """Ingest one calendar delivery into the record store.

    Re-ingesting an unchanged file is a no-op. Ingesting a new revision marks every lower
    revision of the same calendar superseded; ingesting a lower revision after a higher one
    writes it already superseded. Re-delivering the same revision with changed content
    replaces that revision's record.

    Parameters
    ----------
    xml_path : str or Path
        The delivered calendar XML.
    data_dir : str or Path, optional
        Explicit data directory; discovered when omitted.

    Returns
    -------
    Path
        The record file for this delivery (the existing one on a no-op).
    """
    xml_path = Path(xml_path)
    db = ObservationDatabase(data_dir)

    digest = sha256_of_file(xml_path)
    for path, record in db.iter_records("calendars"):
        if record["source"]["sha256"] == digest:
            logger.info(f"{xml_path.name} already ingested (unchanged content); nothing to do.")
            return path

    record = parse_calendar(xml_path)
    siblings = [(p, r) for p, r in db.iter_records("calendars") if r["calendar"]["calendar_id"] == record.calendar.calendar_id]

    # Arriving below an already-ingested revision means this delivery is born superseded.
    highest = max((r["calendar"]["revision"] for _, r in siblings), default=-1)
    if highest > record.calendar.revision:
        record.calendar.superseded = True
        for obs in record.observations:
            obs.superseded = True
        logger.info(f"{xml_path.name} is revision {record.calendar.revision} but revision {highest} exists; ingesting as superseded.")

    path = db.write_record("calendars", f"{record.calendar.calendar_id}-R{record.calendar.revision:03d}.json", record)
    logger.info(f"Ingested {xml_path.name}: {record.calendar.parsed_sequences} observations in {record.calendar.parsed_visits} visits.")

    for sibling_path, sibling in siblings:
        if sibling["calendar"]["revision"] < record.calendar.revision and not sibling["calendar"]["superseded"]:
            sibling["calendar"]["superseded"] = True
            for obs in sibling["observations"]:
                obs["superseded"] = True
            db.write_record("calendars", sibling_path.name, sibling)
            logger.info(f"Revision {record.calendar.revision} supersedes {sibling_path.name}.")

    return path


def ingest_calendar_directory(directory, data_dir=None) -> list[Path]:
    """Ingest every ``*-R*.xml`` calendar delivery in a directory, in name order."""
    paths = sorted(Path(directory).glob("*.xml"))
    if not paths:
        logger.warning(f"No .xml calendar files found in {directory}.")
    return [ingest_calendar(p, data_dir=data_dir) for p in paths if DELIVERY_RE.match(p.name)]
