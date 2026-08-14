"""
PHASE 2 (v3) — Batch enrich with:
(1) Figure-level context window + LLM verification of regex candidate fields (per figure)
(2) Global regex + LLM verification (per PDF)

Outputs:
- <pdf_stem>__phase1_enriched.json next to each __phase1.json

Notes:
- Does NOT overwrite Phase-1 fig["plot_info"].
- Writes verified fields to:
  fig["phase2_xrd_text"]["regex_candidate"]
  fig["phase2_xrd_text"]["verified"]
  and global to phase1["global_xrd_text"][...]
"""

import hashlib
import json
import os
import random
import re
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF (used ONLY for robust caption/window text)
import pdfplumber

# ===================== EDIT HERE =====================
from diffai.xrdreader.config import (
    CAPTION_BLOCK_GAP_PX,
    CAPTION_FOOTER_Y_FRAC,
    CAPTION_MAX_CHARS,
    CAPTION_MIN_START_CONF,
    ENABLE_AGENTIC_PHASE2,
    FIG_WINDOW_MAX_CHARS,
    FIG_WINDOW_SENTENCES,
    MODEL,
    NEARBY_BLOCKS_AFTER_CAPTION,
    NEARBY_MAX_CHARS_BLOCKS,
    OPENAI_MODEL,
    PDF_DIR,
    PHASE1_DIR,
    PHASE2_FORCE_VERIFY_IF_GLOBAL_EMPTY,
    PHASE2_MIN_FIGURE_SIGNALS_TO_VERIFY,
    PHASE2_MIN_GLOBAL_SIGNALS_TO_VERIFY,
    PHASE2_USE_VERIFY_CACHE,
    PROVIDER,
    USE_LLM_VERIFY_FIGS,
    USE_LLM_VERIFY_GLOBAL,
)
from diffai.xrdreader.utils import make_safe_stem

# =================== /EDIT HERE ======================


PHASE2_PROVIDER = (
    os.environ.get("PHASE2_PROVIDER", os.environ.get("PROVIDER", PROVIDER))
    .strip()
    .lower()
)
PHASE2_MODEL = (
    os.environ.get(
        "PHASE2_MODEL",
        os.environ.get(
            "MODEL",
            os.environ.get(
                "OPENAI_MODEL", MODEL if "MODEL" in globals() else OPENAI_MODEL
            ),
        ),
    ).strip()
    or os.environ.get(
        "MODEL", os.environ.get("OPENAI_MODEL", OPENAI_MODEL)
    ).strip()
)

# Agent loop knobs (env-overridable)
PHASE2_AGENT_MAX_STEPS = int(os.getenv("PHASE2_AGENT_MAX_STEPS", "6"))
PHASE2_AGENT_MAX_RETRIES = int(os.getenv("PHASE2_AGENT_MAX_RETRIES", "3"))


FIG_CAP_RE = re.compile(r"(fig\.|figure)\s*(\d+)\s*[:.\-]?\s*", re.IGNORECASE)

# GLOBAL regexes (conservative)
RE_2THETA_RANGE = re.compile(
    r"(?:2θ|2theta|two\s*theta)\s*(?:=|from)?\s*(\d+(?:\.\d+)?)\s*(?:°|deg)?\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:°|deg)?",
    re.IGNORECASE,
)
RE_STEP = re.compile(
    r"(?:step\s*(?:size|width)?|increment)\s*(?:=|:)?\s*(\d+(?:\.\d+)?)\s*(?:°|deg)",
    re.IGNORECASE,
)
RE_RAD = re.compile(
    r"\b(Cu|Mo|Co|Fe)\s*[- ]?\s*K\s*α\b|\b(Cu|Mo|Co|Fe)\s*[- ]?\s*K\s*a\b",
    re.IGNORECASE,
)
RE_WAVELENGTH = re.compile(
    r"(?:λ|lambda)\s*(?:=|:)?\s*(\d+(?:\.\d+)?)\s*(?:Å|A)\b", re.IGNORECASE
)
RE_SOFTWARE = re.compile(
    r"\b(FullProf(?:\s*Suite)?|GSAS-?II|TOPAS(?:-Academic)?|MAUD|JANA2006|HighScore(?:\s*Plus)?)\b",
    re.IGNORECASE,
)
RE_TEMP_ANY = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:°C|C)\b", re.IGNORECASE)

RE_CRYSTAL_STRUCTURE = re.compile(
    r"\b(fcc|bcc|hcp|cubic|tetragonal|orthorhombic|monoclinic|triclinic|trigonal|rhombohedral|hexagonal)\b",
    re.IGNORECASE,
)
RE_SPACE_GROUP = re.compile(
    r"\b(?:space\s*group\s*[:=]?\s*)?([PIFRCA]\s*[0-9]{1,3}(?:/[a-zA-Z0-9\-]+)?)\b",
    re.IGNORECASE,
)

# Lattice parameters  (a = 5.43 Å, b = ..., c = ...)
RE_LATTICE_PARAM = re.compile(
    r"\b([abc])\s*=\s*(\d+(?:\.\d+)?)\s*(?:Å|A|angstrom)\b",
    re.IGNORECASE,
)
RE_LATTICE_ANGLE = re.compile(
    r"\b(α|β|γ|alpha|beta|gamma)\s*=\s*(\d+(?:\.\d+)?)\s*(?:°|deg)\b",
    re.IGNORECASE,
)

# Reported phases / compounds (ICDD/JCPDS references + common mineral/phase names)
RE_PHASE_ICDD = re.compile(
    r"\b(?:ICDD|JCPDS|PDF)[- #]*(\d{2}[-–]\d{3,4})\b",
    re.IGNORECASE,
)
RE_PHASE_NAME = re.compile(
    r"\b(anatase|rutile|brookite|calcite|aragonite|vaterite|quartz|corundum"
    r"|hematite|magnetite|goethite|wustite|ferrite|perovskite|spinel"
    r"|wurtzite|zincblende|halite|fluorite|pyrite|austenite|martensite"
    r"|bainite|cementite|graphite|diamond|α-Fe2O3|γ-Fe2O3|α-Al2O3"
    r"|γ-Al2O3|β-SiC|α-SiC|α-phase|β-phase|γ-phase)\b",
    re.IGNORECASE,
)

# Profile function type
RE_PROFILE_FUNCTION = re.compile(
    r"\b(pseudo[- ]?Voigt|Voigt|Gaussian|Lorentzian|Pearson\s*VII|Thompson[- ]Cox[- ]Hastings|TCH|split[- ]?Pearson)\b",
    re.IGNORECASE,
)

# ---- CIF-comparison metadata (new) ----

# Z = formula units per unit cell (e.g. "Z = 4", "Z=8", "Z = 2")
RE_Z_FORMULA_UNITS = re.compile(
    r"\bZ\s*=\s*(\d{1,3})\b",
)

# Chemical formula — capture stoichiometric formulas like Np2O5, NiFe2O4, Ca2Fe2O5
RE_CHEMICAL_FORMULA = re.compile(
    r"\b([A-Z][a-z]?(?:\d*(?:\.\d+)?)"  # first element + optional count
    r"(?:[A-Z][a-z]?(?:\d*(?:\.\d+)?)){1,8})\b",  # 1-8 more element+count groups
)

# Database reference numbers — ICSD, COD, Materials Project (mp-XXXX)
RE_ICSD_REF = re.compile(
    r"\bICSD\s*[#:\-]?\s*(\d{4,7})\b",
    re.IGNORECASE,
)
RE_COD_REF = re.compile(
    r"\bCOD\s*[#:\-]?\s*(\d{5,7})\b",
    re.IGNORECASE,
)
RE_MP_REF = re.compile(
    r"\b(mp-\d{1,7})\b",
    re.IGNORECASE,
)

# X-ray density (e.g. "X-ray density = 5.36 g/cm³", "ρ_x = 5.37", "dx = 5.37 g cm-3")
RE_XRAY_DENSITY = re.compile(
    r"(?:x[- ]?ray\s+density|ρ_?x|d_?x)\s*(?:=|:)\s*(\d+(?:\.\d+)?)\s*(?:g\s*/?\s*cm|Mg\s*/?\s*m)",
    re.IGNORECASE,
)

# Unit cell volume (e.g. "V = 588.97 Å³", "V = 588.97(8) Å3", "cell volume = 312.4 Å³")
RE_CELL_VOLUME = re.compile(
    r"\b(?:V|V_?0|cell\s*volume|unit\s*cell\s*volume)\s*(?:=|:)\s*"
    r"(\d+(?:\.\d+)?)\s*(?:\(\d+\))?\s*(?:Å3|Å³|A3|angstrom3|angstroms3|nm3)",
    re.IGNORECASE,
)

