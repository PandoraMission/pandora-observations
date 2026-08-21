# Standard library
import shutil
from pathlib import Path

# Third-party
import pytest

# First-party/Local
from pandoraobservations.calendars import ingest_calendar, parse_calendar
from pandoraobservations.database import ObservationDatabase, init_data_dir
from pandoraobservations.sequences import (
    cast_value,
    compare_sequence,
    ingest_sequence,
    parse_command,
    validate_sequence,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CALENDAR_XML = EXAMPLES / "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831-R002.xml"
CALENDAR_ID = "PAN-SCICAL-SCI-20260819-VF-20260824-EX-20260831"
SEQ = EXAMPLES / "S26W35.seq.json"
CONTACTS = EXAMPLES / "ksat_contacts.json"
TELECOM = EXAMPLES / "T26W35.seq.json"


@pytest.fixture(scope="module")
def result():
    calendar = parse_calendar(CALENDAR_XML)
    return compare_sequence(calendar, SEQ, contacts_path=CONTACTS, telecom_path=TELECOM)


def test_command_parsing():
    name, params = parse_command(
        "PANDORA PAYLOAD_READ with CCSDS_AP_ID HSDR, PL_APID 0, PATH '/mnt/data/x.bin', PL_PATH ''"
    )
    assert name == "PAYLOAD_READ"
    assert params == {"CCSDS_AP_ID": "HSDR", "PL_APID": 0, "PATH": "/mnt/data/x.bin", "PL_PATH": ""}
    assert cast_value("0xFFFF") == 65535
    assert cast_value("-1.5e3") == -1500.0
    assert cast_value("[1, 2.5, 'a b']") == [1, 2.5, "a b"]


def test_blocks_and_matching(result):
    seq = result["sequence"]
    assert seq["sequence_file_id"] == "S26W35"
    assert (seq["calendar_id"], seq["revision"]) == (CALENDAR_ID, 2)
    assert seq["command_count"] == 2908
    assert seq["use_cosmos"] is True
    # Every generated block matches a request; nothing was added beyond the calendar.
    assert seq["n_generated_blocks"] == 223
    assert seq["n_matched_blocks"] == 223
    assert result["unmatched_blocks"] == []
    assert len(result["observations"]) == 226


def test_truncation_and_drops(result):
    high = [o for o in result["observations"] if o["priority"] >= 1]
    assert len(high) == 90
    dropped = [o for o in high if o["scheduled_status"] == "dropped"]
    truncated = [o for o in high if o["scheduled_status"] == "truncated"]
    split = [o for o in high if len(o["matched_blocks"]) > 1]
    assert (len(dropped), len(truncated), len(split)) == (0, 8, 2)
    assert sum(sum(o["truncation"].values()) for o in truncated) / 60.0 == pytest.approx(52.00, abs=0.01)

    # The worst cut matches the MOCSeqGen report for this observation to the second.
    worst = max(truncated, key=lambda o: sum(o["truncation"].values()))
    assert worst["obs_id"].endswith(":V0002:S022")
    assert worst["target"] == "TOI-181b"
    assert worst["truncation"] == {"start_late_s": 0.0, "mid_gap_s": 0.0, "end_early_s": 997.0}
    # That cut has a ground contact sitting inside its requested window: the "why".
    assert any(c["ground_station"] == "Troll Station" for c in worst["contact_overlaps"])

    # Only low-priority filler was dropped outright.
    assert sum(1 for o in result["observations"] if o["scheduled_status"] == "dropped") == 7
    assert all(o["priority"] == 0 for o in result["observations"] if o["scheduled_status"] == "dropped")


def test_payload_and_notes(result):
    assert sum(len(o["payload_mismatches"]) for o in result["observations"]) == 0
    notes = [n for o in result["observations"] for n in o["notes"]]
    # One real quirk in the week: the sequence names the planet, the calendar the star.
    assert notes == ["generated target TOI-7189b != requested TOI-7189"]


def test_telecom_cross_checks(result):
    telecom = result["telecom"]
    assert telecom["command_count"] == 324
    assert telecom["conflicts"] == []  # science and telecom never command at the same instant
    assert telecom["uncorrelated_blocks"] == []  # all telecom activity sits inside a contact


def test_validate_writes_report_only(tmp_path, capsys):
    data_dir = init_data_dir(tmp_path / "data")
    seq_copy = tmp_path / SEQ.name
    shutil.copy(SEQ, seq_copy)

    validate_sequence(seq_copy, CALENDAR_XML, contacts_path=CONTACTS, telecom_path=TELECOM)

    report = (tmp_path / "S26W35_validation_report.txt").read_text(encoding="utf-8")
    assert "# Truncation summary" in report
    assert "Truncated observations (priority >= 1): 8 of 90" in report
    assert "TOI-181b" in report
    assert "Science/telecom conflicts: 0 (must be 0)" in report
    assert "# Truncation summary" in capsys.readouterr().out
    # Validation must write nothing into the database.
    assert list((data_dir / "sequences").glob("*.json")) == []


def test_ingest_updates_calendar(tmp_path):
    data_dir = init_data_dir(tmp_path / "data")
    ingest_calendar(CALENDAR_XML, data_dir=data_dir)
    seq_copy = tmp_path / SEQ.name
    shutil.copy(SEQ, seq_copy)

    record_path = ingest_sequence(seq_copy, CALENDAR_ID, data_dir=data_dir, contacts_path=CONTACTS, telecom_path=TELECOM)
    assert record_path.name == "S26W35.seq.json"

    db = ObservationDatabase(data_dir)
    stored = db.read_record("calendars", f"{CALENDAR_ID}-R002.json")
    statuses = [obs["status"] for obs in stored["observations"]]
    assert statuses.count("DROPPED") == 7
    assert statuses.count("REQUESTED") == 0  # every active observation heard back
    scheduled_blocks = [obs["scheduled"] for obs in stored["observations"] if obs["scheduled"]]
    assert len(scheduled_blocks) == 226
    assert all(block["sequence_file_id"] == "S26W35" for block in scheduled_blocks)

    # Unchanged re-delivery is a no-op.
    assert ingest_sequence(seq_copy, CALENDAR_ID, data_dir=data_dir) == record_path
    assert len(list(db.iter_records("sequences"))) == 1
