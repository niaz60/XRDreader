"""Unit tests for __version__.py."""

import diffai.xrdreader  # noqa


def test_package_version():
    """Ensure the package version is defined and not set to the initial
    placeholder."""
    assert hasattr(diffai.xrdreader, "__version__")
    assert diffai.xrdreader.__version__ != "0.0.0"