# Space group number (e.g. "space group No. 62", "Pnma (62)", "#62", "SG 227")
RE_SPACE_GROUP_NUMBER = re.compile(
    r"(?:"
    r"(?:space\s*group|SG)\s*(?:No\.?|number|#)\s*(\d{1,3})"
    r"|"
    r"(?:space\s*group\s*[:=]?\s*\S+)\s*\((\d{1,3})\)"
    r")",
    re.IGNORECASE,
)

# Chemical formula weight / molecular weight (e.g. "M = 245.67 g/mol", "molecular weight = 159.69")
RE_FORMULA_WEIGHT = re.compile(
    r"(?:M(?:_?w)?|molecular\s*weight|formula\s*weight|molar\s*mass)\s*(?:=|:)\s*"
    r"(\d+(?:\.\d+)?)\s*(?:g\s*/?\s*mol|g\s*mol|amu)?",
    re.IGNORECASE,
)

# Wyckoff positions (e.g. "4a site", "8c position", "Wyckoff 4e")
# Two-branch pattern to reduce false positives:
#   Branch 1: "Wyckoff" keyword present → accept any multiplicity + letter
#   Branch 2: No keyword → require trailing "site"/"position" AND restrict
#             letter to a–j (valid Wyckoff letters) AND multiplicity ≤ 192
RE_WYCKOFF = re.compile(
    r"(?:"
    r"Wyckoff\s+(?:position|site|symbol|notation|letter)s?\s*[:=]?\s*(\d{1,3}[a-zA-Z])"
    r"|"
    r"\b(\d{1,3}[a-j])\s+(?:site|position)s?\b"
    r")",
    re.IGNORECASE,
)

# Enhanced lattice parameter with uncertainty: "a = 8.168(2) Å"
RE_LATTICE_PARAM_WITH_ERR = re.compile(
    r"\b([abc])\s*=\s*(\d+(?:\.\d+)?)\s*(?:\((\d+)\))?\s*(?:Å|A|angstrom)\b",
    re.IGNORECASE,
)
RE_LATTICE_ANGLE_WITH_ERR = re.compile(
    r"\b(α|β|γ|alpha|beta|gamma)\s*=\s*(\d+(?:\.\d+)?)\s*(?:\((\d+)\))?\s*(?:°|deg)\b",
    re.IGNORECASE,
)

# Miller indices from XRD patterns (e.g. "(111)", "(220)", "(311)")
# Each index is a single digit 0–9 (covers all practical hkl values).
# Negative-lookbehind excludes years/citations: "(1993)", "(2024)" etc.
# Negative-lookahead excludes trailing digits that would form a 4+-digit number.
RE_MILLER_INDICES = re.compile(
    r"(?<!\d)"  # not preceded by a digit (avoid "1(234)")
    r"\(\s*(\d)\s+(\d)\s+(\d)\s*\)"  # spaced form: (1 1 1), (2 2 0)
    r"|"
    r"(?<!\d)"  # not preceded by a digit
    r"\(([0-9])([0-9])([0-9])\)"  # compact form: (111), (220)
    r"(?!\d)",  # not followed by a digit (exclude "(1993)")
)

# Improvement 8: Structured peak metadata — extract reported 2θ peak positions & FWHM
RE_PEAK_POSITION = re.compile(
    r"(?:2θ|2theta|peak)\s*(?:=|at|:)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:°|deg)",
    re.IGNORECASE,
)
RE_FWHM = re.compile(
    r"(?:FWHM|full\s*width\s*at\s*half\s*max(?:imum)?)\s*(?:=|:)?\s*(\d+(?:\.\d+)?)\s*(?:°|deg)?",
    re.IGNORECASE,
)

# --- Improvement 5: Appendix / Supporting-Information detection ---
RE_SI_HEADING = re.compile(
    r"(?:^|\n)\s*(?:S(?:upporting|upplementary)\s+(?:Information|Materials?|Data|Figures?|Tables?)"
    r"|(?:Appendix|ESI|SI)\b"
    r"|S\d+\.\s)"  # e.g. "S1. XRD patterns"
    r"[^\n]{0,120}",
    re.IGNORECASE,
)
RE_SI_FIG_REF = re.compile(
    r"\b(?:(?:Fig(?:ure)?|Table)\s*S\d+"  # "Figure S1", "Table S2"
    r"|(?:S(?:upplementary|upporting)\s+(?:Fig|Table))\s*\d+)",  # "Supplementary Fig 3"
    re.IGNORECASE,
)


def detect_si_pages(pages_text: list) -> dict:
    """Detect pages likely belonging to Supporting Information / Appendix."""
    si_pages = []
    si_fig_refs = []
    for i, page in enumerate(pages_text, 1):
        if RE_SI_HEADING.search(page):
            si_pages.append(i)
        for m in RE_SI_FIG_REF.finditer(page):
            si_fig_refs.append({"page": i, "ref": m.group(0).strip()})
    return {
        "si_pages": si_pages,
        "si_figure_refs": si_fig_refs[:20],  # cap to avoid bloat
        "has_si_section": len(si_pages) > 0,
    }


# Sentence splitter (simple, works well enough for science PDFs)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# ---------------- LLM prompt (evidence-gated) ----------------
VERIFY_PROMPT = (
    "Return ONLY valid JSON. No markdown.\n\n"
    "You are given:\n"
    "1) CONTEXT: a text window (sentences) from the paper\n"
    "2) CANDIDATE: a JSON dict proposed by regex\n\n"
    "TASK (strict):\n"
    "- For each field in CANDIDATE, keep it ONLY if CONTEXT explicitly supports it.\n"
    '- If not supported, set it to "" (or [] for lists).\n'
    "- Do NOT guess.\n"
    "- For every non-empty field you keep, provide an evidence_span that is an exact substring copied from CONTEXT.\n"
    "- If you cannot provide evidence_span, the field must be blank.\n\n"
    "Return JSON exactly with keys:\n"
    "{\n"
    '  "scan_range_2theta": {"value":"", "evidence_span":""},\n'
    '  "step_size": {"value":"", "evidence_span":""},\n'
    '  "radiation": {"value":"", "evidence_span":""},\n'
    '  "software": {"value":[], "evidence_span":""},\n'
    '  "context_temperature": {"value":[], "evidence_span":""},\n'
    '  "crystal_structure": {"value":[], "evidence_span":""},\n'
    '  "space_group": {"value":[], "evidence_span":""},\n'
    '  "lattice_parameters": {"value":{}, "evidence_span":""},\n'
    '  "formula_units_Z": {"value":"", "evidence_span":""},\n'
    '  "chemical_formula": {"value":"", "evidence_span":""},\n'
    '  "database_refs": {"value":[], "evidence_span":""},\n'
    '  "xray_density": {"value":"", "evidence_span":""},\n'
    '  "cell_volume": {"value":"", "evidence_span":""},\n'
    '  "space_group_number": {"value":"", "evidence_span":""},\n'
    '  "formula_weight": {"value":"", "evidence_span":""},\n'
    '  "radiation_wavelength": {"value":"", "evidence_span":""},\n'
    '  "wyckoff_positions": {"value":[], "evidence_span":""},\n'
    '  "miller_indices": {"value":[], "evidence_span":""}\n'
    "}\n"
)

VERIFY_CACHE_DIR = PHASE1_DIR / "_agent_cache_phase2"
if PHASE2_USE_VERIFY_CACHE:
    VERIFY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

XRD_TEXT_TOKENS = [
    "xrd",
    "pxrd",
    "x-ray diffraction",
    "x ray diffraction",
    "diffract",
    "2θ",
    "2theta",
    "kα",
    "lambda",
    "wavelength",
    "step",
    "scan",
    "range",
    "fullprof",
    "gsas",
    "topas",
    "highscore",
    "rietveld",
    "intensity",
]


def count_candidate_signals(candidate: Dict) -> int:
    """Count non-empty fields in a regex candidate (evidence score)."""
    score = 0
    for k, v in candidate.items():
        if isinstance(v, str) and v.strip():
            score += 1
        elif isinstance(v, list) and len(v) > 0:
            score += 1
    return score


def count_text_signals(text: str) -> int:
    """Count XRD keyword tokens present in a block of text."""
    tl = (text or "").lower()
    return sum(1 for tok in XRD_TEXT_TOKENS if tok in tl)


def should_verify_global_agentically(
    candidate: Dict, methods_corpus: str
) -> Tuple[bool, Dict]:
    """Decide whether to LLM-verify the global metadata, with reasons."""
    cand_signals = count_candidate_signals(candidate)
    text_signals = count_text_signals(methods_corpus)
    should = USE_LLM_VERIFY_GLOBAL
    reasons = []
    if cand_signals >= PHASE2_MIN_GLOBAL_SIGNALS_TO_VERIFY:
        reasons.append("regex_candidate_present")
    if text_signals > 0:
        reasons.append("methods_text_has_xrd_signals")
    if ENABLE_AGENTIC_PHASE2:
        should = (
            cand_signals >= PHASE2_MIN_GLOBAL_SIGNALS_TO_VERIFY
            and text_signals > 0
        ) or (
            PHASE2_FORCE_VERIFY_IF_GLOBAL_EMPTY
            and cand_signals == 0
            and text_signals > 0
        )
        if not should:
            reasons.append("skip_global_verify_low_signal")
    return should, {
        "candidate_signals": cand_signals,
        "text_signals": text_signals,
        "reasons": reasons,
    }


