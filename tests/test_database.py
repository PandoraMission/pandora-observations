# Standard library
import hashlib

# Third-party
import pytest

# First-party/Local
from pandoraobservations import config
from pandoraobservations.database import (
    DATA_MARKER,
    ObservationDatabase,
    find_data_dir,
    init_data_dir,
    sha256_of_file,
)


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch):
    # Keep the user's real config out of discovery so tests only see tmp_path directories.
    monkeypatch.setitem(config["SETTINGS"], "data_dir", "")


def test_init_data_dir_layout(tmp_path):
    root = init_data_dir(tmp_path / "data")
    for name in ("calendars", "sequences", "reports", "edf", "cache"):
        assert (root / name).is_dir()
    assert (root / DATA_MARKER).exists()
    # Idempotent: a second init must not fail or replace the marker.
    marker_text = (root / DATA_MARKER).read_text()
    init_data_dir(root)
    assert (root / DATA_MARKER).read_text() == marker_text


def test_find_data_dir_explicit(tmp_path):
    root = init_data_dir(tmp_path / "data")
    assert find_data_dir(root) == root
    with pytest.raises(FileNotFoundError):
        find_data_dir(tmp_path / "not_initialized")


def test_find_data_dir_walks_up_from_cwd(tmp_path, monkeypatch):
    root = init_data_dir(tmp_path / "repo" / "data")
    nested = tmp_path / "repo" / "notebooks" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert find_data_dir() == root


def test_find_data_dir_from_config(tmp_path, monkeypatch):
    root = init_data_dir(tmp_path / "elsewhere")
    monkeypatch.setitem(config["SETTINGS"], "data_dir", str(root))
    monkeypatch.chdir(tmp_path)  # nothing discoverable from here by walking up
    assert find_data_dir() == root


def test_find_data_dir_error_names_the_fixes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="data_dir"):
        find_data_dir()


def test_write_read_and_reingest_guard(tmp_path):
    init_data_dir(tmp_path / "data")
    db = ObservationDatabase(tmp_path / "data")

    record = {"schema_version": 1, "source": {"path": "cal.xml", "sha256": "deadbeef"}, "observations": []}
    path = db.write_record("calendars", "cal-R001.json", record)
    assert path.name == "cal-R001.json"
    assert db.read_record("calendars", "cal-R001.json") == record

    assert db.is_ingested("calendars", "deadbeef")
    assert not db.is_ingested("calendars", "0000")
    # Same hash in a different kind is a different delivery stream.
    assert not db.is_ingested("sequences", "deadbeef")

    assert [p.name for p, _ in db.iter_records("calendars")] == ["cal-R001.json"]
    with pytest.raises(ValueError):
        db.write_record("images", "x.json", {})


def test_write_record_accepts_to_dict_objects(tmp_path):
    class Wrapper:
        def to_dict(self):
            return {"schema_version": 1}

    init_data_dir(tmp_path / "data")
    db = ObservationDatabase(tmp_path / "data")
    db.write_record("reports", "r.json", Wrapper())
    assert db.read_record("reports", "r.json") == {"schema_version": 1}


def test_sha256_of_file(tmp_path):
    payload = b"pandora"
    target = tmp_path / "f.bin"
    target.write_bytes(payload)
    assert sha256_of_file(target) == hashlib.sha256(payload).hexdigest()
