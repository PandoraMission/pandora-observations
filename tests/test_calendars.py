# Standard library
from pathlib import Path

# Third-party
import pytest

# First-party/Local
from pandoraobservations.calendars import ingest_calendar, parse_calendar
from pandoraobservations.database import ObservationDatabase, init_data_dir
from pandoraobservations.schema import CalendarRecord, ObservationStatus
from pandoraobservations.targets import normalize_target

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831-R002.xml"
CALENDAR_ID = "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831"


@pytest.fixture(scope="module")
def example_record():
    # Parsing the 19k-line example takes a moment, so share one parse across tests.
    return parse_calendar(EXAMPLE)


def test_parse_example_header(example_record):
    calendar = example_record.calendar
    assert calendar.calendar_id == CALENDAR_ID
    assert calendar.revision == 2
    # The scheduler's claim disagrees with the file contents; both sides are recorded.
    assert (calendar.claimed_visits, calendar.claimed_sequences) == (26, 227)
    assert (calendar.parsed_visits, calendar.parsed_sequences) == (25, 226)
    # INVALID is recorded verbatim and does not block ingest.
    assert calendar.calendar_status == "INVALID"
    assert calendar.scheduler_version == "1.3.0"
    assert calendar.tle_line1.startswith("1 67395U")
    # The full <Meta> element is kept verbatim, including the keepout configuration.
    assert calendar.meta_raw["Sun_Min_Deg"] == "91"
    assert calendar.meta_raw["Min_Power_Frac"] == "0.68"
    assert len(calendar.meta_raw) == 27


def test_parse_example_first_observation(example_record):
    obs = example_record.observations[0]
    assert obs.obs_id == f"{CALENDAR_ID}:R002:V0002:S001"
    assert obs.target == "G4476152832143994112"
    assert obs.target_key == "g4476152832143994112"
    assert obs.priority == 0
    assert obs.status is ObservationStatus.REQUESTED
    assert obs.requested.start_utc == "2026-08-24T00:14:00.000"
    assert obs.requested.duration_s == 960.0
    assert obs.requested.roll_deg == 80.0
    # PRI_CMD_DIR is read from the raw XML since shortschedule does not expose it.
    assert obs.requested.pri_cmd_dir == 9
    # Payload parameters are flattened to dot notation with numerics cast.
    assert obs.payload["AcquireInfCamImages.ROI_StartX"] == 1737
    assert obs.payload["AcquireInfCamImages.TargetID"] == "G4476152832143994112"
    assert len(obs.payload) == 58


def test_ingest_and_noop(tmp_path):
    data_dir = init_data_dir(tmp_path / "data")
    path = ingest_calendar(EXAMPLE, data_dir=data_dir)
    assert path.name == f"{CALENDAR_ID}-R002.json"

    db = ObservationDatabase(data_dir)
    record = CalendarRecord.from_dict(db.read_record("calendars", path.name))
    assert record.calendar.superseded is False
    assert len(record.observations) == 226

    # Unchanged delivery: no-op, still exactly one record.
    again = ingest_calendar(EXAMPLE, data_dir=data_dir)
    assert again == path
    assert len(list(db.iter_records("calendars"))) == 1


def make_r001_copy(directory) -> Path:
    # Same calendar one revision earlier: tweak the delivery id so the content hash differs.
    text = EXAMPLE.read_text(encoding="utf-8").replace('Delivery_Id="bb2a', 'Delivery_Id="0000')
    copy = directory / f"{CALENDAR_ID}-R001.xml"
    copy.write_text(text, encoding="utf-8")
    return copy


def test_new_revision_supersedes_old(tmp_path):
    data_dir = init_data_dir(tmp_path / "data")
    ingest_calendar(make_r001_copy(tmp_path), data_dir=data_dir)
    ingest_calendar(EXAMPLE, data_dir=data_dir)

    db = ObservationDatabase(data_dir)
    old = db.read_record("calendars", f"{CALENDAR_ID}-R001.json")
    new = db.read_record("calendars", f"{CALENDAR_ID}-R002.json")
    assert old["calendar"]["superseded"] is True
    assert all(obs["superseded"] for obs in old["observations"])
    assert new["calendar"]["superseded"] is False
    assert not any(obs["superseded"] for obs in new["observations"])


def test_late_lower_revision_arrives_superseded(tmp_path):
    data_dir = init_data_dir(tmp_path / "data")
    ingest_calendar(EXAMPLE, data_dir=data_dir)
    ingest_calendar(make_r001_copy(tmp_path), data_dir=data_dir)

    db = ObservationDatabase(data_dir)
    old = db.read_record("calendars", f"{CALENDAR_ID}-R001.json")
    new = db.read_record("calendars", f"{CALENDAR_ID}-R002.json")
    assert old["calendar"]["superseded"] is True
    assert new["calendar"]["superseded"] is False


def test_normalize_target():
    assert normalize_target("GJ 367") == "gj_367"
    assert normalize_target("GJ_367") == "gj_367"
    assert normalize_target("HAT-P-11b") == "hat_p_11b"
    assert normalize_target("  BD-16 251 ") == "bd_16_251"
