"""Central configuration for ERAF4XRD.

Every setting is read from an environment variable (with a sensible default)
at import time, so CLI flags / env vars must be set BEFORE this module is
imported. Grouped into sections: master switches, download sources + API keys,
search parameters, output paths, and per-phase knobs.
"""

import os
from pathlib import Path


# reads an env var as True/False (or the default)
def get_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


# reads an env var as an int (or the default)
def get_int(name, default):
    val = os.environ.get(name)
    if val is None or str(val).strip() == "":
        return default
    text = str(val).strip().lower()
    # The source switches (USE_ARXIV and friends) are ints, but they read as
    # on/off, so people write --set USE_SPRINGER=true. Accept the same words
    # get_bool does rather than silently falling back to the default, which
    # left the source disabled with no warning.
    if text in {"1", "true", "yes", "on"}:
        return 1
    if text in {"0", "false", "no", "off"}:
        return 0
    try:
        return int(float(text))
    except (ValueError, TypeError):
        raise SystemExit(
            f"{name}={val!r} is not a number or a true/false value."
        )


# ======================================================================================
# MASTER SWITCHES
# ======================================================================================
RUN_DOWNLOAD = get_bool("RUN_DOWNLOAD", True)
RUN_PHASE0_FILTER = get_bool("RUN_PHASE0_FILTER", True)
RUN_PHASE1 = get_bool("RUN_PHASE1", False)
RUN_PHASE2 = get_bool("RUN_PHASE2", False)
RUN_JSON_CLEAN_AGENT = get_bool("RUN_JSON_CLEAN_AGENT", False)  # JSON Cleaning
RUN_JSON_VERIFY_AGENT = get_bool(
    "RUN_JSON_VERIFY_AGENT", False
)  # Phase III <-- need to update the name

# ======================================================================================
# DOWNLOAD SOURCE SWITCHES
# ======================================================================================
# 1 = use this source, 0 = skip
USE_ARXIV = get_int("USE_ARXIV", 1)
USE_SPRINGER = get_int("USE_SPRINGER", 0)
USE_ELSEVIER = get_int("USE_ELSEVIER", 0)
USE_CROSSREF = get_int("USE_CROSSREF", 0)

# ======================================================================================
# API KEYS
# ======================================================================================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEM_API_KEY = os.environ.get("GEMINI_API_KEY")
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY")
SPRINGER_API_KEY = os.environ.get("SPRINGER_API_KEY")
ELSEVIER_API_KEY = os.environ.get("ELSEVIER_API_KEY")
# UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "your_real_email@domain.com")
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "")

# ======================================================================================
# SEARCH PARAMETERS
# ======================================================================================
ELEMENTS = os.environ.get("ELEMENTS", "Cu OR Copper")
TECHNIQUE = os.environ.get(
    "TECHNIQUE",
    '"x-ray diffraction" OR "x ray diffraction" OR XRD OR PXRD OR '
    '"powder x-ray diffraction" OR "powder diffraction"',
)
SEARCH_KEYWORDS = os.environ.get(
    "SEARCH_KEYWORDS", f"(({ELEMENTS}) AND ({TECHNIQUE}))"
)

# ======================================================================================
# COMMON PATHS
# ======================================================================================
BASE_DIR = (
    Path(__file__).resolve().parent
)  # installed package dir (bundled data)
# Writable outputs default to the user's working directory, NOT the installed
# package location. Override the base with ERAF4XRD_WORKDIR.
WORK_DIR = Path(os.environ.get("ERAF4XRD_WORKDIR", ".")).resolve()
# Single output root. Default = a timestamped folder so each run is self-contained
# and never clobbers a previous one; override with --output-dir / ERAF4XRD_OUTPUT_DIR.
from datetime import datetime as _dt  # noqa: E402

OUTPUT_ROOT = Path(
    os.environ.get(
        "ERAF4XRD_OUTPUT_DIR",
        str(
            WORK_DIR
            / "eraf4xrd_output"
            / _dt.now().strftime("%Y-%m-%d_%H%M%S")
        ),
    )
)
PDF_DIR = Path(
    os.environ.get("OUTPUT_PDF_DIR", str(OUTPUT_ROOT / "documents"))
)
OUT_ROOT = Path(os.environ.get("PHASE1_DIR", str(OUTPUT_ROOT / "results")))
PHASE1_DIR = OUT_ROOT
PHASE0_KEEP_DIR = Path(
    os.environ.get("PHASE0_KEPT_DIR", str(OUT_ROOT / "phase0_kept_pdfs"))
)
PHASE0_REJECT_DIR = Path(
    os.environ.get(
        "PHASE0_REJECTED_DIR", str(OUT_ROOT / "phase0_rejected_pdfs")
    )
)
PHASE1_INPUT_PDF_DIR = Path(
    os.environ.get("PHASE1_INPUT_PDF_DIR", str(PHASE0_KEEP_DIR))
)  # Phase I reads only the pdf that are 'kept' by Phase 0
OUTPUT_PDF_DIR = str(PDF_DIR)
OUTPUT_TXT_DIR = str(OUTPUT_ROOT / "logs")
OUTPUT_XRD_DIR = "XRD_figs"

# ======================================================================================
# DOWNLOAD SETTINGS
# ======================================================================================
REQUIRE_CC_LICENSE = get_bool("REQUIRE_CC_LICENSE", False)  # change to True/1
TARGET_DOWNLOADS = get_int("TARGET_DOWNLOADS", 2)
BATCH = 25  # results per API page
SLEEP = 3  # seconds between downloads
PROCESS_SOURCE = "ArXiv"
ANY_JOURNAL = True  # False = only the SPRINGER_JOURNALS below
SPRINGER_JOURNALS = [
    "Scientific Reports",
    "Nature Communications",
    "Nature Materials",
    "Nature Nanotechnology",
    "Nature",
    "Communications Materials",
]

