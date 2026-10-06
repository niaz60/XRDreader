"""Tests for the run report builder.

The report reads a run's own output files, so the fixture here is a minimal
run folder: one kept document, one rejected one, and just enough of the JSON
each step writes for the page to have something to say.
"""

import io
import json
import os

import pytest

from diffai.xrdreader import report


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


@pytest.fixture
def run_dir(tmp_path):
    """A run folder holding one kept and one rejected document."""
    run = tmp_path / "xrdreader_output" / "2026-10-06_120000"
    results = run / "results"
    results.mkdir(parents=True)

    _write(str(results / "phase0_consolidated.json"), {
        "total": 2, "kept": 1, "rejected": 1,
        "results": [
            {
                "source_pdf": str(run / "documents" / "ArXiv" / "A Copper Study__2601.00001v1.pdf"),
                "_source_file": "A Copper Study_abc123__phase0.json",
                "decision": {"decision": "keep", "confidence": 0.97, "reason": "XRD patterns in Fig. 1."},
                "agent_trace": [{"tool": "get_abstract", "args": {}, "observation": "We report XRD of Cu."}],
            },
            {
                "source_pdf": str(run / "documents" / "ArXiv" / "A Spectrometer__2601.00002v1.pdf"),
                "_source_file": "A Spectrometer_def456__phase0.json",
                "decision": {"decision": "reject", "confidence": 0.91, "reason": "Instrumentation, no XRD."},
                "agent_trace": [{"tool": "search_pdf", "args": {"pattern": "XRD"}, "observation": "(no matches)"}],
            },
        ],
    })
    _write(str(results / "usage_report.json"), {
        "grand_total": {"requests": 9, "cost_usd": 0.5, "pipeline_wall_seconds": 60.0},
    })
    _write(str(results / "per_pdf_timing.json"), {
        "n_pdfs": 2,
        "per_pdf": {
            "A Copper Study_abc123.pdf": {
                "total_cost": 0.48, "total_calls": 8,
                "phases": {"phase0": {"cost": 0.01, "calls": 1, "wall": 2.0},
                           "phase3_agent": {"cost": 0.47, "calls": 7, "wall": 40.0}},
            },
            "A Spectrometer_def456.pdf": {
                "total_cost": 0.02, "total_calls": 1,
                "phases": {"phase0": {"cost": 0.02, "calls": 1, "wall": 3.0}},
            },
        },
    })
    _write(str(results / "run_manifest.json"), {
        "timestamp": "2026-10-06T12:00:00",
        "master_switches": {"RUN_DOWNLOAD": True, "RUN_PHASE0_FILTER": True},
        "download_sources": {"USE_ARXIV": 1, "TARGET_DOWNLOADS": 2},
        "search": {"ELEMENTS": "Cu OR Copper"},
        "model": {"PROVIDER": "gpt", "MODEL": "gpt-5.2"},
    })
    _write(str(results / "A Copper Study_abc123__phase3_validated_FINAL.json"), {
        "title": "A Copper Study", "doi": "10.0000/test", "source_type": "arxiv",
        "figures": [{
            "figure_number": 1, "page": 3, "caption": "FIG. 1. XRD patterns.",
            "xrd_metadata": {"space_group": ["Fm-3m"], "scan_range_2theta": ""},
            "plot_info": {"x_axis_label": "2theta (deg)"},
        }],
    })
    _write(str(results / "A Copper Study_abc123__phase3_validation_log.json"), {
        "figures": [{
            "agent_trace": [{"tool": "validate_space_group", "args": {"space_group": "Fm-3m"},
                             "observation": "{\"status\": \"consistent\"}"}],
            "verified": {"space_group": {"candidate_value": ["Fm3m"], "corrected_value": ["Fm-3m"],
                                         "evidence_span": "space group Fm-3m"}},
        }],
        "methods_agent_trace": [{"tool": "get_methods_text", "args": {}, "observation": "Cu Ka radiation."}],
    })
    return run


def test_finds_run_from_parent(run_dir, tmp_path):
    """A parent folder resolves to the run inside it."""
    assert report.find_run(str(tmp_path)) == str(run_dir)
    assert report.find_run(str(run_dir)) == str(run_dir)


def test_missing_run_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        report.find_run(str(tmp_path / "nothing-here"))


def test_report_describes_both_documents(run_dir):
    out = report.build_report(str(run_dir))
    assert os.path.exists(out)
    html = io.open(out, encoding="utf-8").read()

    assert "<title>" in html and "charset" in html
    data = json.loads(html.split('id="run-data">', 1)[1].split("</script>", 1)[0])
    run = data["runs"][0]

    assert run["run"]["n_pdfs"] == 2 and run["run"]["kept"] == 1
    assert "--sources arxiv" in run["run"]["command"]

    kept, rejected = run["papers"]          # kept first, then rejected
    assert kept["decision"] == "keep" and kept["title"] == "A Copper Study"
    assert rejected["decision"] == "reject"
    # a rejected document must show no spend beyond screening
    assert rejected["steps"]["s1"]["calls"] == 0
    assert rejected["steps"]["s0"]["traces"][0][0]["tool"] == "search_pdf"
    # the kept one carries its validation evidence
    assert kept["figures"][0]["verified"]["space_group"]["corrected"] == ["Fm-3m"]


def test_embed_flag_changes_nothing_without_images(run_dir, tmp_path):
    """Both modes work when a run has no crops on disk."""
    linked = report.build_report(str(run_dir), out_path=str(tmp_path / "linked.html"))
    embedded = report.build_report(str(run_dir), out_path=str(tmp_path / "embed.html"), embed=True)
    assert os.path.getsize(linked) > 0 and os.path.getsize(embedded) > 0
