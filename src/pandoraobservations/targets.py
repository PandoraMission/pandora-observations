"""Target name handling.

For now this holds only name normalization, which calendar ingest needs. The target index
build and lookup against PandoraTargetList lands with build item 5 (see
``docs/schemas/targets.md``).
"""

# Standard library
import re


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