def should_verify_figure_agentically(
    candidate: Dict, fig_window: str, fig: Dict
) -> Tuple[bool, Dict]:
    """Decide whether to LLM-verify one figure's metadata, with reasons."""
    cand_signals = count_candidate_signals(candidate)
    text_signals = count_text_signals(fig_window)
    plot_info = fig.get("plot_info", {}) or {}
    plot_signals = 0
    for k in ["x_axis_label", "y_axis_label", "notes"]:
        if (
            isinstance(plot_info.get(k, ""), str)
            and plot_info.get(k, "").strip()
        ):
            plot_signals += 1
    if (
        isinstance(plot_info.get("legend_entries", []), list)
        and len(plot_info.get("legend_entries", [])) > 0
    ):
        plot_signals += 1

    should = USE_LLM_VERIFY_FIGS
    reasons = []
    if cand_signals >= PHASE2_MIN_FIGURE_SIGNALS_TO_VERIFY:
        reasons.append("regex_candidate_present")
    if text_signals > 0:
        reasons.append("figure_window_has_xrd_signals")
    if plot_signals > 0:
        reasons.append("plot_info_present")
    if ENABLE_AGENTIC_PHASE2:
        should = (
            cand_signals >= PHASE2_MIN_FIGURE_SIGNALS_TO_VERIFY
            and text_signals > 0
        )
        if not should:
            reasons.append("skip_figure_verify_low_signal")
    return should, {
        "candidate_signals": cand_signals,
        "text_signals": text_signals,
        "plot_signals": plot_signals,
        "reasons": reasons,
    }


def _verify_cache_key(candidate: Dict, context_text: str) -> str:
    """Hash of provider+model+candidate+context for the verify cache."""
    h = hashlib.sha1()
    h.update(PHASE2_PROVIDER.encode("utf-8"))
    h.update(PHASE2_MODEL.encode("utf-8"))
    h.update(
        json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
    )
    h.update(context_text.encode("utf-8", errors="ignore"))
    return h.hexdigest()


# ---------------- helpers ----------------
def norm(s: str) -> str:
    """Normalize unicode (NFKC), unify dashes, and collapse whitespace."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u00ad", "")
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    return " ".join(s.split())


def resolve_pdf_path(phase1_pdf_value: str, phase1_json_path: Path) -> Path:
    """Find the PDF on disk (stored path, PDF_DIR, or the JSON's folder)."""
    stored = Path(phase1_pdf_value)
    if stored.exists():
        return stored
    name = stored.name
    cand1 = PDF_DIR / name
    if cand1.exists():
        return cand1
    cand2 = phase1_json_path.parent / name
    if cand2.exists():
        return cand2
    raise FileNotFoundError(
        "PDF not found. Tried:\n"
        f"- stored: {stored}\n"
        f"- PDF_DIR: {cand1}\n"
        f"- JSON dir: {cand2}\n"
    )


def extract_lines_and_paras(page) -> Tuple[List[str], List[str], str]:
    """Split a page's text into (lines, paragraphs, raw text)."""
    txt = page.extract_text() or ""
    lines = [norm(x) for x in txt.splitlines() if norm(x)]

    paras: List[str] = []
    cur: List[str] = []
    for raw in txt.splitlines():
        t = norm(raw)
        if not t:
            if cur:
                paras.append(" ".join(cur).strip())
                cur = []
            continue
        cur.append(t)
    if cur:
        paras.append(" ".join(cur).strip())

    return lines, [p for p in paras if p], txt


def doc_level_regex_extract(full_text: str) -> Dict:
    """Regex-scan the whole document text for XRD metadata fields."""
    t = norm(full_text)
    out = {
        "scan_range_2theta": "",
        "step_size": "",
        "radiation": "",
        "software": [],
        "context_temperature": [],
        "crystal_structure": [],
        "space_group": [],
        "lattice_parameters": {},
        "reported_phases": [],
        "profile_function": "",
        "peak_positions_2theta": [],
        "fwhm": [],
        # CIF-comparison fields
        "formula_units_Z": "",
        "chemical_formula": "",
        "database_refs": [],
        "xray_density": "",
        "cell_volume": "",
        "space_group_number": "",
        "formula_weight": "",
        "radiation_wavelength": "",
        "wyckoff_positions": [],
        "miller_indices": [],
    }

    m = RE_2THETA_RANGE.search(t)
    if m:
        out["scan_range_2theta"] = f"{m.group(1)}–{m.group(2)}° 2θ"

    m = RE_STEP.search(t)
    if m:
        out["step_size"] = f"{m.group(1)}°"

    rad = ""
    m = RE_RAD.search(t)
    if m:
        el = (m.group(1) or m.group(2) or "").strip()
        if el:
            rad = f"{el}-Kα"
    mw = RE_WAVELENGTH.search(t)
    if rad and mw:
        out["radiation"] = f"{rad} (λ = {mw.group(1)} Å)"
    else:
        out["radiation"] = rad

    out["software"] = sorted(
        set(m0.group(0) for m0 in RE_SOFTWARE.finditer(t))
    )

    temps = sorted(set(m0.group(1) for m0 in RE_TEMP_ANY.finditer(t)))
    out["context_temperature"] = [f"{x} °C" for x in temps if x]

    out["crystal_structure"] = sorted(
        set(m0.group(1).lower() for m0 in RE_CRYSTAL_STRUCTURE.finditer(t))
    )
    out["space_group"] = sorted(
        set(m0.group(1).strip() for m0 in RE_SPACE_GROUP.finditer(t))
    )

    # Lattice parameters with uncertainty (a, b, c in Å; α, β, γ in °)
    lp = {}
    for m0 in RE_LATTICE_PARAM_WITH_ERR.finditer(t):
        key = m0.group(1).lower()
        val = m0.group(2)
        err = m0.group(3)
        lp[key] = f"{val}({err}) Å" if err else f"{val} Å"
    if not lp:
        for m0 in RE_LATTICE_PARAM.finditer(t):
            key = m0.group(1).lower()
            lp[key] = f"{m0.group(2)} Å"
    for m0 in RE_LATTICE_ANGLE_WITH_ERR.finditer(t):
        raw = m0.group(1).lower()
        key = {"alpha": "α", "beta": "β", "gamma": "γ"}.get(raw, raw)
        val = m0.group(2)
        err = m0.group(3)
        lp[key] = f"{val}({err})°" if err else f"{val}°"
    if not any(k in lp for k in ["α", "β", "γ"]):
        for m0 in RE_LATTICE_ANGLE.finditer(t):
            raw = m0.group(1).lower()
            key = {"alpha": "α", "beta": "β", "gamma": "γ"}.get(raw, raw)
            lp[key] = f"{m0.group(2)}°"
    out["lattice_parameters"] = lp

    # Reported phases (ICDD/JCPDS refs + mineral/compound names)
    phases = set()
    for m0 in RE_PHASE_ICDD.finditer(t):
        phases.add(f"ICDD {m0.group(1)}")
    for m0 in RE_PHASE_NAME.finditer(t):
        phases.add(m0.group(1).strip())
    out["reported_phases"] = sorted(phases)

    # Profile function type
    m = RE_PROFILE_FUNCTION.search(t)
    if m:
        out["profile_function"] = m.group(1).strip()

    # Improvement 8: Structured peak metadata
    peak_positions = sorted(
        set(
            float(m0.group(1))
            for m0 in RE_PEAK_POSITION.finditer(t)
            if 5 <= float(m0.group(1)) <= 120  # sensible 2θ range
        )
    )
    out["peak_positions_2theta"] = [
        f"{p}°" for p in peak_positions[:30]
    ]  # cap at 30

    fwhm_values = sorted(set(m0.group(1) for m0 in RE_FWHM.finditer(t)))
    out["fwhm"] = [f"{v}°" for v in fwhm_values[:10]]

    # ---- CIF-comparison fields ----

    # Z = formula units per unit cell
    m = RE_Z_FORMULA_UNITS.search(t)
    if m:
        out["formula_units_Z"] = m.group(1)

    # Database references (ICSD, COD, Materials Project)
    db_refs = []
    for m0 in RE_ICSD_REF.finditer(t):
        db_refs.append(f"ICSD {m0.group(1)}")
    for m0 in RE_COD_REF.finditer(t):
        db_refs.append(f"COD {m0.group(1)}")
    for m0 in RE_MP_REF.finditer(t):
        db_refs.append(m0.group(1))
    out["database_refs"] = sorted(set(db_refs))

    # X-ray density
    m = RE_XRAY_DENSITY.search(t)
    if m:
        out["xray_density"] = f"{m.group(1)} g/cm³"

    # Cell volume
    m = RE_CELL_VOLUME.search(t)
    if m:
        out["cell_volume"] = f"{m.group(1)} ų"

    # Space group number (International Tables number, 1-230)
    m = RE_SPACE_GROUP_NUMBER.search(t)
    if m:
        num = m.group(1) or m.group(2)
        if num and 1 <= int(num) <= 230:
            out["space_group_number"] = num

    # Formula weight / molecular weight
    m = RE_FORMULA_WEIGHT.search(t)
    if m:
        out["formula_weight"] = f"{m.group(1)} g/mol"

    # Radiation wavelength as separate numeric field
    mw = RE_WAVELENGTH.search(t)
    if mw:
        out["radiation_wavelength"] = f"{mw.group(1)} Å"

    # Wyckoff positions (two-branch regex: group(1) = keyword branch, group(2) = site/position branch)
    wyckoff = set()
    for m0 in RE_WYCKOFF.finditer(t):
        val = m0.group(1) or m0.group(2)
        if val:
            wyckoff.add(val)
    out["wyckoff_positions"] = sorted(wyckoff)[:20]  # cap

    # Miller indices (two-branch regex: groups 1-3 = spaced form, groups 4-6 = compact form)
    miller = set()
    for m0 in RE_MILLER_INDICES.finditer(t):
        h, k, l = (  # noqa: E741
            (m0.group(1), m0.group(2), m0.group(3))
            if m0.group(1)
            else (m0.group(4), m0.group(5), m0.group(6))
        )
        if h and k and l:
            miller.add(f"({h}{k}{l})")
    out["miller_indices"] = sorted(miller)[:30]  # cap

    # Chemical formula — take the most common stoichiometric formula
    # (filter out short/noisy matches)
    formula_candidates = [
        m0.group(0)
        for m0 in RE_CHEMICAL_FORMULA.finditer(t)
        if len(m0.group(0)) >= 4 and any(c.isdigit() for c in m0.group(0))
    ]
    if formula_candidates:
        from collections import Counter

        fc = Counter(formula_candidates)
        out["chemical_formula"] = fc.most_common(1)[0][0]

    return out


def extract_methods_corpus(pages_text: List[str]) -> str:
    """Gather the methods/experimental text likely to hold XRD details."""
    tokens = [
        "xrd",
        "x-ray diffraction",
        "x ray diffraction",
        "diffract",
        "2θ",
        "2theta",
        "kα",
        "lambda",
        "wavelength",
        "step",
        "scan",
        "range",
        "cu",
        "mo",
        "co",
        "fe",
        "fullprof",
        "gsas",
        "topas",
        "highscore",
        "rietveld",
        "lattice",
        "unit cell",
        "space group",
        "crystal structure",
        "icdd",
        "jcpds",
        "phase",
        "pseudo-voigt",
        "voigt",
        "pearson",
        "fwhm",
        "full width",
        "peak position",
        "peak at",
    ]
    blob = norm("\n".join(pages_text))
    sents = SENT_SPLIT.split(blob)
    keep = []
    for s in sents:
        sl = s.lower()
        if any(tok in sl for tok in tokens):
            keep.append(s.strip())
    joined = "\n".join(keep)
    return joined[:20000]


def _build_provenance(
    global_regex: dict,
    fig_candidate: dict,
    fig_verified: Optional[dict],
    fig: dict,
    caption: str,
) -> dict:
    """
    Build a provenance record that explains, for each metadata field,
    where the value came from and how confident we are.

    Sources: global_regex (document-level regex), figure_caption,
             figure_plot_info (Phase I vision), llm_verification (Phase II agent).
    """
    provenance = {}

    # Fields that come from document-wide regex
    for field in (
        "scan_range_2theta",
        "step_size",
        "radiation",
        "software",
        "context_temperature",
        "crystal_structure",
        "space_group",
        "lattice_parameters",
        "reported_phases",
        "profile_function",
        "peak_positions_2theta",
        "fwhm",
    ):
        sources = []
        global_val = global_regex.get(field)
        has_global = (isinstance(global_val, str) and global_val.strip()) or (
            isinstance(global_val, (list, dict)) and global_val
        )

        if has_global:
            sources.append("global_regex")

        # Check if value was enriched by figure-level context
        plot_info = fig.get("plot_info", {}) or {}
        if field in plot_info:
            pi_val = plot_info[field]
            if (isinstance(pi_val, str) and pi_val.strip()) or (
                isinstance(pi_val, (list, dict)) and pi_val
            ):
                sources.append("figure_vision_phase1")

        if caption and field in ("reported_phases", "reported_materials"):
            sources.append("caption")

        # Check LLM verification status
        verified_status = ""
        if isinstance(fig_verified, dict):
            vf = fig_verified.get("verified_fields", fig_verified)
            if isinstance(vf, dict):
                entry = vf.get(field, {})
                if isinstance(entry, dict):
                    verified_status = entry.get("status", "")
                    if verified_status:
                        sources.append("llm_verification")

        provenance[field] = {
            "sources": sources,
            "verified_status": verified_status or "not_verified",
        }

    return provenance


def extract_tables_from_pages(
    pdf_path: str,
    page_indices: Optional[List[int]] = None,
    max_chars: int = 4000,
) -> str:
    """
    Extract tables from specified pages (1-indexed) using pdfplumber.
    Returns a readable text block of all tables found.
    If page_indices is None, extracts from all pages.
    """
    rows_out: List[str] = []
    total_chars = 0

    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages
            if page_indices is not None:
                pages = [
                    pdf.pages[i - 1]
                    for i in page_indices
                    if 0 < i <= len(pdf.pages)
                ]

            for page in pages:
                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables):
                    if not table:
                        continue
                    header = f"[Table on page {page.page_number}]"
                    rows_out.append(header)
                    total_chars += len(header)

                    for row in table:
                        cells = [norm(str(c or "")) for c in row]
                        line = " | ".join(cells)
                        if total_chars + len(line) + 1 > max_chars:
                            rows_out.append("... (truncated)")
                            return "\n".join(rows_out)
                        rows_out.append(line)
                        total_chars += len(line) + 1
    except Exception as e:
        rows_out.append(f"(table extraction error: {e})")

    return "\n".join(rows_out)


