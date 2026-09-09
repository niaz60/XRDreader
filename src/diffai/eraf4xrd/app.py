"""Command-line entry point for ERAF4XRD (`diffai-eraf4xrd`).

Parses CLI flags, translates them into the RUN_* / USE_* env-var switches that
config.py reads, then imports and runs the pipeline. Flags must be applied
before config is imported, since config reads the environment at import time.
"""

import argparse
import os
from datetime import datetime

# The six step switches config.py reads, in pipeline order.
STEP_ENVS = (
    "RUN_DOWNLOAD",
    "RUN_PHASE0_FILTER",
    "RUN_PHASE1",
    "RUN_PHASE2",
    "RUN_JSON_CLEAN_AGENT",
    "RUN_JSON_VERIFY_AGENT",
)
# --steps aliases -> env switch
STEP_ALIASES = {
    "download": "RUN_DOWNLOAD",
    "step0": "RUN_PHASE0_FILTER",
    "phase0": "RUN_PHASE0_FILTER",
    "step1": "RUN_PHASE1",
    "phase1": "RUN_PHASE1",
    "step2": "RUN_PHASE2",
    "phase2": "RUN_PHASE2",
    "clean": "RUN_JSON_CLEAN_AGENT",
    "step3": "RUN_JSON_VERIFY_AGENT",
    "phase3": "RUN_JSON_VERIFY_AGENT",
}
# --sources names -> env flag
SOURCE_ENVS = {
    "arxiv": "USE_ARXIV",
    "springer": "USE_SPRINGER",
    "elsevier": "USE_ELSEVIER",
    "crossref": "USE_CROSSREF",
}


def _build_parser():
    """Build the argparse parser for the diffai-eraf4xrd CLI."""
    ap = argparse.ArgumentParser(
        prog="diffai-eraf4xrd",
        description="Run the ERAF4XRD framework from the command line. With no flags it "
        "uses the switches from config.py / the environment (default: download + Step 0 "
        "only). ANY config setting can be overridden with --set NAME=VALUE.",
    )

    g = ap.add_argument_group("which steps to run")
    g.add_argument(
        "--full-run",
        action="store_true",
        help="Run every step: download -> Step 0 -> I -> II -> JSON clean -> Step III.",
    )
    g.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading; use PDFs already in the output folder.",
    )
    g.add_argument(
        "--skip-screening",
        action="store_true",
        help="Skip Step 0 screening; keep EVERY downloaded PDF and pass them all "
        "to the later steps (sets RUN_PHASE0_FILTER=false).",
    )
    g.add_argument(
        "--steps",
        metavar="LIST",
        help="Comma-separated steps to run (turns these ON, the rest OFF): "
        "download,step0,step1,step2,clean,step3.",
    )

    d = ap.add_argument_group("download & search")
    d.add_argument(
        "-i",
        "--input-dir",
        metavar="DIR",
        help="Run on the PDFs already in DIR (points the input folder there "
        "and skips downloading). Easiest way to process a specific folder.",
    )
    d.add_argument(
        "-n",
        "--downloads",
        type=int,
        metavar="N",
        help="PDFs to download per enabled source (TARGET_DOWNLOADS).",
    )
    d.add_argument(
        "--elements", metavar="STR", help="Material search terms (ELEMENTS)."
    )
    d.add_argument(
        "--technique", metavar="STR", help="Technique keywords (TECHNIQUE)."
    )
    d.add_argument(
        "--sources",
        metavar="LIST",
        help="Sources to enable (enables these, disables the rest): "
        "arxiv,springer,elsevier,crossref.",
    )
    d.add_argument(
        "--cc-only",
        action="store_true",
        help="Keep only Creative-Commons-licensed downloads (REQUIRE_CC_LICENSE).",
    )

    m = ap.add_argument_group("LLM")
    m.add_argument(
        "--provider",
        choices=["gpt", "gemini", "claude", "grok", "together"],
        help="LLM provider (PROVIDER). 'together' = open-source models "
        "(Llama/Qwen/DeepSeek) via Together AI; needs TOGETHER_API_KEY.",
    )
    m.add_argument("--model", metavar="NAME", help="Model name (MODEL).")
    m.add_argument(
        "--single-pass",
        action="store_true",
        help="Disable agentic loops; one LLM call per step (DISABLE_ALL_AGENTS).",
    )

    ap.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Set ANY config env var directly; repeatable and applied last. "
        "e.g. --set PHASE1_MODEL=gpt-4o --set VERIFY_PROVIDER=claude",
    )
    ap.add_argument(
        "-o",
        "--output-dir",
        metavar="DIR",
        help="Write all outputs under DIR (documents/, results/, logs/). "
        "Default: a timestamped folder ./eraf4xrd_output/<date>_<time>/.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved run configuration and exit WITHOUT running "
        "(no API calls, no downloads). Use it to check a command.",
    )
    return ap


