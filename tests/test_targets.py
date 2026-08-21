# Standard library
import json

# Third-party
import pytest

# First-party/Local
from pandoraobservations import config
from pandoraobservations.database import init_data_dir
from pandoraobservations.targets import TargetIndex, build_target_index, find_target_list_dir


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch):
    monkeypatch.setitem(config["SETTINGS"], "data_dir", "")
    monkeypatch.setitem(config["SETTINGS"], "target_list_dir", "")


def write_definition(tree, category, stem, info):
    folder = tree / category
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}_target_definition.json").write_text(json.dumps(info), encoding="utf-8")


@pytest.fixture
def target_list(tmp_path):
    # A miniature PandoraTargetList checkout, shaped like the real one.
    checkout = tmp_path / "PandoraTargetList"
    tree = checkout / "target_definition_files"
    write_definition(
        tree, "auxiliary-exoplanet", "GJ_367b",
        {"Star Name": "GJ_367", "Planet Name": "GJ_367b", "Version": "1.0.0", "Period (days)": 0.3219225},
    )
    # The same star in a second category: must merge into one entry with two files.
    write_definition(tree, "occultation-standard", "GJ_367", {"Star Name": "GJ_367", "Version": "1.1.0"})
    write_definition(tree, "auxiliary-standard", "G4476152832143994112", {"Star Name": "G4476152832143994112", "Version": "1.0.0"})
    # Non-definition files in the tree must be ignored.
    (tree / "vda_readout_schemes.json").write_text("{}", encoding="utf-8")
    return checkout


@pytest.fixture
def data_dir(tmp_path):
    return init_data_dir(tmp_path / "data")


def test_find_target_list_dir(target_list):
    tree = target_list / "target_definition_files"
    assert find_target_list_dir(target_list) == tree  # checkout root accepted
    assert find_target_list_dir(tree) == tree  # the tree itself accepted
    with pytest.raises(FileNotFoundError, match="target_list_dir"):
        find_target_list_dir()  # nothing configured


def test_build_index(target_list, data_dir):
    path = build_target_index(target_list, data_dir)
    index = json.loads(path.read_text(encoding="utf-8"))
    assert set(index["targets"]) == {"gj_367", "g4476152832143994112"}

    entry = index["targets"]["gj_367"]
    assert set(entry["aliases"]) == {"GJ_367", "GJ_367b"}
    assert [f["category"] for f in entry["definition_files"]] == ["auxiliary-exoplanet", "occultation-standard"]
    assert entry["definition_files"][0]["file_version"] == "1.0.0"
    assert index["unresolved"] == []


def test_resolve_by_any_alias(target_list, data_dir):
    index = TargetIndex(data_dir, target_list)
    for spelling in ("GJ_367", "GJ 367b", "gj-367", "G4476152832143994112"):
        assert index.resolve(spelling) is not None
    assert index.resolve("GJ 367b")["target_key"] == "gj_367"

    paths = index.definition_paths("GJ_367")
    assert len(paths) == 2
    assert all(p.exists() for p in paths)


def test_miss_triggers_rescan(target_list, data_dir):
    index = TargetIndex(data_dir, target_list)
    write_definition(target_list / "target_definition_files", "auxiliary-standard", "TOI-999", {"Star Name": "TOI-999", "Version": "1.0.0"})
    # Not in the loaded index, but one automatic rescan of the tree finds it.
    assert index.resolve("TOI-999")["target_key"] == "toi_999"


def test_unresolved_recorded_and_cleared(target_list, data_dir):
    index = TargetIndex(data_dir, target_list)
    assert index.resolve("MYSTERY_STAR") is None
    assert index.resolve("MYSTERY_STAR") is None  # no duplicate entry
    on_disk = json.loads(index.path.read_text(encoding="utf-8"))
    assert on_disk["unresolved"] == ["MYSTERY_STAR"]

    # Rebuilding keeps it while it still fails to resolve...
    build_target_index(target_list, data_dir)
    assert json.loads(index.path.read_text(encoding="utf-8"))["unresolved"] == ["MYSTERY_STAR"]

    # ...and drops it once PandoraTargetList gains the target.
    info = {"Star Name": "MYSTERY_STAR", "Version": "1.0.0"}
    write_definition(target_list / "target_definition_files", "auxiliary-standard", "MYSTERY_STAR", info)
    build_target_index(target_list, data_dir)
    assert json.loads(index.path.read_text(encoding="utf-8"))["unresolved"] == []
