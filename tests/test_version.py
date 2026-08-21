# First-party/Local
from pandoraobservations import __version__


def test_version():
    # Note that this imports the package as it is installed, not relative
    # to the test directory.
    assert __version__ == "0.0.1"