def extract_tables_near_figure(
    pdf_path: str,
    fig_page: int,
    max_chars: int = 3000,
) -> str:
    """
    Extract tables from the figure's page and adjacent pages (page-1, page, page+1).
    Crystallographic data tables are often on the same or next page as the XRD figure.
    """
    nearby_pages = [
        p for p in [fig_page - 1, fig_page, fig_page + 1] if p >= 1
    ]
    return extract_tables_from_pages(pdf_path, nearby_pages, max_chars)


def _blocks_from_page(
    doc: fitz.Document, page_i_1based: int
) -> Tuple[List[Tuple[float, float, float, float, str]], float, float]:
    """
    Returns blocks as (x0,y0,x1,y1,text), plus page width/height.
    Uses PyMuPDF which is usually better at preserving layout than pdfplumber's text order.
    """
    page = doc.load_page(page_i_1based - 1)
    rect = page.rect
    pw, ph = float(rect.width), float(rect.height)

    raw = page.get_text("blocks")  # (x0,y0,x1,y1,"text", block_no, block_type)
    blocks = []
    for b in raw:
        if len(b) < 5:
            continue
        x0, y0, x1, y1, txt = (
            float(b[0]),
            float(b[1]),
            float(b[2]),
            float(b[3]),
            b[4],
        )
        t = norm(txt)
        if not t:
            continue
        blocks.append((x0, y0, x1, y1, t))

    # Reading-ish order: top-to-bottom then left-to-right
    blocks.sort(key=lambda z: (z[1], z[0]))
    return blocks, pw, ph


def _looks_like_footer(block_text: str, y0: float, page_h: float) -> bool:
    """True if a text block sits low enough on the page to be a footer."""
    if page_h <= 0:
        return False
    if y0 < CAPTION_FOOTER_Y_FRAC * page_h:
        return False
    t = block_text.lower()
    # Generic footer cues (publisher lines, copyright, page numbers, journal metadata)
    if "copyright" in t or "all rights reserved" in t:
        return True
    if "doi" in t or "issn" in t:
        return True
    if re.search(r"\bpage\s*\d+\b", t):
        return True
    # lots of years / citation style near bottom
    if re.search(r"\b(19|20)\d{2}\b", t) and len(t) < 120:
        return True
    return False


