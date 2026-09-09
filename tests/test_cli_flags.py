"""CLI flags -> environment mapping (diffai.eraf4xrd.app._apply_cli_flags).

These run offline: _apply_cli_flags only translates flags into os.environ; it
does not import config or make any network calls.
"""

import os

import pytest

from diffai.eraf4xrd.app import STEP_ENVS, _apply_cli_flags


@pytest.fixture(autouse=True)
def _restore_environ():
    """Snapshot os.environ before each test and restore it after."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_full_run_turns_every_step_on():
    _apply_cli_flags(["--full-run"])
    for var in STEP_ENVS:
        assert os.environ[var] == "true"


def test_skip_screening_turns_step0_off_but_keeps_the_rest():
    _apply_cli_flags(["--full-run", "--skip-screening"])
    assert os.environ["RUN_PHASE0_FILTER"] == "false"
    assert os.environ["RUN_PHASE1"] == "true"


def test_skip_download_turns_download_off():
    _apply_cli_flags(["--full-run", "--skip-download"])
    assert os.environ["RUN_DOWNLOAD"] == "false"


def test_steps_selects_only_named_steps():
    _apply_cli_flags(["--steps", "step1"])
    assert os.environ["RUN_PHASE1"] == "true"
    assert os.environ["RUN_DOWNLOAD"] == "false"
    assert os.environ["RUN_PHASE0_FILTER"] == "false"


def test_unknown_step_errors():
    with pytest.raises(SystemExit):
        _apply_cli_flags(["--steps", "bogus"])


def test_sources_enables_only_named():
    _apply_cli_flags(["--sources", "arxiv"])
    assert os.environ["USE_ARXIV"] == "1"
    assert os.environ["USE_SPRINGER"] == "0"


def test_downloads_provider_model_map_through():
    _apply_cli_flags(
        ["-n", "3", "--provider", "claude", "--model", "some-model"]
    )
    assert os.environ["TARGET_DOWNLOADS"] == "3"
    assert os.environ["PROVIDER"] == "claude"
    assert os.environ["MODEL"] == "some-model"


def test_input_dir_implies_no_download():
    _apply_cli_flags(["-i", "some_folder"])
    assert os.environ["RUN_DOWNLOAD"] == "false"
    assert os.environ["OUTPUT_PDF_DIR"] == "some_folder"


def test_set_overrides_any_key():
    _apply_cli_flags(["--set", "PHASE1_MODEL=gpt-4o"])
    assert os.environ["PHASE1_MODEL"] == "gpt-4o"
