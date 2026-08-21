# Standard library
import json

# First-party/Local
from pandoraobservations.schema import (
    CalendarInfo,
    CalendarRecord,
    Observation,
    ObservationStatus,
    RequestedWindow,
    Source,
)


def make_record() -> CalendarRecord:
    observation = Observation(
        obs_id="PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831:R002:V0002:S001",
        calendar_id="PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831",
        revision=2,
        visit_id="0002",
        sequence_id="001",
        target="G4476152832143994112",
        target_key="g4476152832143994112",
        priority=0,
        requested=RequestedWindow(
            start_utc="2026-08-24T00:14:00.000",
            stop_utc="2026-08-24T00:30:00.000",
            duration_s=960.0,
            ra_deg=269.859559,
            dec_deg=8.231819,
            roll_deg=80.0,
            pri_cmd_dir=9,
        ),
        payload={"AcquireInfCamImages.SC_Integrations": 115},
    )
    calendar = CalendarInfo(
        calendar_id="PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831",
        revision=2,
        valid_from="2026-08-24T00:00:00.000",
        expires="2026-08-31T00:00:00.000",
        created="2026-08-19T17:17:23.262",
        delivery_id="bb2a0d0e-7a4d-48d6-b8d0-930613c02651",
        calendar_status="INVALID",
        scheduler_version="1.3.0",
        tle_line1="1 67395U ...",
        tle_line2="2 67395 ...",
        claimed_visits=26,
        claimed_sequences=227,
        parsed_visits=26,
        parsed_sequences=227,
        meta_raw={"Calendar_Status": "INVALID", "Min_Power_Frac": "0.68"},
    )
    return CalendarRecord(
        ingested_utc="2026-08-21T12:00:00Z",
        source=Source(path="examples/PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831-R002.xml", sha256="abc123"),
        calendar=calendar,
        observations=[observation],
    )


def test_round_trip_through_json():
    record = make_record()
    rebuilt = CalendarRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert rebuilt == record


def test_json_layout():
    data = make_record().to_dict()
    # Header-first ordering, matching docs/schemas/calendar-record.md.
    assert list(data)[:2] == ["schema_version", "ingested_utc"]
    # The status enum must serialize as a plain string.
    assert json.loads(json.dumps(data))["observations"][0]["status"] == "REQUESTED"


def test_new_observation_defaults():
    observation = make_record().observations[0]
    assert observation.status is ObservationStatus.REQUESTED
    assert observation.superseded is False
    assert observation.scheduled is None and observation.executed is None and observation.quality is None