def _find_caption_block_index(
    blocks: List[Tuple[float, float, float, float, str]], fig_num: int
) -> int:
    """
    Find the best block containing 'Figure <fig_num>' or 'Fig. <fig_num>'.
    """
    target = str(fig_num)
    best_i = -1
    best_score = -(10**9)

    for i, (_, y0, _, _, txt) in enumerate(blocks):
        m = FIG_CAP_RE.search(txt)
        if not m:
            continue
        n = m.group(2)
        if n != target:
            continue

        # score: prefer startswith and earlier in block
        score = 0
        if FIG_CAP_RE.match(txt):
            score += 10
        score += max(0, 50 - m.start())  # earlier match better
        score -= int(y0) // 50  # slightly prefer higher up (weak)
        if score > best_score:
            best_score = score
            best_i = i

    return best_i


def find_caption_for_fig_blocks(
    blocks: List[Tuple[float, float, float, float, str]],
    fig_num: int,
    page_h: float,
) -> Tuple[str, int]:
    """
    Returns (caption_text, caption_block_index). index is -1 if not found.

    Builds caption by concatenating nearby blocks following the 'Figure N' block,
    but avoids footer contamination and stops when caption likely ends.
    """
    idx = _find_caption_block_index(blocks, fig_num)
    if idx < 0:
        return "", -1

    x0, y0, x1, y1, t0 = blocks[idx]

    # If strict, require the caption block itself to contain the fig label at all
    if CAPTION_MIN_START_CONF >= 1 and not FIG_CAP_RE.search(t0):
        return "", -1

    cap_parts = [t0]
    total = len(t0)
    prev_y1 = y1
    base_x0 = x0

    for j in range(idx + 1, len(blocks)):
        bx0, by0, bx1, by1, bt = blocks[j]

        if _looks_like_footer(bt, by0, page_h):
            break

        # Stop if next figure caption starts
        m = FIG_CAP_RE.match(bt)
        if m:
            break

        # Heuristic: caption usually stays close vertically
        gap = by0 - prev_y1
        if gap > CAPTION_BLOCK_GAP_PX and total > 60:
            break

        # Heuristic: keep in same "caption column" (avoid jumping across columns)
        if abs(bx0 - base_x0) > 120 and gap > 2:
            # column jump and not immediately adjacent
            break

        # Stop if it becomes too long (avoid swallowing body text)
        if total + 1 + len(bt) > CAPTION_MAX_CHARS:
            break

        cap_parts.append(bt)
        total += 1 + len(bt)
        prev_y1 = by1

    return " ".join(cap_parts).strip(), idx


def nearby_text_after_caption_blocks(
    blocks: List[Tuple[float, float, float, float, str]],
    caption_block_idx: int,
    page_h: float,
) -> str:
    """
    Pull a small amount of post-caption context from subsequent blocks,
    skipping footers. This is generic: works whether "nearby" is body text or not.
    """
    if caption_block_idx < 0:
        return ""

    out = []
    total = 0

    for j in range(
        caption_block_idx + 1,
        min(len(blocks), caption_block_idx + 1 + NEARBY_BLOCKS_AFTER_CAPTION),
    ):
        bx0, by0, bx1, by1, bt = blocks[j]
        if _looks_like_footer(bt, by0, page_h):
            continue
        if FIG_CAP_RE.match(bt):
            break
        if total + len(bt) > NEARBY_MAX_CHARS_BLOCKS:
            break
        out.append(bt)
        total += len(bt)

    return "\n".join(out).strip()


def build_figure_window_text_from_caption_and_nearby(
    caption: str, nearby: str
) -> str:
    """Build a short text window from a figure's caption + nearby text."""
    blob = norm("\n".join([caption or "", nearby or ""]).strip())
    sents = [s.strip() for s in SENT_SPLIT.split(blob) if s.strip()]
    window = " ".join(sents[:FIG_WINDOW_SENTENCES])
    return window[:FIG_WINDOW_MAX_CHARS]


def _extract_json_object(s: str) -> str:
    """Extract the first complete {...} JSON object (brace-counting)."""
    s = (s or "").strip()
    if not s:
        return ""
    if s.startswith("{") and s.endswith("}"):
        return s

    start = s.find("{")
    if start < 0:
        return ""

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return ""