def _apply_cli_flags(argv=None):
    """Translate CLI flags into the env-var switches config.py reads.

    MUST run before config/pipeline are imported, because config.py reads all settings
    from the environment at import time. No flags => environment untouched => default
    behavior (download + Step 0) is unchanged.
    """
    ap = _build_parser()
    args = ap.parse_args(argv)

    # ---- steps ----
    if args.full_run:
        for v in STEP_ENVS:
            os.environ[v] = "true"
    if args.steps:
        wanted = {
            s.strip().lower() for s in args.steps.split(",") if s.strip()
        }
        unknown = sorted(w for w in wanted if w not in STEP_ALIASES)
        if unknown:
            ap.error(f"--steps: unknown step(s): {', '.join(unknown)}")
        on = {STEP_ALIASES[w] for w in wanted}
        for v in STEP_ENVS:
            os.environ[v] = "true" if v in on else "false"
    if args.skip_download:
        os.environ["RUN_DOWNLOAD"] = "false"
    if args.skip_screening:
        os.environ["RUN_PHASE0_FILTER"] = "false"

    # ---- input dir: run on your own folder of PDFs (implies no download) ----
    if args.input_dir is not None:
        os.environ["OUTPUT_PDF_DIR"] = args.input_dir
        os.environ["RUN_DOWNLOAD"] = "false"

    # ---- output dir: single root for all outputs (default = timestamped) ----
    if args.output_dir is not None:
        os.environ["ERAF4XRD_OUTPUT_DIR"] = args.output_dir

    # ---- download & search ----
    if args.downloads is not None:
        os.environ["TARGET_DOWNLOADS"] = str(args.downloads)
    if args.elements is not None:
        os.environ["ELEMENTS"] = args.elements
    if args.technique is not None:
        os.environ["TECHNIQUE"] = args.technique
    if args.sources:
        want = {
            s.strip().lower() for s in args.sources.split(",") if s.strip()
        }
        unknown = sorted(s for s in want if s not in SOURCE_ENVS)
        if unknown:
            ap.error(f"--sources: unknown source(s): {', '.join(unknown)}")
        for name, env in SOURCE_ENVS.items():
            os.environ[env] = "1" if name in want else "0"
    if args.cc_only:
        os.environ["REQUIRE_CC_LICENSE"] = "true"

    # ---- LLM ----
    if args.provider is not None:
        os.environ["PROVIDER"] = args.provider
    if args.model is not None:
        os.environ["MODEL"] = args.model
    if args.single_pass:
        os.environ["DISABLE_ALL_AGENTS"] = "true"

    # ---- generic escape hatch (applied last so it overrides anything above) ----
    for item in args.overrides:
        if "=" not in item:
            ap.error(f"--set expects KEY=VALUE, got: {item!r}")
        key, val = item.split("=", 1)
        os.environ[key.strip()] = val

    return args


def _print_dry_run():
    """Print the resolved configuration (flags + env + config.py defaults) and exit.

    Imports config (stdlib-only) AFTER the flags have set the environment, so this
    reflects exactly what a real run would use. Runs nothing.
    """
    import os

    from diffai.eraf4xrd import config as c

    steps = [
        ("Download", c.RUN_DOWNLOAD),
        ("Step 0", c.RUN_PHASE0_FILTER),
        ("Step I", c.RUN_PHASE1),
        ("Step II", c.RUN_PHASE2),
        ("JSON clean", c.RUN_JSON_CLEAN_AGENT),
        ("Step III", c.RUN_JSON_VERIFY_AGENT),
    ]
    on = [n for n, v in steps if v]
    print("DRY RUN -- nothing will execute. Resolved configuration:")
    print("  Output   :", c.OUTPUT_ROOT)
    print("  Steps ON :", " -> ".join(on) if on else "(none)")
    print("  Steps OFF:", ", ".join(n for n, v in steps if not v) or "(none)")
    if c.RUN_DOWNLOAD:
        srcs = [
            name
            for name, flag in (
                ("arxiv", c.USE_ARXIV),
                ("springer", c.USE_SPRINGER),
                ("elsevier", c.USE_ELSEVIER),
                ("crossref", c.USE_CROSSREF),
            )
            if flag
        ]
        print(
            f"  Download : {c.TARGET_DOWNLOADS} per source | sources: {', '.join(srcs) or '(NONE -- nothing to download!)'}"
        )
        print(f"  Search   : ELEMENTS={c.ELEMENTS!r}")
    else:
        print(f"  Input    : {c.PDF_DIR}  (download skipped)")
    mode = (
        "single-pass" if getattr(c, "DISABLE_ALL_AGENTS", False) else "agentic"
    )
    print(f"  LLM      : provider={c.PROVIDER}  model={c.MODEL}  [{mode}]")
    extras = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("PHASE0_", "PHASE1_", "PHASE2_", "VERIFY_"))
        and (k.endswith("_PROVIDER") or k.endswith("_MODEL"))
    }
    if extras:
        print(
            "  Overrides:",
            ", ".join(f"{k}={v}" for k, v in sorted(extras.items())),
        )
    if getattr(c, "REQUIRE_CC_LICENSE", False):
        print("  License  : CC-only")


def run():
    """Set up the run's log file, then execute the pipeline."""
    # Imported here (not at module top) so _apply_cli_flags() sets env vars first --
    # config.py reads every setting from the environment at import time.
    from pathlib import Path

    from diffai.eraf4xrd.config import OUTPUT_TXT_DIR
    from diffai.eraf4xrd.pipeline import pipeline
    from diffai.eraf4xrd.utils import ensure_dir, log, set_log_file

    ensure_dir(OUTPUT_TXT_DIR)
    log_path = (
        Path(OUTPUT_TXT_DIR)
        / f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )

    with open(log_path, "a", encoding="utf-8") as fh:
        set_log_file(fh)
        log(f"Logging all output to {log_path}")
        try:
            pipeline()
            log("Pipeline completed.")
        except Exception as e:
            log(f"Pipeline failed: {e}")
            raise


def main():
    """Console entry point (diffai-eraf4xrd): parse flags, then run."""
    args = _apply_cli_flags()
    if args.dry_run:
        _print_dry_run()
    else:
        run()


if __name__ == "__main__":
    main()
