"""Target name handling and the PandoraTargetList index.

PandoraTargetList owns all target metadata (coordinates, camera settings, transit
ephemerides); this package copies none of it. What lives here is ``data/target_index.json``,
a lightweight index linking every target name and alias to its definition file(s) inside the
PandoraTargetList ``target_definition_files/`` tree, so lookups need no tree search. See
``docs/schemas/targets.md``. The checkout location comes from the ``target_list_dir`` config
entry (or an explicit argument); index paths are stored relative to the tree so the index
works across machines.
"""

# Standard library
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# First-party/Local
from pandoraobservations import CONFIGPATH, config, logger
from pandoraobservations.database import find_data_dir

INDEX_VERSION = 1
INDEX_NAME = "target_index.json"
DEFINITION_SUFFIX = "_target_definition.json"


def normalize_target(name: str) -> str:
    """Normalize a target name into the key used across the database.

    Lowercase, with runs of whitespace, dashes, and underscores collapsed to single
    underscores, so ``GJ 367``, ``GJ_367``, and ``gj-367`` all key to ``gj_367``.

    Parameters
    ----------
    name : str
        A target name as it appears in a calendar, sequence, or report.

    Returns
    -------
    str
        The normalized key.
    """
    return re.sub(r"[\s_\-]+", "_", name.strip()).lower()


def find_target_list_dir(explicit=None) -> Path:
    """Locate the PandoraTargetList ``target_definition_files`` directory.

    Parameters
    ----------
    explicit : str or Path, optional
        A PandoraTargetList checkout (or its ``target_definition_files`` directory
        directly). Falls back to the ``target_list_dir`` config entry.

    Raises
    ------
    FileNotFoundError
        If no location is configured or the location holds no definition tree.
    """
    location = explicit or config["SETTINGS"].get("target_list_dir", "")
    if not str(location):
        raise FileNotFoundError(
            f"No PandoraTargetList location configured. Set `target_list_dir` in {CONFIGPATH} "
            "to a PandoraTargetList checkout, or pass one explicitly."
        )
    root = Path(location)
    if (root / "target_definition_files").is_dir():
        return root / "target_definition_files"
    if root.name == "target_definition_files" and root.is_dir():
        return root
    raise FileNotFoundError(f"{root} does not contain a target_definition_files directory.")


def build_target_index(target_list_dir=None, data_dir=None) -> Path:
    """Scan the PandoraTargetList tree and (re)write ``data/target_index.json``.

    One index entry per star, keyed by its normalized name, with one ``definition_files``
    entry per category the target appears in (targets are intentionally in multiple
    categories). Previously recorded unresolved names are kept unless they now resolve.

    Parameters
    ----------
    target_list_dir : str or Path, optional
        PandoraTargetList location; falls back to config.
    data_dir : str or Path, optional
        Explicit data directory; discovered when omitted.

    Returns
    -------
    Path
        The index file written.
    """
    tree = find_target_list_dir(target_list_dir)
    index_path = find_data_dir(data_dir) / INDEX_NAME

    targets = {}
    for definition in sorted(tree.glob(f"*/*{DEFINITION_SUFFIX}")):
        try:
            info = json.loads(definition.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"Skipping unreadable target definition {definition}.")
            continue
        stem = definition.name[: -len(DEFINITION_SUFFIX)]
        star = info.get("Star Name") or stem
        entry = targets.setdefault(normalize_target(star), {"aliases": [], "definition_files": []})
        for name in (star, info.get("Planet Name"), stem):
            if name and name not in entry["aliases"]:
                entry["aliases"].append(name)
        entry["definition_files"].append(
            {
                "category": definition.parent.name,
                "path": f"{definition.parent.name}/{definition.name}",
                "file_version": str(info.get("Version", "")),
            }
        )

    # Keep previously noted unresolved names, dropping any that now resolve.
    unresolved = []
    if index_path.exists():
        known = set(targets) | {normalize_target(a) for entry in targets.values() for a in entry["aliases"]}
        previous = json.loads(index_path.read_text(encoding="utf-8")).get("unresolved", [])
        unresolved = sorted(name for name in set(previous) if normalize_target(name) not in known)

    payload = {
        "schema_version": INDEX_VERSION,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "targets": {key: targets[key] for key in sorted(targets)},
        "unresolved": unresolved,
    }
    tmp = index_path.with_name(index_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, index_path)
    logger.info(f"Target index built: {len(targets)} targets from {tree}.")
    return index_path


class TargetIndex:
    """Lookup from any target name or alias to its PandoraTargetList definition files.

    Loads ``data/target_index.json``, building it first if absent. A lookup miss triggers
    one rescan of the PandoraTargetList tree (targets are added there regularly); a name
    that still misses is recorded in the index's ``unresolved`` list for a human to sort
    out.

    Parameters
    ----------
    data_dir : str or Path, optional
        Explicit data directory; discovered when omitted.
    target_list_dir : str or Path, optional
        PandoraTargetList location; falls back to config.
    """

    def __init__(self, data_dir=None, target_list_dir=None):
        self.target_list_dir = target_list_dir
        self.path = find_data_dir(data_dir) / INDEX_NAME
        if not self.path.exists():
            build_target_index(target_list_dir, self.path.parent)
        self._rescanned = False
        self._load()

    def _load(self):
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._key_by_alias = {}
        for key, entry in self.data["targets"].items():
            self._key_by_alias[key] = key
            for alias in entry["aliases"]:
                self._key_by_alias.setdefault(normalize_target(alias), key)

    def resolve(self, name: str):
        """Return ``{"target_key": ..., "aliases": ..., "definition_files": ...}`` or None.

        Parameters
        ----------
        name : str
            Any spelling of a target name, e.g. from a calendar or quality report.
        """
        key = self._key_by_alias.get(normalize_target(name))
        if key is not None:
            return {"target_key": key, **self.data["targets"][key]}

        if not self._rescanned:
            self._rescanned = True
            try:
                build_target_index(self.target_list_dir, self.path.parent)
            except FileNotFoundError:
                pass  # no tree on this machine (e.g. fresh clone); work with the index as-is
            else:
                self._load()
                return self.resolve(name)

        if name not in self.data["unresolved"]:
            self.data["unresolved"] = sorted(self.data["unresolved"] + [name])
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
            logger.warning(f"Target {name!r} not found in PandoraTargetList; recorded as unresolved.")
        return None

    def definition_paths(self, name: str) -> list[Path]:
        """Absolute paths of a target's definition files, empty when unresolved."""
        entry = self.resolve(name)
        if entry is None:
            return []
        tree = find_target_list_dir(self.target_list_dir)
        return [tree / item["path"] for item in entry["definition_files"]]