def _http_post_json(url: str, payload: Dict, headers: Dict[str, str]) -> Dict:
    """POST JSON and return the parsed response dict."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def call_text_llm(prompt_a: str, prompt_b: str) -> str:
    """Send a text prompt to the Phase 2 provider and return the reply."""
    provider = PHASE2_PROVIDER

    if provider == "gpt":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "Missing OPENAI_API_KEY but LLM verify is enabled"
            )
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        _t0 = time.time()
        resp = client.responses.create(
            model=PHASE2_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt_a},
                        {"type": "input_text", "text": prompt_b},
                    ],
                }
            ],
            temperature=0.0,
        )
        from diffai.xrdreader.usage_tracker import get_tracker

        get_tracker().log_call(
            phase="phase2",
            model=PHASE2_MODEL,
            resp=resp,
            wall_seconds=time.time() - _t0,
            tool_name="call_text_llm",
        )
        return (getattr(resp, "output_text", "") or "").strip()

    if provider == "grok":
        if not os.environ.get("XAI_API_KEY"):
            raise RuntimeError("Missing XAI_API_KEY but LLM verify is enabled")
        from openai import OpenAI

        client = OpenAI(
            api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1"
        )
        resp = client.responses.create(
            model=PHASE2_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt_a},
                        {"type": "input_text", "text": prompt_b},
                    ],
                }
            ],
            temperature=0.0,
        )
        return (getattr(resp, "output_text", "") or "").strip()

    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Missing GEMINI_API_KEY but LLM verify is enabled"
            )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{PHASE2_MODEL}:generateContent?key={api_key}"
        payload = {
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt_a},
                        {"text": prompt_b},
                    ],
                }
            ],
        }
        _t0 = time.time()
        obj = _http_post_json(url, payload, headers={})
        from diffai.xrdreader.usage_tracker import log_http_call

        log_http_call(
            "phase2",
            PHASE2_MODEL,
            "gemini",
            obj,
            wall_seconds=time.time() - _t0,
            tool_name="call_text_llm",
        )
        txt = ""
        for cand in obj.get("candidates", []):
            content = cand.get("content", {})
            for part in content.get("parts", []):
                if "text" in part:
                    txt += part["text"]
        return txt.strip()

    if provider == "claude":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Missing ANTHROPIC_API_KEY but LLM verify is enabled"
            )
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": PHASE2_MODEL,
            "max_tokens": 4000,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_a},
                        {"type": "text", "text": prompt_b},
                    ],
                }
            ],
        }
        _t0 = time.time()
        obj = _http_post_json(
            url,
            payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        from diffai.xrdreader.usage_tracker import log_http_call

        log_http_call(
            "phase2",
            PHASE2_MODEL,
            "claude",
            obj,
            wall_seconds=time.time() - _t0,
            tool_name="call_text_llm",
        )
        txt = ""
        for block in obj.get("content", []):
            if block.get("type") == "text":
                txt += block.get("text", "")
        return txt.strip()

    if provider == "together":
        api_key = os.environ.get("TOGETHER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Missing TOGETHER_API_KEY but LLM verify is enabled"
            )
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key, base_url="https://api.together.xyz/v1"
        )
        _t0 = time.time()
        resp = client.chat.completions.create(
            model=PHASE2_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt_a}\n\n{prompt_b}",
                }
            ],
            temperature=0.0,
        )
        from diffai.xrdreader.usage_tracker import get_tracker

        _tracker = get_tracker()
        usage = getattr(resp, "usage", None)
        _in = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
        _out = getattr(usage, "completion_tokens", 0) or 0 if usage else 0
        _tracker.log_call(
            phase="phase2",
            model=PHASE2_MODEL,
            input_tokens=_in,
            output_tokens=_out,
            wall_seconds=time.time() - _t0,
            tool_name="call_text_llm",
        )
        return (resp.choices[0].message.content or "").strip()

    raise ValueError(f"Unsupported PHASE2_PROVIDER: {provider}")


def llm_verify_candidate(candidate: Dict, context_text: str) -> Optional[Dict]:
    """
    Returns evidence-gated verified structure (with evidence spans) or None if disabled.
    """
    if not context_text.strip():
        return None

    cache_path = None
    if PHASE2_USE_VERIFY_CACHE:
        cache_key = _verify_cache_key(candidate, context_text)
        cache_path = VERIFY_CACHE_DIR / f"{cache_key}.json"
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    payload = (
        "CONTEXT:\n"
        f"{context_text}\n\n"
        "CANDIDATE:\n"
        f"{json.dumps(candidate, ensure_ascii=False)}\n"
    )

    txt = call_text_llm(VERIFY_PROMPT, payload)
    if not txt.strip():
        return None

    raw = _extract_json_object(txt)
    if not raw:
        return None
    try:
        out = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    # Hard-filter to expected keys only
    expected = {
        "scan_range_2theta",
        "step_size",
        "radiation",
        "software",
        "context_temperature",
        "crystal_structure",
        "space_group",
    }
    out = {k: out.get(k, None) for k in expected}

    # Normalize empty/invalid structures defensively
    def norm_item(item, is_list: bool):
        if not isinstance(item, dict):
            return {"value": [] if is_list else "", "evidence_span": ""}
        val = item.get("value", [] if is_list else "")
        ev = item.get("evidence_span", "")
        if is_list:
            if not isinstance(val, list):
                val = []
        else:
            if not isinstance(val, str):
                val = ""
        if not isinstance(ev, str):
            ev = ""
        return {"value": val, "evidence_span": ev}

    out["scan_range_2theta"] = norm_item(
        out.get("scan_range_2theta"), is_list=False
    )
    out["step_size"] = norm_item(out.get("step_size"), is_list=False)
    out["radiation"] = norm_item(out.get("radiation"), is_list=False)
    out["software"] = norm_item(out.get("software"), is_list=True)
    out["context_temperature"] = norm_item(
        out.get("context_temperature"), is_list=True
    )
    out["crystal_structure"] = norm_item(
        out.get("crystal_structure"), is_list=True
    )
    out["space_group"] = norm_item(out.get("space_group"), is_list=True)

    if cache_path is not None:
        cache_path.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return out


# ======================================================================================
# Agentic verification loop — tools + perceive-act cycle for metadata verification
# ======================================================================================


def _sleep_backoff_p2(attempt: int):
    """Sleep with exponential backoff + jitter before a retry."""
    time.sleep(1.0 * (2**attempt) + random.random() * 0.25)


class Phase2AgentToolbox:
    """
    Holds the open PDF and exposes tool implementations for the verification agent.
    """

    def __init__(
        self, doc: fitz.Document, pages_text: List[str], pdf_path: str = ""
    ):
        self.doc = doc
        self.pages_text = pages_text
        self.n_pages = len(pages_text)
        self.pdf_path = pdf_path

    # ---- tool: search_text --------------------------------------------------
    def tool_search_text(
        self, pattern: str, max_hits: int = 8, snippet_chars: int = 180
    ) -> str:
        """Regex-search page texts; return (page, snippet) hits as JSON."""
        if not pattern or not isinstance(pattern, str):
            return "(error: empty pattern)"
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"(error: bad regex: {e})"

        hits = []
        for p_idx, txt in enumerate(self.pages_text):
            if not txt:
                continue
            for m in rx.finditer(txt):
                a = max(0, m.start() - snippet_chars // 2)
                b = min(len(txt), m.end() + snippet_chars // 2)
                snip = re.sub(r"\s+", " ", txt[a:b]).strip()
                hits.append({"page": p_idx + 1, "snippet": snip})
                if len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                break

        if not hits:
            return "(no matches)"
        return json.dumps(hits, ensure_ascii=False)

    # ---- tool: get_methods_text ---------------------------------------------
    def tool_get_methods_text(self, max_chars: int = 5000) -> str:
        """Return the methods/experimental corpus (XRD-related text)."""
        corpus = extract_methods_corpus(self.pages_text)
        if not corpus.strip():
            return (
                "(no methods/experimental text with XRD-related content found)"
            )
        return corpus[:max_chars]

    # ---- tool: get_page_text ------------------------------------------------
    def tool_get_page_text(self, page: int, max_chars: int = 3500) -> str:
        """Return one 1-indexed page's cleaned text (length-bounded)."""
        if not isinstance(page, int):
            return "(error: page must be int)"
        if page < 1 or page > self.n_pages:
            return f"(error: page {page} out of range 1..{self.n_pages})"
        txt = self.pages_text[page - 1]
        if not txt.strip():
            return f"(page {page} is empty)"
        return re.sub(r"\s+", " ", txt).strip()[:max_chars]

    # ---- tool: get_figure_context -------------------------------------------
    def tool_get_figure_context(self, fig_num: int) -> str:
        """Return the caption + nearby text window for a figure number."""
        if not isinstance(fig_num, int):
            return "(error: fig_num must be int)"
        try:
            for p_idx in range(min(self.n_pages, self.doc.page_count)):
                blocks, pw, ph = _blocks_from_page(self.doc, p_idx + 1)
                cap, cap_bidx = find_caption_for_fig_blocks(
                    blocks, fig_num, ph
                )
                if cap.strip():
                    near = nearby_text_after_caption_blocks(
                        blocks, cap_bidx, ph
                    )
                    return build_figure_window_text_from_caption_and_nearby(
                        cap, near
                    )[:3000]
        except Exception as e:
            return f"(error: {e})"
        return f"(Figure {fig_num} caption not found)"

    # ---- tool: get_tables --------------------------------------------------
    def tool_get_tables(
        self, page: Optional[int] = None, max_chars: int = 3000
    ) -> str:
        """Extract table text from a page (or all pages)."""
        if not self.pdf_path:
            return "(error: pdf_path not set)"
        pages = [page] if isinstance(page, int) and page >= 1 else None
        result = extract_tables_from_pages(self.pdf_path, pages, max_chars)
        if not result.strip():
            return "(no tables found)"
        return result


