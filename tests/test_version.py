"""Unit tests for __version__.py."""

import diffai.eraf4xrd  # noqa


def test_package_version():
    """Ensure the package version is defined and not set to the initial
    placeholder."""
    assert hasattr(diffai.eraf4xrd, "__version__")
    assert diffai.eraf4xrd.__version__ != "0.0.0"
