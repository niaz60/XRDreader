"""Smoke tests: the package and its stdlib-only config import cleanly."""

import importlib


def test_package_version():
    pkg = importlib.import_module("diffai.xrdreader")
    assert pkg.__version__


def test_config_imports():
    cfg = importlib.import_module("diffai.xrdreader.config")
    assert hasattr(cfg, "RUN_DOWNLOAD")


def test_app_entrypoint_exists():
    app = importlib.import_module("diffai.xrdreader.app")
    assert callable(app.main) and callable(app.run)