PHASE2_AGENT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_text",
        "description": (
            "Regex search across the full PDF text. Returns up to 8 (page, snippet) matches. "
            "Use for XRD terms like '2θ|2theta|step.*size|scan.*range|Cu.*Kα|Rietveld|space.*group|crystal.*structure'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_methods_text",
        "description": (
            "Return XRD-related sentences extracted from methods/experimental sections. "
            "Start here — this is usually where scan parameters, radiation, and software are reported."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_page_text",
        "description": "Return the full text of a specific page (1-indexed). Use after a search hit to read surrounding context.",
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "minimum": 1},
            },
            "required": ["page"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_figure_context",
        "description": "Return the caption and nearby text for a specific figure number. Use to verify figure-level metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "fig_num": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "The figure number to look up",
                },
            },
            "required": ["fig_num"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_tables",
        "description": (
            "Extract tables from the PDF. Tables often contain crystallographic data "
            "(lattice parameters, space groups, phase fractions). "
            "Optionally specify a page number; otherwise returns tables from all pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Page number to extract tables from (optional, omit for all pages)",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "finalize",
        "description": (
            "Submit the verified XRD metadata and END the loop. "
            "For each field, provide the value and an evidence_span (exact substring from the PDF). "
            "If a field cannot be verified, set value to empty string (or empty list) and leave evidence_span empty."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scan_range_2theta": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "step_size": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "radiation": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "software": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "context_temperature": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "crystal_structure": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "space_group": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "lattice_parameters": {
                    "type": "object",
                    "description": "Lattice parameters. value is an object like {a: '5.43 Å', b: '...', c: '...', α: '90°', ...}",
                    "properties": {
                        "value": {"type": "object"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "reported_phases": {
                    "type": "object",
                    "description": "Phase names, mineral names, or ICDD/JCPDS reference numbers",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "profile_function": {
                    "type": "object",
                    "description": "Peak profile function used (e.g. pseudo-Voigt, Gaussian, Pearson VII)",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "peak_positions_2theta": {
                    "type": "object",
                    "description": "Reported 2θ peak positions in degrees (e.g. ['25.3°', '48.0°'])",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "fwhm": {
                    "type": "object",
                    "description": "FWHM values reported in the paper",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "formula_units_Z": {
                    "type": "object",
                    "description": "Z = number of formula units per unit cell (e.g. '4', '8')",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "chemical_formula": {
                    "type": "object",
                    "description": "Stoichiometric chemical formula of the main material (e.g. 'Np2O5', 'NiFe2O4', 'Ca2Fe2O5')",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "database_refs": {
                    "type": "object",
                    "description": "Database reference numbers: ICSD, COD, or Materials Project IDs (e.g. ['ICSD 12345', 'mp-19114'])",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "xray_density": {
                    "type": "object",
                    "description": "X-ray density (theoretical density from crystal structure) in g/cm3",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "cell_volume": {
                    "type": "object",
                    "description": "Unit cell volume in ų (e.g. '588.97 ų', '312.4 ų')",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "space_group_number": {
                    "type": "object",
                    "description": "International Tables space group number 1-230 (e.g. '62', '227')",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "formula_weight": {
                    "type": "object",
                    "description": "Chemical formula weight / molecular weight in g/mol (e.g. '245.67 g/mol')",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "radiation_wavelength": {
                    "type": "object",
                    "description": "Radiation wavelength as a separate numeric value in Å (e.g. '1.5406 Å', '0.71073 Å')",
                    "properties": {
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
                "wyckoff_positions": {
                    "type": "object",
                    "description": "Wyckoff positions/sites mentioned (e.g. ['4a', '8c', '16d'])",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
                "miller_indices": {
                    "type": "object",
                    "description": "Miller indices (hkl) of reported XRD peaks (e.g. ['(111)', '(220)', '(311)'])",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_span": {"type": "string"},
                    },
                },
            },
            "required": [
                "scan_range_2theta",
                "step_size",
                "radiation",
                "software",
                "context_temperature",
                "crystal_structure",
                "space_group",
                "lattice_parameters",
                "reported_phases",
                "profile_function",
                "peak_positions_2theta",
                "fwhm",
                "formula_units_Z",
                "chemical_formula",
                "database_refs",
                "xray_density",
                "cell_volume",
                "space_group_number",
                "formula_weight",
                "radiation_wavelength",
                "wyckoff_positions",
                "miller_indices",
            ],
            "additionalProperties": False,
        },
    },
]


PHASE2_AGENT_SYSTEM_PROMPT = """
You are an XRD metadata verification agent. You are given a CANDIDATE JSON block
containing XRD parameters extracted by regex from a scientific PDF.

Your goal: verify each field against the actual PDF text, keeping only fields that
are explicitly supported by evidence. For each field you keep, provide an evidence_span
that is an exact substring copied from the PDF.

CANDIDATE FIELDS:
- scan_range_2theta: the 2θ range (e.g. "10-80°")
- step_size: scan step size (e.g. "0.02°")
- radiation: X-ray source (e.g. "Cu Kα")
- software: Rietveld/analysis software (e.g. ["FullProf", "GSAS-II"])
- context_temperature: temperatures mentioned (e.g. ["300°C", "RT"])
- crystal_structure: crystal systems (e.g. ["fcc", "bcc"])
- space_group: space groups (e.g. ["Fm-3m", "P63/mmc"])
- lattice_parameters: unit cell parameters (e.g. {{"a": "5.43 Å", "b": "5.43 Å", "c": "5.43 Å", "α": "90°"}})
- reported_phases: identified phase names, minerals, or ICDD/JCPDS refs (e.g. ["anatase", "rutile", "ICDD 21-1272"])
- profile_function: peak profile function used in fitting (e.g. "pseudo-Voigt")
- peak_positions_2theta: reported 2θ peak positions (e.g. ["25.3°", "48.0°"])
- fwhm: reported full-width-at-half-maximum values (e.g. ["0.15°"])
- formula_units_Z: number of formula units per unit cell (e.g. "4")
- chemical_formula: stoichiometric formula of the main material (e.g. "Np2O5", "NiFe2O4")
- database_refs: ICSD, COD, or Materials Project reference numbers (e.g. ["ICSD 12345"])
- xray_density: X-ray/theoretical density in g/cm3 (e.g. "5.36 g/cm³")
- cell_volume: unit cell volume in ų (e.g. "588.97 ų")
- space_group_number: International Tables space group number 1-230 (e.g. "62", "227")
- formula_weight: chemical formula weight / molecular weight in g/mol (e.g. "245.67 g/mol")
- radiation_wavelength: radiation wavelength as a numeric value in Å (e.g. "1.5406 Å")
- wyckoff_positions: Wyckoff sites mentioned (e.g. ["4a", "8c"])
- miller_indices: Miller indices (hkl) of XRD peaks (e.g. ["(111)", "(220)", "(311)"])

Strategy:
1. Start with get_methods_text — methods/experimental sections usually contain
   scan parameters, radiation source, and software.
2. Use get_tables to check for crystallographic data tables (lattice parameters,
   space groups, phase fractions are often reported in tables rather than text).
3. If specific fields are still unverified, use search_text with targeted patterns.
4. For figure-specific context, use get_figure_context.
5. Call finalize when you have verified (or rejected) all fields.

Rules:
- Do NOT guess. If a field is not supported by text evidence, set it to empty.
- evidence_span must be an exact substring from the PDF text you retrieved.
- Budget: {max_steps} tool calls. Be efficient.
""".strip()


def _safe_json_loads_p2(s: str) -> Dict:
    """Parse the first JSON object in `s`; return {} on any failure."""
    try:
        obj = _extract_json_object(s)
        return json.loads(obj) if obj else {}
    except Exception:
        return {}


def _execute_phase2_tool(
    toolbox: Phase2AgentToolbox, name: str, args: Dict
) -> str:
    """Dispatch one Phase 2 tool call; return its observation string."""
    try:
        if name == "search_text":
            return toolbox.tool_search_text(str(args.get("pattern", "")))
        if name == "get_methods_text":
            return toolbox.tool_get_methods_text()
        if name == "get_page_text":
            return toolbox.tool_get_page_text(int(args.get("page", 0)))
        if name == "get_figure_context":
            return toolbox.tool_get_figure_context(int(args.get("fig_num", 0)))
        if name == "get_tables":
            page_arg = args.get("page")
            return toolbox.tool_get_tables(
                page=int(page_arg) if page_arg is not None else None
            )
        return f"(error: unknown tool '{name}')"
    except Exception as e:
        return f"(error: tool {name} failed: {e})"


def _normalize_finalize_output(args: Dict) -> Dict:
    """Normalize a finalize call's arguments into the standard Phase 2 verified schema."""
    # field_name -> type: "str", "list", "dict"
    expected = {
        "scan_range_2theta": "str",
        "step_size": "str",
        "radiation": "str",
        "software": "list",
        "context_temperature": "list",
        "crystal_structure": "list",
        "space_group": "list",
        "lattice_parameters": "dict",
        "reported_phases": "list",
        "profile_function": "str",
        "peak_positions_2theta": "list",
        "fwhm": "list",
        # CIF-comparison fields
        "formula_units_Z": "str",
        "chemical_formula": "str",
        "database_refs": "list",
        "xray_density": "str",
        "cell_volume": "str",
        "space_group_number": "str",
        "formula_weight": "str",
        "radiation_wavelength": "str",
        "wyckoff_positions": "list",
        "miller_indices": "list",
    }
    out = {}
    for field, ftype in expected.items():
        item = args.get(field)
        if not isinstance(item, dict):
            item = {}
        default = [] if ftype == "list" else ({} if ftype == "dict" else "")
        val = item.get("value", default)
        ev = item.get("evidence_span", "")
        if ftype == "list" and not isinstance(val, list):
            val = []
        elif ftype == "dict" and not isinstance(val, dict):
            val = {}
        elif ftype == "str" and not isinstance(val, str):
            val = str(val) if val else ""
        if not isinstance(ev, str):
            ev = ""
        out[field] = {"value": val, "evidence_span": ev}
    return out


def run_phase2_verify_agent(
    candidate: Dict,
    doc: fitz.Document,
    pages_text: List[str],
    scope_label: str = "global",
    fig_num: Optional[int] = None,
    log_prefix: str = "",
    pdf_path: str = "",
) -> Optional[Dict]:
    """
    Agentic verification loop — works with ALL providers via ToolCaller.
    """
    provider = PHASE2_PROVIDER
    model = PHASE2_MODEL

    from diffai.xrdreader.tool_calling import ToolCaller

    toolbox = Phase2AgentToolbox(doc, pages_text, pdf_path=pdf_path)

    caller = ToolCaller(provider, model, max_retries=PHASE2_AGENT_MAX_RETRIES)
    caller.set_system(
        PHASE2_AGENT_SYSTEM_PROMPT.format(max_steps=PHASE2_AGENT_MAX_STEPS)
    )

    cand_json = json.dumps(candidate, ensure_ascii=False)
    scope_hint = f"Scope: {scope_label}."
    if fig_num is not None:
        scope_hint += f" This is for Figure {fig_num}."

    caller.add_user_message(
        f"{scope_hint}\n\n"
        f"CANDIDATE (from regex):\n{cand_json}\n\n"
        "Search the PDF for evidence to verify or reject each field, then call finalize."
    )

    trace = []
    final = None

    for step in range(PHASE2_AGENT_MAX_STEPS):
        try:
            _t0 = time.time()
            tool_calls, _ = caller.call(PHASE2_AGENT_TOOLS)
            elapsed = time.time() - _t0

            from diffai.xrdreader.usage_tracker import get_tracker

            usage = caller.get_last_call_usage()
            get_tracker().log_call(
                phase="phase2_agent",
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                wall_seconds=elapsed,
            )
        except Exception as e:
            print(f"{log_prefix}[phase2_agent] API failed: {repr(e)}")
            break

        if not tool_calls:
            print(
                f"{log_prefix}[phase2_agent] step {step+1}: no tool calls; stopping."
            )
            break

        stop_loop = False
        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"]
            call_id = tc["call_id"]

            if name == "finalize":
                final = _normalize_finalize_output(args)
                trace.append(
                    {
                        "step": step + 1,
                        "tool": "finalize",
                        "args": final,
                        "observation": "(loop ended)",
                    }
                )
                stop_loop = True
                break

            observation = _execute_phase2_tool(toolbox, name, args)
            trace.append(
                {
                    "step": step + 1,
                    "tool": name,
                    "args": args,
                    "observation": observation[:2000],
                }
            )
            caller.add_tool_result(call_id, name, observation)

        if stop_loop:
            break

    steps_used = len(trace)
    if final is None:
        print(
            f"{log_prefix}[phase2_agent] budget exhausted after {steps_used} steps."
        )
        final = _normalize_finalize_output({})

    final["agent_trace"] = trace
    final["steps_used"] = steps_used
    return final


def tag_if_nonempty_value(v) -> str:
    """Return a "phase 2 addition" tag if the value is non-empty."""
    if isinstance(v, str):
        return "phase 2 addition" if v.strip() else ""
    if isinstance(v, list):
        return "phase 2 addition" if len(v) > 0 else ""
    if isinstance(v, dict) and "value" in v:
        return tag_if_nonempty_value(v["value"])
    return ""


def enrich_one_phase1_json(phase1_json_path: Path):
    """Enrich one Phase 1 JSON with verified XRD metadata.

    Regex-extracts candidates from the PDF text, optionally LLM-verifies them
    (global + per figure), and writes `*__phase2_enriched.json`.
    """
    phase1 = json.loads(phase1_json_path.read_text(encoding="utf-8"))
    if "pdf" not in phase1:
        raise KeyError(f"{phase1_json_path.name} missing key: 'pdf'")

    pdf_path = resolve_pdf_path(phase1["pdf"], phase1_json_path)
    phase1["pdf_resolved"] = str(pdf_path)

    # pdfplumber cache (kept for global full-text extraction)
    page_cache: Dict[int, Tuple[List[str], List[str], str]] = {}
    pages_text: List[str] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            lines, paras, txt = extract_lines_and_paras(page)
            page_cache[i] = (lines, paras, txt)
            pages_text.append(txt)

    full_text = "\n".join(pages_text)

    # Open the fitz doc early so the agent can use it for both global and figure verify.
    doc = fitz.open(str(pdf_path))
    try:

        # ---------------- GLOBAL regex + optional verify (1 call / PDF) ----------------
        global_regex = doc_level_regex_extract(full_text)
        global_verified_struct: Optional[Dict] = None

        methods_corpus = extract_methods_corpus(pages_text)
        should_verify_global, global_agent_decision = (
            should_verify_global_agentically(global_regex, methods_corpus)
        )
        if should_verify_global:
            global_verified_struct = run_phase2_verify_agent(
                global_regex,
                doc,
                pages_text,
                scope_label="global",
                log_prefix=f"[{phase1_json_path.name}] ",
                pdf_path=str(pdf_path),
            )

        # Strip agent_trace from the verified struct before storing in global_xrd_text;
        # store it separately for auditability.
        global_agent_trace = {}
        if isinstance(global_verified_struct, dict):
            global_agent_trace = {
                "agent_trace": global_verified_struct.pop("agent_trace", []),
                "steps_used": global_verified_struct.pop("steps_used", 0),
            }

        phase1["global_xrd_text"] = {
            "regex_extracted": global_regex,
            "regex_extracted_tag": tag_if_nonempty_value(
                [
                    v
                    for v in global_regex.values()
                    if (isinstance(v, str) and v.strip())
                    or (isinstance(v, list) and len(v) > 0)
                ]
            ),
            "verified": global_verified_struct or {},
            "verified_tag": tag_if_nonempty_value(
                global_verified_struct or {}
            ),
            "agent_decision": {**global_agent_decision, **global_agent_trace},
        }

        # --- Improvement 5: Detect Supporting Information / Appendix sections ---
        si_info = detect_si_pages(pages_text)
        phase1["supplementary_info"] = si_info

        safe_base = make_safe_stem(pdf_path.stem)

        # ---------------- FIGURE: caption + nearby + optional per-figure verify ----------------
        for fig in phase1.get("xrd_figures", []):
            fig_num = fig.get("matched_fig_num", None)
            page_i = fig.get("page", None)
            if not isinstance(fig_num, int) or not isinstance(page_i, int):
                continue

            blocks, pw, ph = _blocks_from_page(doc, page_i)

            cap, cap_bidx = find_caption_for_fig_blocks(blocks, fig_num, ph)
            near = nearby_text_after_caption_blocks(blocks, cap_bidx, ph)

            table_text = extract_tables_near_figure(str(pdf_path), page_i)

            fig.setdefault("text_info", {})
            fig["text_info"]["caption_extracted"] = cap if cap.strip() else ""
            fig["text_info"]["caption_extracted_tag"] = (
                "phase 2 addition" if cap.strip() else ""
            )
            fig["text_info"]["nearby_text"] = near if near.strip() else ""
            fig["text_info"]["nearby_text_tag"] = (
                "phase 2 addition" if near.strip() else ""
            )
            fig["text_info"]["nearby_tables"] = (
                table_text if table_text.strip() else ""
            )
            fig["text_info"]["nearby_tables_tag"] = (
                "phase 2 addition" if table_text.strip() else ""
            )

            fig_window = build_figure_window_text_from_caption_and_nearby(
                cap, near
            )
            if table_text.strip():
                fig_window += "\n\n[TABLES NEAR FIGURE]\n" + table_text[:2000]

            plot_info = fig.get("plot_info", {}) or {}

            reported_materials = plot_info.get("reported_materials", []) or []
            reported_phases = plot_info.get("reported_phases", []) or []

            legend_materials = []
            for entry in plot_info.get("legend_entries", []) or []:
                if isinstance(entry, dict):
                    m = str(entry.get("material", "")).strip()
                    if m:
                        legend_materials.append(m)

            def uniq_clean(items):
                out = []
                seen = set()
                for x in items:
                    s = " ".join(str(x).split()).strip()
                    if s and s.lower() not in seen:
                        seen.add(s.lower())
                        out.append(s)
                return out

            material_systems = uniq_clean(
                reported_materials + legend_materials
            )

            fig_candidate = dict(global_regex)
            fig_candidate["reported_materials"] = uniq_clean(
                reported_materials
            )
            fig_candidate["reported_phases"] = uniq_clean(reported_phases)
            fig_candidate["legend_materials"] = uniq_clean(legend_materials)
            fig_candidate["material_systems"] = material_systems

            fig_verified_struct: Optional[Dict] = None
            should_verify_fig, fig_agent_decision = (
                should_verify_figure_agentically(
                    fig_candidate, fig_window, fig
                )
            )
            if should_verify_fig and fig_window.strip():
                fig_verified_struct = run_phase2_verify_agent(
                    fig_candidate,
                    doc,
                    pages_text,
                    scope_label="figure",
                    fig_num=fig_num,
                    log_prefix=f"[{phase1_json_path.name} fig{fig_num}] ",
                    pdf_path=str(pdf_path),
                )

            # Strip agent_trace from verified struct; store separately.
            fig_agent_trace = {}
            if isinstance(fig_verified_struct, dict):
                fig_agent_trace = {
                    "agent_trace": fig_verified_struct.pop("agent_trace", []),
                    "steps_used": fig_verified_struct.pop("steps_used", 0),
                }

            fig["phase2_xrd_text"] = {
                "context_window": fig_window,
                "context_window_tag": (
                    "phase 2 addition" if fig_window.strip() else ""
                ),
                "regex_candidate": fig_candidate,
                "regex_candidate_tag": tag_if_nonempty_value(
                    [
                        v
                        for v in fig_candidate.values()
                        if (isinstance(v, str) and v.strip())
                        or (isinstance(v, list) and len(v) > 0)
                    ]
                ),
                "verified": fig_verified_struct or {},
                "verified_tag": tag_if_nonempty_value(
                    fig_verified_struct or {}
                ),
                "agent_decision": {**fig_agent_decision, **fig_agent_trace},
            }

            # --- Data-metadata linking: provenance + figure_id ---
            fig["figure_id"] = f"{safe_base}__fig{fig_num}_p{page_i}"
            fig["metadata_provenance"] = _build_provenance(
                global_regex,
                fig_candidate,
                fig_verified_struct,
                fig,
                cap,
            )
    finally:
        doc.close()

    safe_base = make_safe_stem(pdf_path.stem)
    out_json = phase1_json_path.with_name(f"{safe_base}__phase2_enriched.json")
    out_json.write_text(
        json.dumps(phase1, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[OK] {phase1_json_path.name} -> {out_json.name}")


def main():
    """Enrich every Phase 1 JSON in the run folder."""
    print(
        f"[INFO] Phase 2 using provider={PHASE2_PROVIDER}, model={PHASE2_MODEL}"
    )
    if not PHASE1_DIR.exists():
        raise FileNotFoundError(f"PHASE1_DIR not found: {PHASE1_DIR}")

    phase1_files = sorted(PHASE1_DIR.glob("*__phase1_raw.json"))
    if not phase1_files:
        raise FileNotFoundError(
            f"No *__phase1_raw.json found in: {PHASE1_DIR}"
        )

    for p1 in phase1_files:
        try:
            enrich_one_phase1_json(p1)
        except Exception as e:
            import traceback

            print(f"[FAIL] {p1.name}: {repr(e)}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
