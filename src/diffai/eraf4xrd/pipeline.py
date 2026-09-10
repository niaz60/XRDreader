"""Pipeline orchestrator for ERAF4XRD.

Runs the enabled stages in order -- download -> Phase 0 (screen) -> Phase I
(figures) -> Phase II (metadata) -> JSON clean -> Phase III (verify) ->
digitizer -- based on the RUN_* switches in config. Each stage reads the
previous stage's JSON from the same run folder. Also writes a run manifest,
a consolidated Phase 0 log, per-PDF timing, and the LLM usage report.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from diffai.eraf4xrd.config import (
    MODEL,
    OUT_ROOT,
    OUTPUT_PDF_DIR,
    PHASE1_DIR,
    PHASE1_INPUT_PDF_DIR,
    PROVIDER,
    RUN_DIGITIZER,
    RUN_DOWNLOAD,
    RUN_JSON_CLEAN_AGENT,
    RUN_JSON_VERIFY_AGENT,
    RUN_PHASE0_FILTER,
    RUN_PHASE1,
    RUN_PHASE2,
    USE_ARXIV,
    USE_CROSSREF,
    USE_ELSEVIER,
    USE_SPRINGER,
)
from diffai.eraf4xrd.usage_tracker import UsageTracker, set_tracker
from diffai.eraf4xrd.utils import log


# ---------------------------------------------------------------------------
# Feature 1: Run config snapshot
# ---------------------------------------------------------------------------
def _save_run_manifest():
    """Dump all config values + env state to outputs/run_manifest.json."""
    from diffai.eraf4xrd import config as cfg

    manifest = {
        "timestamp": datetime.now().isoformat(),
        "master_switches": {
            "RUN_DOWNLOAD": cfg.RUN_DOWNLOAD,
            "RUN_PHASE0_FILTER": cfg.RUN_PHASE0_FILTER,
            "RUN_PHASE1": cfg.RUN_PHASE1,
            "RUN_PHASE2": cfg.RUN_PHASE2,
            "RUN_JSON_CLEAN_AGENT": cfg.RUN_JSON_CLEAN_AGENT,
            "RUN_JSON_VERIFY_AGENT": cfg.RUN_JSON_VERIFY_AGENT,
            "RUN_DIGITIZER": cfg.RUN_DIGITIZER,
        },
        "download_sources": {
            "USE_ARXIV": cfg.USE_ARXIV,
            "USE_SPRINGER": cfg.USE_SPRINGER,
            "USE_ELSEVIER": cfg.USE_ELSEVIER,
            "USE_CROSSREF": cfg.USE_CROSSREF,
            "TARGET_DOWNLOADS": cfg.TARGET_DOWNLOADS,
            "REQUIRE_CC_LICENSE": cfg.REQUIRE_CC_LICENSE,
        },
        "search": {
            "ELEMENTS": cfg.ELEMENTS,
            "TECHNIQUE": cfg.TECHNIQUE,
            "SEARCH_KEYWORDS": cfg.SEARCH_KEYWORDS,
        },
        "model": {
            "PROVIDER": cfg.PROVIDER,
            "MODEL": cfg.MODEL,
            "PHASE0_PROVIDER": os.environ.get("PHASE0_PROVIDER", cfg.PROVIDER),
            "PHASE0_MODEL": os.environ.get("PHASE0_MODEL", cfg.MODEL),
            "PHASE1_PROVIDER": os.environ.get("PHASE1_PROVIDER", cfg.PROVIDER),
            "PHASE1_MODEL": os.environ.get("PHASE1_MODEL", cfg.MODEL),
            "PHASE2_PROVIDER": os.environ.get("PHASE2_PROVIDER", cfg.PROVIDER),
            "PHASE2_MODEL": os.environ.get("PHASE2_MODEL", cfg.MODEL),
            "VERIFY_PROVIDER": os.environ.get("VERIFY_PROVIDER", cfg.PROVIDER),
            "VERIFY_MODEL": os.environ.get("VERIFY_MODEL", cfg.MODEL),
        },
        "agentic": {
            "DISABLE_ALL_AGENTS": cfg.DISABLE_ALL_AGENTS,
            "ENABLE_AGENTIC_PHASE0": cfg.ENABLE_AGENTIC_PHASE0,
            "ENABLE_AGENTIC_PHASE1": cfg.ENABLE_AGENTIC_PHASE1,
        },
        "api_keys_present": {
            "OPENAI_API_KEY": bool(cfg.OPENAI_API_KEY),
            "SPRINGER_API_KEY": bool(cfg.SPRINGER_API_KEY),
            "ELSEVIER_API_KEY": bool(cfg.ELSEVIER_API_KEY),
            "UNPAYWALL_EMAIL": bool(cfg.UNPAYWALL_EMAIL),
        },
        "paths": {
            "PDF_DIR": str(cfg.PDF_DIR),
            "OUT_ROOT": str(cfg.OUT_ROOT),
            "PHASE0_KEEP_DIR": str(cfg.PHASE0_KEEP_DIR),
            "PHASE0_REJECT_DIR": str(cfg.PHASE0_REJECT_DIR),
        },
    }
    out_path = cfg.OUT_ROOT / "run_manifest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    log(f"Run manifest saved to {out_path}")


# ---------------------------------------------------------------------------
# Feature 3: Consolidated Phase 0 log
# ---------------------------------------------------------------------------
def _consolidate_phase0():
    """Collect all Phase 0 decisions into one summary JSON."""
    from diffai.eraf4xrd import config as cfg

    results = []
    # read every phase0 decision json from the keep + reject folders
    for folder, decision in [
        (cfg.PHASE0_KEEP_DIR, "KEEP"),
        (cfg.PHASE0_REJECT_DIR, "REJECT"),
    ]:
        if not folder.exists():
            continue
        for p0_file in sorted(folder.glob("*__phase0.json")):
            try:
                data = json.loads(p0_file.read_text(encoding="utf-8"))
                # Errored PDFs are written to the reject folder, so the
                # folder alone cannot tell a rejection from a failure. Trust
                # the recorded decision and fall back to the folder label.
                recorded = (data.get("decision") or {}).get("decision")
                data["_decision"] = (
                    recorded.upper() if recorded else decision
                )
                data["_source_file"] = p0_file.name
                results.append(data)
            except Exception:
                results.append(
                    {
                        "_source_file": p0_file.name,
                        "_decision": decision,
                        "_error": "parse_failed",
                    }
                )

    if results:
        out_path = cfg.OUT_ROOT / "phase0_consolidated.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "timestamp": datetime.now().isoformat(),
                    "total": len(results),
                    "kept": sum(
                        1 for r in results if r["_decision"] == "KEEP"
                    ),
                    "rejected": sum(
                        1 for r in results if r["_decision"] == "REJECT"
                    ),
                    "errors": sum(
                        1 for r in results if r["_decision"] == "ERROR"
                    ),
                    "results": results,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        log(f"Phase 0 consolidated: {len(results)} decisions -> {out_path}")


# ---------------------------------------------------------------------------
# Feature 4: Per-PDF timing summary (appended to usage report)
# ---------------------------------------------------------------------------
def _save_per_pdf_timing(tracker: UsageTracker):
    """Aggregate usage tracker calls by pdf_name and save."""
    from diffai.eraf4xrd import config as cfg

    per_pdf = {}
    # group every tracked LLM call by pdf, then by phase
    for call in tracker.calls:
        pdf = call.get("pdf_name", "") or "unknown"
        if pdf not in per_pdf:
            per_pdf[pdf] = {
                "phases": {},
                "total_cost": 0.0,
                "total_wall": 0.0,
                "total_calls": 0,
            }
        phase = call.get("phase", "unknown")
        if phase not in per_pdf[pdf]["phases"]:
            per_pdf[pdf]["phases"][phase] = {
                "calls": 0,
                "cost": 0.0,
                "wall": 0.0,
                "tokens": 0,
            }
        p = per_pdf[pdf]["phases"][phase]
        p["calls"] += 1
        p["cost"] += call.get("cost_usd", 0)
        p["wall"] += call.get("wall_seconds", 0)
        p["tokens"] += call.get("total_tokens", 0)
        per_pdf[pdf]["total_cost"] += call.get("cost_usd", 0)
        per_pdf[pdf]["total_wall"] += call.get("wall_seconds", 0)
        per_pdf[pdf]["total_calls"] += 1

    if per_pdf:
        out_path = cfg.OUT_ROOT / "per_pdf_timing.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "timestamp": datetime.now().isoformat(),
                    "n_pdfs": len(per_pdf),
                    "per_pdf": per_pdf,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        log(f"Per-PDF timing saved: {len(per_pdf)} PDFs -> {out_path}")


def _env_or_default(name: str, default: str) -> str:
    """Read an env var, or fall back to `default`; return it stripped."""
    value = os.environ.get(name, default)
    return str(value).strip()


def _log_agent_config(
    agent_name: str, provider_env: str = "", model_env: str = ""
):
    """Log which provider/model a stage will use (or 'no LLM used')."""
    if provider_env and model_env:
        provider_used = _env_or_default(provider_env, PROVIDER)
        model_used = _env_or_default(model_env, MODEL)
        log(
            f"{agent_name} config -> provider={provider_used}, model={model_used}"
        )
    else:
        log(f"{agent_name} config -> no LLM used")


def _skip_missing_input(
    step_label: str,
    missing_desc: str,
    needed_glob: str,
    resume_step: str,
    upstream_ran: bool,
):
    """Explain why a later step found no input, and how to fix it.

    A later step reads earlier steps' JSON from the SAME output folder. Since each run
    defaults to a NEW timestamped folder, running e.g. `--steps step3` alone lands in an
    empty folder and silently does nothing. Point `-o` at the prior run's ROOT to resume.
    When `upstream_ran` is True the earlier phase ran this session but produced nothing,
    which is a different (legitimate) situation -- so we don't show the resume hint then.
    """
    log(f"No {missing_desc} found in {PHASE1_DIR}. {step_label}")
    if upstream_ran:
        log(
            "  (An earlier phase ran this session but produced no such output -- nothing to do.)"
        )
    else:
        log(
            "  A later step reads earlier steps' output from the SAME run folder, but each run"
        )
        log(
            "  defaults to a NEW timestamped folder -- so running it on its own starts empty."
        )
        log(
            "  To resume a previous run, point -o at that run's ROOT folder (the one whose"
        )
        log(
            f"  results/ holds {needed_glob}) -- NOT -i, which only feeds PDFs into step0/step1:"
        )
        log(f"     --steps {resume_step} -o eraf4xrd_output\\<DATE>_<TIME>")


def pipeline():
    """Run every enabled stage in order and write the usage report.

    Each `if RUN_X:` block is one stage; a stage is skipped (with a logged
    reason) when its switch is off or its input files are missing.
    """
    # set up the usage tracker as the run-wide singleton
    tracker = UsageTracker()
    set_tracker(tracker)
    any_downloads = False

    # Feature 1: Save run config snapshot
    _save_run_manifest()

    # download open-access PDFs from each enabled source
    if RUN_DOWNLOAD:
        log("Download agents config -> no LLM used")

        if USE_ARXIV:
            from diffai.eraf4xrd.downloaders.arxiv_downloader import (
                download_arxiv_pdfs,
            )

            log("Starting ArXiv download phase...")
            any_downloads |= download_arxiv_pdfs()

        if USE_SPRINGER:
            from diffai.eraf4xrd.downloaders.springer_downloader import (
                download_springer_pdfs,
            )

            log("Starting Springer download phase...")
            any_downloads |= download_springer_pdfs()

        if USE_ELSEVIER:
            from diffai.eraf4xrd.downloaders.elsevier_downloader import (
                download_elsevier_pdfs,
            )

            log("Starting Elsevier download phase...")
            any_downloads |= download_elsevier_pdfs()

        if USE_CROSSREF:
            from diffai.eraf4xrd.downloaders.crossref_downloader import (
                download_crossref_oa_pdfs,
            )

            log("Starting CrossRef + Unpaywall download phase...")
            any_downloads |= download_crossref_oa_pdfs()
    else:
        log("Download phase skipped.")

    # Phase 0: LLM screens each PDF, keeps only the XRD-relevant ones
    if RUN_PHASE0_FILTER:
        pdfs_exist = list(Path(OUTPUT_PDF_DIR).rglob("*.pdf"))
        if pdfs_exist:
            log("Starting Phase 0...")
            _log_agent_config(
                "Phase 0 agent", "PHASE0_PROVIDER", "PHASE0_MODEL"
            )
            from diffai.eraf4xrd import phase_0_download_filter as phase0

            try:
                phase0.main()
            except Exception:
                # Phase 0 raises when it could not screen anything. Write the
                # consolidated summary first so the per-PDF errors are still
                # readable, then let the failure propagate.
                _consolidate_phase0()
                raise
        else:
            log("No PDFs found. Skipping Phase 0.")
    else:
        log("Phase 0 skipped.")

    # Feature 3: Consolidate Phase 0 results
    _consolidate_phase0()

    # Phase 1: detect figures and classify which ones are XRD
    if RUN_PHASE1:
        phase1_pdf_dir = (
            PHASE1_INPUT_PDF_DIR if RUN_PHASE0_FILTER else Path(OUTPUT_PDF_DIR)
        )
        pdfs_exist = list(Path(phase1_pdf_dir).rglob("*.pdf"))
        if pdfs_exist:
            log("Starting Phase 1...")
            _log_agent_config(
                "Phase 1 agent", "PHASE1_PROVIDER", "PHASE1_MODEL"
            )
            from diffai.eraf4xrd import phase_I_classify_genJSON as phase1

            phase1.main()
        else:
            log(f"No PDFs in {phase1_pdf_dir}. Skipping Phase 1.")
    else:
        log("Phase 1 skipped.")

    # Phase 2: extract crystallographic metadata from the text
    if RUN_PHASE2:
        phase1_jsons = list(Path(PHASE1_DIR).glob("*__phase1_raw.json"))
        if phase1_jsons:
            log("Starting Phase 2...")
            _log_agent_config(
                "Phase 2 agent", "PHASE2_PROVIDER", "PHASE2_MODEL"
            )
            from diffai.eraf4xrd import phase_II_enrichJSON as phase2

            phase2.main()
        else:
            _skip_missing_input(
                "Skipping Phase 2.",
                "Phase 1 output (*__phase1_raw.json)",
                "*__phase1_raw.json",
                "step1,step2",
                upstream_ran=RUN_PHASE1,
            )
    else:
        log("Phase 2 skipped.")

    # JSON clean: normalize + dedupe the enriched metadata
    if RUN_JSON_CLEAN_AGENT:
        enriched_jsons = list(Path(PHASE1_DIR).glob("*__phase2_enriched.json"))
        if enriched_jsons:
            log("Starting JSON clean agent...")
            _log_agent_config("JSON clean agent")
            from diffai.eraf4xrd import JSON_cleaner as json_cleaner

            json_cleaner.main()
        else:
            _skip_missing_input(
                "Skipping JSON cleaner.",
                "Phase 2 enriched output (*__phase2_enriched.json)",
                "*__phase2_enriched.json",
                "step2,clean",
                upstream_ran=RUN_PHASE2,
            )
    else:
        log("JSON clean agent skipped.")

    # Phase 3: cross-check the metadata against the PDF text
    if RUN_JSON_VERIFY_AGENT:
        clean_jsons = list(Path(PHASE1_DIR).glob("*__phase2_clean.json"))
        if clean_jsons:
            log("Starting JSON verify agent...")
            _log_agent_config(
                "JSON verify agent", "VERIFY_PROVIDER", "VERIFY_MODEL"
            )
            from diffai.eraf4xrd import (
                phase_III_verifyJSON as phase_III_verifyJSON,
            )

            phase_III_verifyJSON.main()
        else:
            _skip_missing_input(
                "Skipping JSON verifier.",
                "Phase 2 clean output (*__phase2_clean.json)",
                "*__phase2_clean.json",
                "step3",
                upstream_ran=(RUN_PHASE2 or RUN_JSON_CLEAN_AGENT),
            )
    else:
        log("JSON verify agent skipped.")

    # ---- Phase 4: Digitization (manual trigger only) ----
    # the digitizer is optional/deferred -- import it only if present
    digitizer_available = False
    if RUN_DIGITIZER:
        try:
            from diffai.eraf4xrd.config import (
                DIGITIZER_ALGORITHM,
                DIGITIZER_MODEL,
                DIGITIZER_OUTPUT_DIR,
                PALETTE_PATH,
            )
            from diffai.eraf4xrd.digitizer import (
                build_digitized_output,
                digitize_figure,
                discover_xrd_figures,
                find_validated_json_for_figure,
            )

            digitizer_available = True
        except ImportError as e:
            log(f"Digitizer not available (deferred feature): {e}")

    if RUN_DIGITIZER and digitizer_available:

        log("Starting Digitizer...")
        DIGITIZER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # find the XRD figure crops that Phase 1 saved
        figures = discover_xrd_figures(str(PHASE1_DIR))
        if not figures:
            log("No XRD figures found to digitize.")
        else:
            log(f"Found {len(figures)} XRD figures to digitize.")
            # digitize each figure into curve data, then save it
            for fig_info in figures:
                try:
                    log(
                        f"Digitizing: {fig_info['pdf_name']} Fig {fig_info['figure_number']}"
                    )
                    result = digitize_figure(
                        image_path=fig_info["crop_path"],
                        palette_path=str(PALETTE_PATH),
                        algorithm=DIGITIZER_ALGORITHM,
                        output_dir=str(DIGITIZER_OUTPUT_DIR),
                        model=DIGITIZER_MODEL,
                    )

                    validated_path = find_validated_json_for_figure(
                        str(PHASE1_DIR), fig_info["pdf_name"]
                    )

                    stem = Path(fig_info["crop_path"]).stem
                    out_path = str(
                        DIGITIZER_OUTPUT_DIR / f"{stem}__digitized.json"
                    )
                    build_digitized_output(
                        validated_json_path=validated_path,
                        digitized_results=result,
                        figure_number=fig_info["figure_number"],
                        output_path=out_path,
                    )
                    log(
                        f"Digitized {len(result.get('results', []))} curves from Fig {fig_info['figure_number']}"
                    )
                except Exception as e:
                    log(
                        f"[FAIL] Digitizer error for {fig_info['crop_path']}: {repr(e)}"
                    )

    # ---- Usage report ----
    tracker.print_summary()
    # tracker.save_json(str(Path("outputs") / "usage_report.json"))
    tracker.save_json(str(OUT_ROOT / "usage_report.json"))

    # Feature 4: Per-PDF timing breakdown
    _save_per_pdf_timing(tracker)