# ======================================================================================
# AI PROVIDER SETTINGS
# ======================================================================================
PROVIDER = os.environ.get("PROVIDER", "gpt")
MODEL = os.environ.get("MODEL", "gpt-5.2")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", MODEL)
THINKING_BUDGET = 512
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.0"))

# ======================================================================================
# AGENTIC MODE — master kill switch
# ======================================================================================
DISABLE_ALL_AGENTS = get_bool("DISABLE_ALL_AGENTS", False)

# ======================================================================================
# PHASE 0 KNOBS
# ======================================================================================
ENABLE_AGENTIC_PHASE0 = get_bool("ENABLE_AGENTIC_PHASE0", True)

# ======================================================================================
# PHASE 1 KNOBS
# ======================================================================================
DPI_CROP = 300  # dpi for cropped figure images
DPI_PAGE = 160  # dpi for full-page renders
GRID = 240
MIN_COMPONENT_AREA = 1200  # minimum vector component size; filters out tiny vector marks, ticks, symbols, or noise.
MERGE_PAD_PX = 12  # padding added around detected vector regions before making a bounding box.
MAX_VEC_PAGE_FRAC = 0.70  # max vector region (fraction of page)
MERGE_IOU_THR = 0.02  # overlap needed to merge two boxes
MIN_CROP_AREA_FRAC = 0.01  # smallest crop (fraction of page)
MAX_CROP_AREA_FRAC = 0.90  # largest crop (fraction of page)
MAX_API_RETRIES = 4
BASE_SLEEP_SEC = 1.0
KEEP_ONLY_MATCHED_AND_XRD = True
SAVE_DEBUG_HIGHLIGHT = True  # need to turn it off
ENABLE_AGENTIC_PHASE1 = True
PHASE1_USE_CALL_CACHE = True
PHASE1_SKIP_TEXTLESS_VECTOR_CANDIDATES = False
PHASE1_MIN_RENDERED_CROP_BYTES = 1200
PHASE1_MIN_AGENT_SCORE_TO_CALL = 0.20
SKIP_SIMULATED_ONLY_XRD = get_bool("SKIP_SIMULATED_ONLY_XRD", True)

# ======================================================================================
# PHASE 2 KNOBS
# ======================================================================================
ENABLE_AGENTIC_PHASE2 = True
PHASE2_USE_VERIFY_CACHE = True
PHASE2_MIN_GLOBAL_SIGNALS_TO_VERIFY = 1
PHASE2_MIN_FIGURE_SIGNALS_TO_VERIFY = 1
PHASE2_FORCE_VERIFY_IF_GLOBAL_EMPTY = False
NEARBY_PARAS = 3  # paragraphs near a figure to read
NEARBY_MAX_CHARS = 2500  # cap on that nearby text
FIG_WINDOW_SENTENCES = 10  # sentences of context around a figure
FIG_WINDOW_MAX_CHARS = 6000  # cap on that window
USE_LLM_VERIFY_GLOBAL = True
USE_LLM_VERIFY_FIGS = True
CAPTION_MAX_CHARS = 700  # cap on caption length
CAPTION_BLOCK_GAP_PX = 18
CAPTION_FOOTER_Y_FRAC = 0.86
CAPTION_MIN_START_CONF = 1
NEARBY_BLOCKS_AFTER_CAPTION = 4
NEARBY_MAX_CHARS_BLOCKS = 2500

# ======================================================================================
# PHASE 3 KNOBS
# ======================================================================================
ENABLE_AGENTIC_PHASE3 = get_bool("ENABLE_AGENTIC_PHASE3", True)

# ======================================================================================
# JSON CLEAN / VERIFY AGENTS
# ======================================================================================
CLEAN_JSON_PREFIX = "SUPERCLEAN_"
VERIFY_JSON_FINAL_SUFFIX = "__verified_final.json"
VERIFY_JSON_LOG_SUFFIX = "__verified_log.json"
USE_XRD_FOCUSED_TEXT_VERIFY = True
MAX_GLOBAL_CONTEXT_CHARS_VERIFY = 12000
MAX_FIGURE_CONTEXT_CHARS_VERIFY = 8000

# ======================================================================================
# DIGITIZER SETTINGS
# ======================================================================================
RUN_DIGITIZER = get_bool("RUN_DIGITIZER", False)
DIGITIZER_ALGORITHM = (
    os.environ.get("DIGITIZER_ALGORITHM", "topmost").strip().lower()
)
DIGITIZER_MODEL = os.environ.get(
    "DIGITIZER_MODEL", os.environ.get("MODEL", "gpt-4o")
).strip()
PALETTE_PATH = BASE_DIR / "palette.json"
DIGITIZER_OUTPUT_DIR = OUT_ROOT / "digitized"

# ======================================================================================
# CONFIDENCE CALIBRATION (Improvement 7)
# ======================================================================================
# When enabled, pipeline logs (confidence, outcome) pairs for post-hoc calibration analysis.
# Pairs are saved in outputs/confidence_log.jsonl — each line is one prediction.
ENABLE_CONFIDENCE_LOGGING = get_bool("ENABLE_CONFIDENCE_LOGGING", True)
CONFIDENCE_LOG_PATH = OUT_ROOT / "confidence_log.jsonl"

# ======================================================================================
# MASTER OVERRIDE — disable all agents in one shot
# ======================================================================================
if DISABLE_ALL_AGENTS:
    ENABLE_AGENTIC_PHASE0 = False
    ENABLE_AGENTIC_PHASE1 = False
    ENABLE_AGENTIC_PHASE2 = False
    ENABLE_AGENTIC_PHASE3 = False
