"""The JSON record store: data directory discovery, reads, writes, and re-ingest no-ops.

Layout (see ``docs/schemas/README.md``)::

    data/
      calendars/  sequences/  reports/  edf/  cache/
      pandora_obs_data.json   <- marker file, written by init_data_dir()

The record layer is append-only with respect to deliveries: re-ingesting a source file whose
hash is already on disk is a no-op, and nothing here deletes records. Existing record files
are only rewritten to add downstream blocks (scheduled/executed/quality) or flags.
"""

# Standard library
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# First-party/Local
from pandoraobservations import CONFIGPATH, config, logger

DATA_MARKER = "pandora_obs_data.json"
DATA_LAYOUT_VERSION = 1
# Directory names that hold JSON record files (edf/ and cache/ are created but hold other formats).
RECORD_KINDS = ("calendars", "sequences", "reports")


def sha256_of_file(path) -> str:
    """Return the SHA-256 hex digest of a file, streamed so large sequences are cheap.

    Parameters
    ----------
    path : str or Path
        File to hash.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def init_data_dir(root) -> Path:
    """Create the data directory layout and its marker file.

    Parameters
    ----------
    root : str or Path
        The data directory itself (for this repo, ``<repo root>/data``).

    Returns
    -------
    Path
        The initialized data directory.
    """
    root = Path(root)
    for name in RECORD_KINDS + ("edf", "cache"):
        (root / name).mkdir(parents=True, exist_ok=True)
    marker = root / DATA_MARKER
    if not marker.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker.write_text(
            json.dumps({"layout_version": DATA_LAYOUT_VERSION, "created_utc": stamp}, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info(f"Initialized data directory at {root}")
    return root


def find_data_dir(explicit=None) -> Path:
    """Locate the data directory without creating one.

    Resolution order:

    1. The ``explicit`` argument. It must already be initialized; pointing at an
       uninitialized directory raises an error.
    2. The ``data_dir`` entry in the package config file, if it holds an initialized
       data directory (the default config value does not, so it is normally skipped).
    3. Walking up from the current working directory looking for ``data/pandora_obs_data.json``.

    Parameters
    ----------
    explicit : str or Path, optional
        A data directory chosen by the caller. Overrides all discovery.

    Returns
    -------
    Path
        The data directory containing the marker file.

    Raises
    ------
    FileNotFoundError
        If no initialized data directory can be found.
    """
    if explicit is not None:
        root = Path(explicit)
        if not (root / DATA_MARKER).exists():
            raise FileNotFoundError(
                f"{root} is not an initialized data directory (no {DATA_MARKER}). "
                "Run pandoraobservations.database.init_data_dir() on it first."
            )
        return root

    configured = config["SETTINGS"].get("data_dir", "")
    if configured and (Path(configured) / DATA_MARKER).exists():
        return Path(configured)

    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "data"
        if (candidate / DATA_MARKER).exists():
            return candidate

    raise FileNotFoundError(
        "No pandora-observations data directory found. Either pass one explicitly, set "
        f"`data_dir` in the config file ({CONFIGPATH}), or run from within the repo after "
        "pandoraobservations.database.init_data_dir('<repo>/data')."
    )


class ObservationDatabase:
    """Read and write access to the JSON record store.

    Parameters
    ----------
    data_dir : str or Path, optional
        Explicit data directory. When omitted, the directory is discovered per
        `find_data_dir`.
    """

    def __init__(self, data_dir=None):
        self.root = find_data_dir(data_dir)

    def _kind_dir(self, kind: str) -> Path:
        if kind not in RECORD_KINDS:
            raise ValueError(f"Unknown record kind {kind!r}; expected one of {RECORD_KINDS}.")
        return self.root / kind

    def write_record(self, kind: str, name: str, record) -> Path:
        """Write one record file, atomically replacing any existing file of that name.

        Parameters
        ----------
        kind : str
            One of ``calendars``, ``sequences``, ``reports``.
        name : str
            File name including the ``.json`` extension.
        record : dict or object with a ``to_dict()`` method
            The record content.

        Returns
        -------
        Path
            The path written.
        """
        if hasattr(record, "to_dict"):
            record = record.to_dict()
        path = self._kind_dir(kind) / name
        if path.exists():
            logger.info(f"Updating existing record {path.name}")
        # Write to a sibling temp file then replace, so a crash never leaves a half-written record.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    def read_record(self, kind: str, name: str) -> dict:
        """Load one record file as a dict."""
        return json.loads((self._kind_dir(kind) / name).read_text(encoding="utf-8"))

    def iter_records(self, kind: str):
        """Yield ``(path, record_dict)`` for every record of a kind, sorted by file name."""
        for path in sorted(self._kind_dir(kind).glob("*.json")):
            yield path, json.loads(path.read_text(encoding="utf-8"))

    def is_ingested(self, kind: str, sha256: str) -> bool:
        """True when a record of this kind already carries this source hash.

        This is the re-ingest guard: an unchanged delivery is a no-op, while a changed
        delivery (different hash) ingests as a new record.
        """
        for _, record in self.iter_records(kind):
            if record.get("source", {}).get("sha256") == sha256:
                return True
        return False
