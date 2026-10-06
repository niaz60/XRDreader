"""Phase III -- cross-verify Phase II metadata against the PDF.

A second LLM pass (optionally an audit agent with validation tools) checks
each extracted field against the paper's own text, then deterministic
consistency checks (space group vs crystal system, lattice-parameter
metric, X-ray wavelength) run as a safety net. Reads `*__phase2_clean.json`
and writes `*__verified_final.json` (+ a `*__verified_log.json`).
"""

import base64
import json
import os
import random
import re
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz
import pdfplumber

from diffai.xrdreader.config import (
    ENABLE_AGENTIC_PHASE3,
    MAX_FIGURE_CONTEXT_CHARS_VERIFY,
    MAX_GLOBAL_CONTEXT_CHARS_VERIFY,
    MODEL,
    PHASE1_DIR,
    PROVIDER,
    USE_XRD_FOCUSED_TEXT_VERIFY,
)
from diffai.xrdreader.utils import make_safe_stem

# Agent loop knobs (env-overridable)
PHASE3_AGENT_MAX_STEPS = int(os.getenv("PHASE3_AGENT_MAX_STEPS", "12"))
PHASE3_AGENT_MAX_RETRIES = int(os.getenv("PHASE3_AGENT_MAX_RETRIES", "3"))


FIG_CAP_RE = re.compile(r"(fig\.|figure)\s*(\d+)\s*[:.\-]?\s*", re.IGNORECASE)

XRD_TEXT_TOKENS = [
    "xrd",
    "pxrd",
    "x-ray diffraction",
    "x ray diffraction",
    "diffract",
    "2θ",
    "2theta",
    "two theta",
    "kα",
    "lambda",
    "wavelength",
    "scan",
    "step",
    "rietveld",
    "fullprof",
    "gsas",
    "topas",
    "space group",
    "crystal structure",
    "phase",
    "room temperature",
    "lattice",
    "unit cell",
    "icdd",
    "jcpds",
    "pseudo-voigt",
    "voigt",
    "pearson",
    "profile function",
    "peak shape",
    "fwhm",
    "full width",
    "peak position",
    "peak at",
]

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# ---- Crystallographic reference data (T1 validation tools) ----------------

_SG_NUM_UPPER = [
    (2, "triclinic"),
    (15, "monoclinic"),
    (74, "orthorhombic"),
    (142, "tetragonal"),
    (167, "trigonal"),
    (194, "hexagonal"),
    (230, "cubic"),
]


def _sg_number_to_system(n: int) -> str:
    """Map space-group number (1-230) to crystal system."""
    for upper, system in _SG_NUM_UPPER:
        if n <= upper:
            return system
    return ""


# Complete ITA (International Tables Vol. A) space-group table — all 230 entries.
# Keys use standard short Hermann-Mauguin notation with underscore for subscripts
# and dash for overlines (e.g. P2_1/c, Fm-3m).  Common alternate notations and
# dash-free variants are appended at the end.
_SG_TO_NUM: Dict[str, int] = {
    # ---- Triclinic (1-2) ----
    "P1": 1,
    "P-1": 2,
    # ---- Monoclinic (3-15) ----
    "P2": 3,
    "P2_1": 4,
    "C2": 5,
    "Pm": 6,
    "Pc": 7,
    "Cm": 8,
    "Cc": 9,
    "P2/m": 10,
    "P2_1/m": 11,
    "C2/m": 12,
    "P2/c": 13,
    "P2_1/c": 14,
    "C2/c": 15,
    # ---- Orthorhombic (16-74) ----
    "P222": 16,
    "P222_1": 17,
    "P2_12_12": 18,
    "P2_12_12_1": 19,
    "C222_1": 20,
    "C222": 21,
    "F222": 22,
    "I222": 23,
    "I2_12_12_1": 24,
    "Pmm2": 25,
    "Pmc2_1": 26,
    "Pcc2": 27,
    "Pma2": 28,
    "Pca2_1": 29,
    "Pnc2": 30,
    "Pmn2_1": 31,
    "Pba2": 32,
    "Pna2_1": 33,
    "Pnn2": 34,
    "Cmm2": 35,
    "Cmc2_1": 36,
    "Ccc2": 37,
    "Amm2": 38,
    "Aem2": 39,
    "Ama2": 40,
    "Aea2": 41,
    "Fmm2": 42,
    "Fdd2": 43,
    "Imm2": 44,
    "Iba2": 45,
    "Ima2": 46,
    "Pmmm": 47,
    "Pnnn": 48,
    "Pccm": 49,
    "Pban": 50,
    "Pmma": 51,
    "Pnna": 52,
    "Pmna": 53,
    "Pcca": 54,
    "Pbam": 55,
    "Pccn": 56,
    "Pbcm": 57,
    "Pnnm": 58,
    "Pmmn": 59,
    "Pbcn": 60,
    "Pbca": 61,
    "Pnma": 62,
    "Cmcm": 63,
    "Cmce": 64,
    "Cmmm": 65,
    "Cccm": 66,
    "Cmme": 67,
    "Ccce": 68,
    "Fmmm": 69,
    "Fddd": 70,
    "Immm": 71,
    "Ibam": 72,
    "Ibca": 73,
    "Imma": 74,
    # ---- Tetragonal (75-142) ----
    "P4": 75,
    "P4_1": 76,
    "P4_2": 77,
    "P4_3": 78,
    "I4": 79,
    "I4_1": 80,
    "P-4": 81,
    "I-4": 82,
    "P4/m": 83,
    "P4_2/m": 84,
    "P4/n": 85,
    "P4_2/n": 86,
    "I4/m": 87,
    "I4_1/a": 88,
    "P422": 89,
    "P42_12": 90,
    "P4_122": 91,
    "P4_12_12": 92,
    "P4_222": 93,
    "P4_22_12": 94,
    "P4_322": 95,
    "P4_32_12": 96,
    "I422": 97,
    "I4_122": 98,
    "P4mm": 99,
    "P4bm": 100,
    "P4_2cm": 101,
    "P4_2nm": 102,
    "P4cc": 103,
    "P4nc": 104,
    "P4_2mc": 105,
    "P4_2bc": 106,
    "I4mm": 107,
    "I4cm": 108,
    "I4_1md": 109,
    "I4_1cd": 110,
    "P-42m": 111,
    "P-42c": 112,
    "P-42_1m": 113,
    "P-42_1c": 114,
    "P-4m2": 115,
    "P-4c2": 116,
    "P-4b2": 117,
    "P-4n2": 118,
    "I-4m2": 119,
    "I-4c2": 120,
    "I-42m": 121,
    "I-42d": 122,
    "P4/mmm": 123,
    "P4/mcc": 124,
    "P4/nbm": 125,
    "P4/nnc": 126,
    "P4/mbm": 127,
    "P4/mnc": 128,
    "P4/nmm": 129,
    "P4/ncc": 130,
    "P4_2/mmc": 131,
    "P4_2/mcm": 132,
    "P4_2/nbc": 133,
    "P4_2/nnm": 134,
    "P4_2/mbc": 135,
    "P4_2/mnm": 136,
    "P4_2/nmc": 137,
    "P4_2/ncm": 138,
    "I4/mmm": 139,
    "I4/mcm": 140,
    "I4_1/amd": 141,
    "I4_1/acd": 142,
    # ---- Trigonal (143-167) ----
    "P3": 143,
    "P3_1": 144,
    "P3_2": 145,
    "R3": 146,
    "P-3": 147,
    "R-3": 148,
    "P312": 149,
    "P321": 150,
    "P3_112": 151,
    "P3_121": 152,
    "P3_212": 153,
    "P3_221": 154,
    "R32": 155,
    "P3m1": 156,
    "P31m": 157,
    "P3c1": 158,
    "P31c": 159,
    "R3m": 160,
    "R3c": 161,
    "P-31m": 162,
    "P-31c": 163,
    "P-3m1": 164,
    "P-3c1": 165,
    "R-3m": 166,
    "R-3c": 167,
    # ---- Hexagonal (168-194) ----
    "P6": 168,
    "P6_1": 169,
    "P6_5": 170,
    "P6_2": 171,
    "P6_4": 172,
    "P6_3": 173,
    "P-6": 174,
    "P6/m": 175,
    "P6_3/m": 176,
    "P622": 177,
    "P6_122": 178,
    "P6_522": 179,
    "P6_222": 180,
    "P6_422": 181,
    "P6_322": 182,
    "P6mm": 183,
    "P6cc": 184,
    "P6_3cm": 185,
    "P6_3mc": 186,
    "P-6m2": 187,
    "P-6c2": 188,
    "P-62m": 189,
    "P-62c": 190,
    "P6/mmm": 191,
    "P6/mcc": 192,
    "P6_3/mcm": 193,
    "P6_3/mmc": 194,
    # ---- Cubic (195-230) ----
    "P23": 195,
    "F23": 196,
    "I23": 197,
    "P2_13": 198,
    "I2_13": 199,
    "Pm-3": 200,
    "Pn-3": 201,
    "Fm-3": 202,
    "Fd-3": 203,
    "Im-3": 204,
    "Pa-3": 205,
    "Ia-3": 206,
    "P432": 207,
    "P4_232": 208,
    "F432": 209,
    "F4_132": 210,
    "I432": 211,
    "P4_332": 212,
    "P4_132": 213,
    "I4_132": 214,
    "P-43m": 215,
    "F-43m": 216,
    "I-43m": 217,
    "P-43n": 218,
    "F-43c": 219,
    "I-43d": 220,
    "Pm-3m": 221,
    "Pn-3n": 222,
    "Pm-3n": 223,
    "Pn-3m": 224,
    "Fm-3m": 225,
    "Fm-3c": 226,
    "Fd-3m": 227,
    "Fd-3c": 228,
    "Im-3m": 229,
    "Ia-3d": 230,
}

# ---- Common alternate notations (non-standard settings, dash-free, etc.) ----
# Papers frequently omit dashes in cubic overlines or use alternate axis settings.
_SG_ALTERNATES: Dict[str, int] = {
    # Dash-free cubic variants (very common in papers)
    "Pm3m": 221,
    "Pn3n": 222,
    "Pm3n": 223,
    "Pn3m": 224,
    "Fm3m": 225,
    "Fm3c": 226,
    "Fd3m": 227,
    "Fd3c": 228,
    "Im3m": 229,
    "Ia3d": 230,
    "Pm3": 200,
    "Pn3": 201,
    "Fm3": 202,
    "Fd3": 203,
    "Im3": 204,
    "Pa3": 205,
    "Ia3": 206,
    "P43m": 215,
    "F43m": 216,
    "I43m": 217,
    "P43n": 218,
    "F43c": 219,
    "I43d": 220,
    # Alternate orthorhombic settings (non-standard axis choices)
    "Pbnm": 62,
    "Pcmn": 62,  # alt settings of Pnma (#62)
    "Amma": 63,
    "Bbmm": 63,  # alt settings of Cmcm (#63)
    "Cmca": 64,
    "Abm2": 39,
    "Abma": 64,  # old ITA names for Cmce / Aem2
    "Ccca": 68,
    "Cmma": 67,  # old ITA names for Ccce / Cmme
    # Alternate monoclinic settings
    "B2/b": 15,
    "I2/a": 15,
    "A2/a": 15,  # alt settings of C2/c
    "P2_1/n": 14,
    "P2_1/a": 14,  # alt settings of P2_1/c
    "I2/c": 15,
    # Trigonal with/without overline
    "R3m": 160,
    "R3c": 161,
    "R3": 146,  # already in _SG_TO_NUM but explicit
    "R-3m": 166,
    "R-3c": 167,
    "R3bar": 148,
    "R3barm": 166,
    "R3barc": 167,
    # Tetragonal alternate subscript styles
    "P42/mnm": 136,
    "P42/nmc": 137,
    "P42/ncm": 138,
    "I41/amd": 141,
    "I41/acd": 142,
    "I41/a": 88,
    "P42/mmc": 131,
    "P42/mcm": 132,
}

# Merge alternates into the main table
_SG_TO_NUM.update(_SG_ALTERNATES)

# Backward-compatible alias
_COMMON_SG_TO_NUM = _SG_TO_NUM

# ---- X-ray characteristic wavelengths (Angstroms) ----
# Sources: International Tables Vol. C, NIST X-ray Transition Energies database
_WAVELENGTH_DB: Dict[str, Dict[str, float]] = {
    "cu": {"ka1": 1.5406, "ka2": 1.5444, "ka": 1.5418, "kb": 1.3922},
    "mo": {"ka1": 0.7093, "ka2": 0.7136, "ka": 0.7107, "kb": 0.6323},
    "co": {"ka1": 1.7890, "ka2": 1.7929, "ka": 1.7902, "kb": 1.6208},
    "fe": {"ka1": 1.9360, "ka2": 1.9400, "ka": 1.9373, "kb": 1.7566},
    "cr": {"ka1": 2.2897, "ka2": 2.2936, "ka": 2.2909, "kb": 2.0849},
    "ag": {"ka1": 0.5594, "ka2": 0.5638, "ka": 0.5608, "kb": 0.4970},
    "w": {"ka1": 0.2090, "ka2": 0.2136, "ka": 0.2104, "kb": 0.1844},
    "ni": {"ka1": 1.6579, "ka2": 1.6617, "ka": 1.6592, "kb": 1.5001},
    "mn": {"ka1": 2.1018, "ka2": 2.1058, "ka": 2.1031, "kb": 1.9102},
    "ti": {"ka1": 2.7496, "ka2": 2.7522, "ka": 2.7505, "kb": 2.5139},
    "ga": {"ka1": 1.3401, "ka2": 1.3440, "ka": 1.3414, "kb": 1.2079},
    "in": {"ka1": 0.5121, "ka2": 0.5163, "ka": 0.5135, "kb": 0.4551},
}


def _parse_lattice_value(s) -> Optional[float]:
    """Extract the leading numeric value from strings like '5.43 A' or '90.0 deg'."""
    if not s or not isinstance(s, str):
        return None
    m = re.search(r"(\d+\.?\d*)", str(s))
    return float(m.group(1)) if m else None


# ---- Value coercion + crystallographic vocabulary normalization -----------
# Phase II emits several of these fields as LISTS (e.g. space_group=["Fm-3m"],
# crystal_structure=["fcc"]) and stores structure/Bravais descriptors ("fcc",
# "bcc", "hcp", "rocksalt"...) in crystal_structure rather than one of the 7
# crystal systems.  These helpers let the deterministic checks consume the real
# pipeline data instead of silently no-op'ing on a type/vocabulary mismatch.


def _coerce_to_str(val) -> str:
    """First non-empty string from a value that may be a str or a list."""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        for x in val:
            if isinstance(x, str) and x.strip():
                return x.strip()
    return ""


def _as_str_list(val) -> List[str]:
    """Normalize a str-or-list candidate value into a list of non-empty strings."""
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    if isinstance(val, list):
        return [x.strip() for x in val if isinstance(x, str) and x.strip()]
    return []


def _parse_sg_number(val) -> int:
    """Parse an ITA space-group number (1-230) from a str/int/list value."""
    if isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return val if 1 <= val <= 230 else 0
    if isinstance(val, str):
        m = re.search(r"\d+", val)
        if m:
            n = int(m.group())
            return n if 1 <= n <= 230 else 0
    if isinstance(val, list):
        for x in val:
            n = _parse_sg_number(x)
            if n:
                return n
    return 0


def _normalize_sg_symbol(s: str) -> str:
    """Canonicalize a Hermann-Mauguin symbol: fold Unicode subscripts and the
    Unicode minus / en-dash / em-dash / overline to ASCII so inputs like
    'Pm−3m' or 'P6₃/mmc' resolve against the dash-based lookup table."""
    s = unicodedata.normalize("NFKC", s or "")
    s = (
        s.replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("‾", "")
    )
    return s.strip()


# Structure / Bravais-lattice descriptors -> the 7 (+rhombohedral) crystal
# systems.  crystal_structure from Phase II holds values like "fcc"/"bcc"/"hcp",
# NOT crystal systems, so the checks must translate before comparing.
_STRUCTURE_TO_SYSTEM: Dict[str, str] = {
    # cubic family
    "fcc": "cubic",
    "face centered cubic": "cubic",
    "face centred cubic": "cubic",
    "bcc": "cubic",
    "body centered cubic": "cubic",
    "body centred cubic": "cubic",
    "sc": "cubic",
    "simple cubic": "cubic",
    "primitive cubic": "cubic",
    "diamond": "cubic",
    "diamond cubic": "cubic",
    "isometric": "cubic",
    "rocksalt": "cubic",
    "rock salt": "cubic",
    "halite": "cubic",
    "nacl": "cubic",
    "zincblende": "cubic",
    "zinc blende": "cubic",
    "sphalerite": "cubic",
    "fluorite": "cubic",
    "antifluorite": "cubic",
    "spinel": "cubic",
    "perovskite": "cubic",
    "garnet": "cubic",
    "pyrochlore": "cubic",
    "bixbyite": "cubic",
    # hexagonal family
    "hcp": "hexagonal",
    "hexagonal close packed": "hexagonal",
    "wurtzite": "hexagonal",
    "graphite": "hexagonal",
    # tetragonal
    "anatase": "tetragonal",
    "rutile": "tetragonal",
    "scheelite": "tetragonal",
    # orthorhombic
    "brookite": "orthorhombic",
    "aragonite": "orthorhombic",
    "brownmillerite": "orthorhombic",
    "olivine": "orthorhombic",
    # the crystal systems pass through unchanged
    "cubic": "cubic",
    "tetragonal": "tetragonal",
    "orthorhombic": "orthorhombic",
    "hexagonal": "hexagonal",
    "trigonal": "trigonal",
    "rhombohedral": "rhombohedral",
    "monoclinic": "monoclinic",
    "triclinic": "triclinic",
}

_CRYSTAL_SYSTEMS = {
    "cubic",
    "tetragonal",
    "orthorhombic",
    "hexagonal",
    "trigonal",
    "rhombohedral",
    "monoclinic",
    "triclinic",
}


def _normalize_crystal_system(value: str) -> str:
    """Map a crystal_structure value (a system name OR a structure/Bravais
    descriptor) onto a canonical crystal-system name.  Returns the cleaned
    lowercase string unchanged when nothing matches, so genuinely unknown values
    stay 'unresolved' rather than being forced into a wrong system."""
    v = " ".join((value or "").strip().lower().replace("-", " ").split())
    if not v:
        return ""
    if v in _STRUCTURE_TO_SYSTEM:
        return _STRUCTURE_TO_SYSTEM[v]
    tokens = set(v.split())
    # an explicit crystal-system word wins
    for sysname in _CRYSTAL_SYSTEMS:
        if sysname in tokens:
            return sysname
    # otherwise look for a known structure-type word
    for key, sysname in _STRUCTURE_TO_SYSTEM.items():
        if " " in key:
            if key in v:
                return sysname
        elif key in tokens:
            return sysname
    return v


def _systems_from_value(val) -> List[str]:
    """Distinct crystal systems implied by a crystal_structure str/list value."""
    out: List[str] = []
    for it in _as_str_list(val):
        sysname = _normalize_crystal_system(it)
        if sysname and sysname not in out:
            out.append(sysname)
    return out


def _extract_wavelength_from_radiation(radiation: str) -> str:
    """Pull a wavelength (Angstroms) out of a radiation string such as
    'Cu-Kα (λ = 1.5406 Å)'.  Phase II frequently folds the wavelength into
    `radiation` and leaves the separate radiation_wavelength field empty.
    Requires a decimal point, so 'Kα1' (the integer 1) is never mistaken for
    a wavelength."""
    if not radiation:
        return ""
    m = re.search(
        r"(?:λ|lambda|=)\s*=?\s*(\d+\.\d+)", radiation, re.IGNORECASE
    )
    if m:
        return m.group(1)
    for m in re.finditer(r"\d+\.\d+", radiation):
        v = float(m.group())
        if 0.1 <= v <= 3.0:
            return m.group()
    return ""


# ---- Standalone validation helpers (used by tools AND post-finalize check) ---


def _check_space_group(
    space_group: str, crystal_system: str, space_group_number: int = 0
) -> Dict:
    """Check a space group against its crystal system (deterministic)."""
    sg = _normalize_sg_symbol(space_group)
    claimed = _normalize_crystal_system(crystal_system)
    if not sg and not space_group_number:
        return {"status": "error", "note": "no space_group or number provided"}

    sg_num = space_group_number
    if not sg_num and sg in _COMMON_SG_TO_NUM:
        sg_num = _COMMON_SG_TO_NUM[sg]
    if not sg_num:
        sg_norm = sg.replace("_", "").replace("-", "").replace(" ", "").lower()
        for key, val in _COMMON_SG_TO_NUM.items():
            if (
                key.replace("_", "").replace("-", "").replace(" ", "").lower()
                == sg_norm
            ):
                sg_num = val
                break

    if sg_num:
        actual = _sg_number_to_system(sg_num)
        if not claimed:
            return {
                "space_group": sg,
                "number": sg_num,
                "crystal_system": actual,
                "status": "resolved",
            }
        if claimed in (actual, "rhombohedral") and actual == "trigonal":
            return {
                "status": "consistent",
                "space_group": sg,
                "number": sg_num,
                "crystal_system": actual,
            }
        if claimed == actual:
            return {
                "status": "consistent",
                "space_group": sg,
                "number": sg_num,
                "crystal_system": actual,
            }
        return {
            "status": "CONTRADICTION",
            "claimed_system": claimed,
            "actual_system": actual,
            "space_group": sg,
            "number": sg_num,
            "explanation": f"{sg} (#{sg_num}) belongs to {actual}, not {claimed}",
        }

    return {
        "status": "unresolved",
        "note": f"Space group '{sg}' not in lookup table; manual check needed",
    }


def _check_lattice_params(
    crystal_system: str, lattice_parameters: Dict
) -> Dict:
    """Check lattice parameters against the crystal system's metric rules."""
    system = _normalize_crystal_system(crystal_system)
    params = lattice_parameters or {}
    a = _parse_lattice_value(params.get("a"))
    b = _parse_lattice_value(params.get("b"))
    c = _parse_lattice_value(params.get("c"))
    alpha = _parse_lattice_value(params.get("alpha"))
    beta = _parse_lattice_value(params.get("beta"))
    gamma = _parse_lattice_value(params.get("gamma"))
    tol_l, tol_a = 0.02, 0.5
    issues: List[str] = []
    # -------- lattice range test
    # cell edges must be positive and < 100 Å
    # (kept unit-safe: valid Å ~2-60 and valid nm ~0.2-6 both pass; only
    #  garbage like 0, negatives, or absurd values like 8168 get flagged)
    for _nm, _v in [("a", a), ("b", b), ("c", c)]:
        if _v is not None and (_v <= 0 or _v > 100.0):
            issues.append(
                f"lattice {_nm}={_v} is non-physical (expected 0 < edge < 100 Å)"
            )

    if system == "cubic":
        if a and b and abs(a - b) > tol_l:
            issues.append(f"cubic requires a=b, got a={a}, b={b}")
        if b and c and abs(b - c) > tol_l:
            issues.append(f"cubic requires b=c, got b={b}, c={c}")
        for nm, v in [("alpha", alpha), ("beta", beta), ("gamma", gamma)]:
            if v and abs(v - 90.0) > tol_a:
                issues.append(f"cubic requires {nm}=90, got {v}")
    elif system == "tetragonal":
        if a and b and abs(a - b) > tol_l:
            issues.append(f"tetragonal requires a=b, got a={a}, b={b}")
        for nm, v in [("alpha", alpha), ("beta", beta), ("gamma", gamma)]:
            if v and abs(v - 90.0) > tol_a:
                issues.append(f"tetragonal requires {nm}=90, got {v}")
    elif system == "hexagonal":
        if a and b and abs(a - b) > tol_l:
            issues.append(f"hexagonal requires a=b, got a={a}, b={b}")
        for nm, v in [("alpha", alpha), ("beta", beta)]:
            if v and abs(v - 90.0) > tol_a:
                issues.append(f"hexagonal requires {nm}=90, got {v}")
        if gamma and abs(gamma - 120.0) > tol_a:
            issues.append(f"hexagonal requires gamma=120, got {gamma}")
    elif system == "trigonal":
        # Trigonal may be reported in hexagonal axes (a=b, gamma=120) OR in
        # rhombohedral axes (a=b=c, alpha=beta=gamma). Only flag if NEITHER holds,
        # otherwise a perfectly valid rhombohedral-setting cell looks like a
        # violation of the gamma=120 hexagonal rule.
        hex_ok = not (
            (a and b and abs(a - b) > tol_l)
            or (gamma and abs(gamma - 120.0) > tol_a)
        )
        rho_ok = not (
            (a and b and abs(a - b) > tol_l)
            or (b and c and abs(b - c) > tol_l)
            or (alpha and beta and abs(alpha - beta) > tol_a)
            or (beta and gamma and abs(beta - gamma) > tol_a)
        )
        if not (hex_ok or rho_ok):
            issues.append(
                "trigonal requires either hexagonal axes (a=b, gamma=120) or "
                "rhombohedral axes (a=b=c, alpha=beta=gamma)"
            )
    elif system == "orthorhombic":
        for nm, v in [("alpha", alpha), ("beta", beta), ("gamma", gamma)]:
            if v and abs(v - 90.0) > tol_a:
                issues.append(f"orthorhombic requires {nm}=90, got {v}")
    elif system == "monoclinic":
        for nm, v in [("alpha", alpha), ("gamma", gamma)]:
            if v and abs(v - 90.0) > tol_a:
                issues.append(f"monoclinic requires {nm}=90, got {v}")
    elif system == "rhombohedral":
        if a and b and abs(a - b) > tol_l:
            issues.append(f"rhombohedral requires a=b=c, got a={a}, b={b}")
        if b and c and abs(b - c) > tol_l:
            issues.append(f"rhombohedral requires a=b=c, got b={b}, c={c}")
        if alpha and beta and abs(alpha - beta) > tol_a:
            issues.append(
                f"rhombohedral requires alpha=beta=gamma, got alpha={alpha}, beta={beta}"
            )
    parsed = {
        "a": a,
        "b": b,
        "c": c,
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
    }
    if not issues:
        return {
            "status": "consistent",
            "crystal_system": system,
            "parsed": parsed,
        }
    return {
        "status": "VIOLATION",
        "issues": issues,
        "crystal_system": system,
        "parsed": parsed,
    }


def _check_wavelength(radiation: str, wavelength: str) -> Dict:
    """Check a radiation label against its expected X-ray wavelength."""
    rad_lower = (
        (radiation or "").lower().replace("α", "a").replace("β", "b").strip()
    )
    wl_val = _parse_lattice_value(wavelength)
    if not wl_val:
        return {
            "status": "no_wavelength",
            "note": "Could not parse wavelength value",
        }

    # --- sanity check: wavelength unit (Å vs nm) --- #
    # had some issues with it, so decided to add this
    wl_str = (wavelength or "").lower()
    if "nm" in wl_str and "pm" not in wl_str:  # value stated in nanometres
        if wl_val < 0.35:  # e.g. 0.15406 nm -> valid, convert to Å
            wl_val = wl_val * 10.0
        else:  # e.g. 1.5406 nm -> impossible for X-rays
            return {
                "status": "UNIT_ERROR",
                "reported": wavelength,
                "note": (
                    "Wavelength looks mislabeled: X-ray wavelengths are "
                    "~0.5-2.5 Å (0.05-0.25 nm); '%s' is ~10x too large for nm."
                    % wavelength
                ),
            }

    element = ""
    for el in _WAVELENGTH_DB:
        if el in rad_lower:
            element = el
            break
    if not element:
        return {
            "status": "unknown_source",
            "note": f"Radiation '{radiation}' not in database",
        }
    line = ""
    for ln in ("ka1", "ka2", "ka", "kb"):
        if ln in rad_lower:
            line = ln
            break
    if not line:
        line = "ka1" if "1" in rad_lower else "ka"
    expected = _WAVELENGTH_DB[element].get(line, 0.0)
    tol = 0.002
    if abs(wl_val - expected) <= tol:
        return {
            "status": "consistent",
            "element": element,
            "line": line,
            "expected": expected,
            "reported": wl_val,
        }
    closest_line, closest_val, closest_dist = "", 0.0, 999.0
    for ln, val in _WAVELENGTH_DB[element].items():
        d = abs(wl_val - val)
        if d < closest_dist:
            closest_line, closest_val, closest_dist = ln, val, d
    if closest_dist <= tol:
        return {
            "status": "MISLABELED",
            "claimed_line": line,
            "actual_line": closest_line,
            "reported": wl_val,
            "expected_for_claimed": expected,
            "matches": closest_val,
            "explanation": (
                f"Value {wl_val} matches {element.upper()} {closest_line} "
                f"({closest_val}), not {line} ({expected})"
            ),
        }
    return {
        "status": "MISMATCH",
        "element": element,
        "line": line,
        "expected": expected,
        "reported": wl_val,
        "difference": round(abs(wl_val - expected), 4),
        "note": "Reported wavelength does not match any known line for this source",
    }


def _post_finalize_consistency_check(
    verified_fields: Dict, candidate_block: Dict
) -> Dict:
    """Deterministic safety net — runs crystallographic consistency checks after
    the agent finalizes, regardless of whether the agent remembered to call them.

    Why this exists:
    1. Budget exhaustion — the agent may run out of steps before reaching checks.
    2. LLM non-determinism — even with 'ALWAYS run these', the model may skip them.
    3. Hallucination resistance — the LLM might accept 'Fm-3m + tetragonal' as
       plausible.  A deterministic check catches this unconditionally.
    4. Defense in depth — these checks are pure logic (no LLM, no cost).  Running
       them as a guaranteed post-pass adds a safety layer for free.
    """
    overrides: Dict[str, Dict] = {}

    sg_list: List[str] = []
    cs_systems: List[str] = []
    lp: Dict = {}
    rad = ""
    wl = ""
    sg_number = 0

    for field, entry in verified_fields.items():
        val = entry.get("candidate_value", "")
        # NOTE: space_group and crystal_structure arrive as LISTS from Phase II
        # (e.g. ["Fm-3m"], ["fcc"]).  Reading them only when isinstance(val, str)
        # — the previous behaviour — silently disabled this whole safety net.
        if field == "space_group":
            sg_list = _as_str_list(val)
        elif field == "space_group_number":
            sg_number = _parse_sg_number(val)
        elif field == "crystal_structure":
            cs_systems = _systems_from_value(val)
        elif field == "lattice_parameters" and isinstance(val, dict):
            lp = val
        elif field == "radiation":
            rad = _coerce_to_str(val)
        elif field == "radiation_wavelength":
            wl = _coerce_to_str(val)

    # Only act on an UNAMBIGUOUS crystal system (a single distinct value).
    # Multi-phase samples (e.g. anatase + rutile) are skipped to avoid false
    # contradictions between one phase's space group and another's system.
    claimed_system = cs_systems[0] if len(cs_systems) == 1 else ""

    # Check 1: space group vs crystal system.
    # Prefer the explicit ITA number (unambiguously covers all 230); otherwise
    # use the H-M symbol when there is exactly one.
    if claimed_system and (sg_number or len(sg_list) == 1):
        sg_symbol = sg_list[0] if len(sg_list) == 1 else ""
        result = _check_space_group(sg_symbol, claimed_system, sg_number)
        if result.get("status") == "CONTRADICTION":
            for f in ("space_group", "crystal_structure"):
                if (
                    f in verified_fields
                    and verified_fields[f].get("status") == "supported"
                ):
                    overrides[f] = {
                        **verified_fields[f],
                        "status": "unsupported",
                        "evidence_span": f"POST-FINALIZE: {result['explanation']}",
                    }

    # Check 2: lattice params vs crystal system.
    if claimed_system and lp:
        result = _check_lattice_params(claimed_system, lp)
        if result.get("status") == "VIOLATION":
            if (
                "lattice_parameters" in verified_fields
                and verified_fields["lattice_parameters"].get("status")
                == "supported"
            ):
                overrides["lattice_parameters"] = {
                    **verified_fields["lattice_parameters"],
                    "status": "unsupported",
                    "evidence_span": f"POST-FINALIZE: {'; '.join(result['issues'])}",
                }

    # Check 3: radiation vs wavelength.  Phase II often folds the wavelength into
    # the radiation string and leaves radiation_wavelength empty, so fall back to
    # parsing it out of `rad`.
    wl_eff = wl or _extract_wavelength_from_radiation(rad)
    if rad and wl_eff:
        result = _check_wavelength(rad, wl_eff)
        if result.get("status") in (
            "MISMATCH",
            "MISLABELED",
            "UNIT_ERROR",
        ):  # unit_error added
            note = result.get("explanation") or result.get("note", "")
            for f in ("radiation", "radiation_wavelength"):
                if (
                    f in verified_fields
                    and verified_fields[f].get("status") == "supported"
                ):
                    overrides[f] = {
                        **verified_fields[f],
                        "status": "unsupported",
                        "evidence_span": f"POST-FINALIZE: {note}",
                    }

    if overrides:
        return {**verified_fields, **overrides}
    return verified_fields


VERIFY_PROMPT = """
Return ONLY valid JSON. No markdown.

You are verifying XRD metadata claims against PDF text context.

You are given:
1) CONTEXT: text from the paper
2) CANDIDATE_JSON: a simple clean JSON block containing XRD metadata

Task:
- For each field in CANDIDATE_JSON, determine whether it is:
  "supported", "unsupported", or "unclear"
- Keep the original field names exactly
- For each field:
  - copy the candidate value into "candidate_value"
  - return "status"
  - return "corrected_value"
  - return "evidence_span"
- Use corrected_value only if the context explicitly supports a better value
- If unsupported or unclear, corrected_value may be "" or []
- evidence_span must be an exact substring from CONTEXT
- Do not guess
- Do not use knowledge outside CONTEXT
- For lattice_parameters (dict of a, b, c, alpha, beta, gamma values): verify each sub-value
  individually. If the CONTEXT gives different values, provide the corrected dict.
- For reported_phases (list of phase names or ICDD/JCPDS refs): verify each entry.
  Remove entries not mentioned in CONTEXT, add any the candidate missed.
- For profile_function (string like "pseudo-Voigt"): verify it is explicitly mentioned in CONTEXT.
- For peak_positions_2theta (list of 2θ values): verify each position is mentioned in CONTEXT.
- For fwhm (list of FWHM values): verify each value is mentioned in CONTEXT.

Return JSON in exactly this structure:
{
  "verified_fields": {
    "<field_name>": {
      "candidate_value": "",
      "status": "supported",
      "corrected_value": "",
      "evidence_span": ""
    }
  }
}
""".strip()


def norm(s: str) -> str:
    """Normalize unicode and collapse whitespace."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u00ad", "")
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    return " ".join(s.split())


def read_pdf_pages(pdf_path: str) -> List[str]:
    """Return each PDF page's normalized text as a list."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            pages.append(txt)
    return pages


def build_xrd_focused_text(pages_text: List[str], max_chars: int) -> str:
    """Gather the XRD-focused text used as verification context."""
    blob = norm("\n".join(pages_text))
    sents = SENT_SPLIT.split(blob)
    keep = []
    for s in sents:
        sl = s.lower()
        if any(tok in sl for tok in XRD_TEXT_TOKENS):
            keep.append(s.strip())
    return "\n".join(keep)[:max_chars]


def build_global_context(pages_text: List[str], max_chars: int) -> str:
    """Build the text context for verifying paper-level metadata."""
    if USE_XRD_FOCUSED_TEXT_VERIFY:
        return build_xrd_focused_text(pages_text, max_chars=max_chars)
    return norm("\n".join(pages_text))[:max_chars]


def _blocks_from_page(doc: fitz.Document, page_i_1based: int):
    """Return a page's text blocks with positions."""
    page = doc.load_page(page_i_1based - 1)
    raw = page.get_text("blocks")
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
        if t:
            blocks.append((x0, y0, x1, y1, t))
    blocks.sort(key=lambda z: (z[1], z[0]))
    return blocks


def _find_caption_block_index(blocks, fig_num: int) -> int:
    """Find the block index of a figure's caption."""
    target = str(fig_num)
    best_i = -1
    best_score = -(10**9)

    for i, (_, y0, _, _, txt) in enumerate(blocks):
        m = FIG_CAP_RE.search(txt)
        if not m:
            continue
        if m.group(2) != target:
            continue

        score = 0
        if FIG_CAP_RE.match(txt):
            score += 10
        score += max(0, 50 - m.start())
        score -= int(y0) // 50

        if score > best_score:
            best_score = score
            best_i = i

    return best_i


def extract_figure_context(
    pdf_path: str,
    figure_number: Optional[int],
    page_number: Optional[int],
    caption: str,
    max_chars: int,
) -> str:
    """Return the caption + nearby text window for a figure."""
    doc = fitz.open(pdf_path)
    try:
        parts = []

        if isinstance(page_number, int) and page_number >= 1:
            blocks = _blocks_from_page(doc, page_number)

            if isinstance(figure_number, int):
                idx = _find_caption_block_index(blocks, figure_number)
            else:
                idx = -1

            if idx >= 0:
                local = []
                for j in range(idx, min(len(blocks), idx + 12)):
                    local.append(blocks[j][4])
                parts.append(" ".join(local))
            else:
                page = doc.load_page(page_number - 1)
                page_text = norm(page.get_text("text"))
                if caption:
                    parts.append(caption)
                parts.append(page_text[:max_chars])

        elif caption:
            parts.append(caption)

        return norm("\n".join(parts))[:max_chars]
    finally:
        doc.close()


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
    """Send a text prompt to the Phase III provider; return the reply."""
    provider = os.environ.get("VERIFY_PROVIDER", PROVIDER).strip().lower()
    model = os.environ.get("VERIFY_MODEL", MODEL).strip()
    _temperature = float(os.environ.get("TEMPERATURE", "0.0"))

    if provider == "gpt":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Missing OPENAI_API_KEY")
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        _t0 = time.time()
        resp = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt_a},
                        {"type": "input_text", "text": prompt_b},
                    ],
                }
            ],
            temperature=_temperature,
        )
        from diffai.xrdreader.usage_tracker import get_tracker

        get_tracker().log_call(
            phase="phase3",
            model=model,
            resp=resp,
            wall_seconds=time.time() - _t0,
            tool_name="call_text_llm",
        )
        return (getattr(resp, "output_text", "") or "").strip()

    if provider == "grok":
        if not os.environ.get("XAI_API_KEY"):
            raise RuntimeError("Missing XAI_API_KEY")
        from openai import OpenAI

        client = OpenAI(
            api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1"
        )
        resp = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt_a},
                        {"type": "input_text", "text": prompt_b},
                    ],
                }
            ],
            temperature=_temperature,
        )
        return (getattr(resp, "output_text", "") or "").strip()

    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "generationConfig": {
                "temperature": _temperature,
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
            "phase3",
            model,
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
            raise RuntimeError("Missing ANTHROPIC_API_KEY")
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 4000,
            "temperature": _temperature,
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
            "phase3",
            model,
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
            raise RuntimeError("Missing TOGETHER_API_KEY")
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key, base_url="https://api.together.xyz/v1"
        )
        _t0 = time.time()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt_a}\n\n{prompt_b}",
                }
            ],
            temperature=_temperature,
        )
        from diffai.xrdreader.usage_tracker import get_tracker

        _tracker = get_tracker()
        usage = getattr(resp, "usage", None)
        _in = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
        _out = getattr(usage, "completion_tokens", 0) or 0 if usage else 0
        _tracker.log_call(
            phase="phase3",
            model=model,
            input_tokens=_in,
            output_tokens=_out,
            wall_seconds=time.time() - _t0,
            tool_name="call_text_llm",
        )
        return (resp.choices[0].message.content or "").strip()

    raise ValueError(f"Unsupported VERIFY_PROVIDER: {provider}")


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


def verify_block_with_llm(candidate_block: Dict, context_text: str) -> Dict:
    """Ask the LLM to verify one metadata block against the text."""
    payload = (
        "CONTEXT:\n"
        f"{context_text}\n\n"
        "CANDIDATE_JSON:\n"
        f"{json.dumps(candidate_block, ensure_ascii=False)}\n"
    )

    txt = call_text_llm(VERIFY_PROMPT, payload)
    raw_json = _extract_json_object(txt)
    if not raw_json:
        raise ValueError("LLM did not return a valid JSON object")

    try:
        out = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"LLM returned invalid JSON: {e}")

    verified_fields = out.get("verified_fields", {})
    if not isinstance(verified_fields, dict):
        verified_fields = {}

    cleaned = {}
    for field, candidate_value in candidate_block.items():
        entry = verified_fields.get(field, {})
        if not isinstance(entry, dict):
            entry = {}

        status = entry.get("status", "unclear")
        if status not in {"supported", "unsupported", "unclear"}:
            status = "unclear"

        corrected_value = entry.get(
            "corrected_value", "" if isinstance(candidate_value, str) else []
        )
        evidence_span = entry.get("evidence_span", "")

        cleaned[field] = {
            "candidate_value": candidate_value,
            "status": status,
            "corrected_value": corrected_value,
            "evidence_span": evidence_span,
        }

    return {"verified_fields": cleaned}


# ======================================================================================
# Agentic verification loop — tools + perceive-act cycle for Phase 3 audit
# ======================================================================================


def _sleep_backoff_p3(attempt: int):
    """Sleep with exponential backoff + jitter before a retry."""
    time.sleep(1.0 * (2**attempt) + random.random() * 0.25)


def _safe_json_loads_p3(s: str) -> Dict:
    """Parse the first JSON object in `s`; return {} on any failure."""
    try:
        obj = _extract_json_object(s)
        return json.loads(obj) if obj else {}
    except Exception:
        return {}


class Phase3AgentToolbox:
    """
    Holds the open PDF + extracted page texts and exposes tools for the audit agent.
    """

    def __init__(
        self,
        doc: fitz.Document,
        pages_text: List[str],
        pdf_path: str = "",
        crop_dir: str = "",
    ):
        self.doc = doc
        self.pages_text = pages_text
        self.n_pages = len(pages_text)
        self.pdf_path = pdf_path
        self.crop_dir = crop_dir

    # ---- tool: search_pdf ---------------------------------------------------
    def tool_search_pdf(
        self, pattern: str, max_hits: int = 8, snippet_chars: int = 180
    ) -> str:
        """Regex-search the PDF text; return (page, snippet) hits as JSON."""
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
        corpus = build_xrd_focused_text(self.pages_text, max_chars=max_chars)
        if not corpus.strip():
            return "(no XRD-related text found in methods/experimental)"
        return corpus

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
                blocks = _blocks_from_page(self.doc, p_idx + 1)
                idx = _find_caption_block_index(blocks, fig_num)
                if idx >= 0:
                    local = []
                    for j in range(idx, min(len(blocks), idx + 12)):
                        local.append(blocks[j][4])
                    return " ".join(local)[:3000]
        except Exception as e:
            return f"(error: {e})"
        return f"(Figure {fig_num} caption not found)"

    # ---- tool: get_table_text -----------------------------------------------
    def tool_get_table_text(self, page: int = 0) -> str:
        """Extract tables from PDF using pdfplumber. page=0 means all pages."""
        if not self.pdf_path:
            return "(error: pdf_path not available)"
        try:
            tables_out = []
            with pdfplumber.open(self.pdf_path) as pdf:
                if 1 <= page <= len(pdf.pages):
                    target_pages = [pdf.pages[page - 1]]
                else:
                    target_pages = pdf.pages
                for p in target_pages:
                    raw_tables = p.extract_tables()
                    for ti, tbl in enumerate(raw_tables):
                        if not tbl:
                            continue
                        rows_str = []
                        for row in tbl:
                            cells = [str(c or "").strip() for c in row]
                            rows_str.append(" | ".join(cells))
                        tables_out.append(
                            f"[Page {p.page_number}, Table {ti + 1}]\n"
                            + "\n".join(rows_str)
                        )
            if not tables_out:
                return (
                    "(no tables found)"
                    if page == 0
                    else f"(no tables on page {page})"
                )
            return "\n\n".join(tables_out)[:5000]
        except Exception as e:
            return f"(error extracting tables: {e})"

    # ---- tool: get_references_section ---------------------------------------
    def tool_get_references_section(self, max_chars: int = 3000) -> str:
        """Return the References / Bibliography section text."""
        full = "\n".join(self.pages_text)
        m = re.search(r"\n\s*(References|Bibliography|REFERENCES)\s*\n", full)
        if not m:
            return "(References section not found)"
        return full[m.start() :][:max_chars]

    # ---- tool: validate_space_group -----------------------------------------
    def tool_validate_space_group(
        self,
        space_group: str,
        crystal_system: str,
        space_group_number: int = 0,
    ) -> str:
        """Tool: check a space group vs its crystal system."""
        return json.dumps(
            _check_space_group(space_group, crystal_system, space_group_number)
        )

    # ---- tool: validate_lattice_params --------------------------------------
    def tool_validate_lattice_params(
        self, crystal_system: str, lattice_parameters: Dict
    ) -> str:
        """Tool: check lattice params vs crystal-system metric rules."""
        return json.dumps(
            _check_lattice_params(crystal_system, lattice_parameters)
        )

    # ---- tool: check_wavelength_consistency ---------------------------------
    def tool_check_wavelength_consistency(
        self, radiation: str, wavelength: str
    ) -> str:
        """Tool: check a radiation label vs its expected wavelength."""
        return json.dumps(_check_wavelength(radiation, wavelength))

    # ---- tool: view_crop (T2 vision) ----------------------------------------
    def tool_view_crop(self, fig_num: int) -> Tuple[str, Optional[Dict]]:
        """Return the Phase I crop image for a figure. Returns (text, image_dict)."""
        if not self.crop_dir or not Path(self.crop_dir).is_dir():
            return "(crop directory not available)", None

        crop_dir = Path(self.crop_dir)
        candidates: List[Path] = []
        for pat in [
            f"*fig{fig_num}[_. ]*",
            f"*figure{fig_num}[_. ]*",
            f"*fig_{fig_num}_*",
            f"*_f{fig_num}_*",
            f"*_f{fig_num}.*",
        ]:
            candidates.extend(crop_dir.glob(pat + ".png"))
            candidates.extend(crop_dir.glob(pat + ".jpg"))
        if not candidates:
            for f in sorted(crop_dir.glob("*.png")) + sorted(
                crop_dir.glob("*.jpg")
            ):
                if (
                    f"fig{fig_num}" in f.stem.lower()
                    or f"figure{fig_num}" in f.stem.lower()
                ):
                    candidates.append(f)
        if not candidates:
            return (
                f"(no crop image found for Figure {fig_num} in {crop_dir.name}/)",
                None,
            )

        img_path = candidates[0]
        img_bytes = img_path.read_bytes()
        b64 = base64.b64encode(img_bytes).decode("ascii")
        media = (
            "image/png" if img_path.suffix.lower() == ".png" else "image/jpeg"
        )
        return (
            f"[Crop image for Figure {fig_num}: {img_path.name}, {len(img_bytes) // 1024}KB]",
            {"data_base64": b64, "media_type": media},
        )

    # ---- tool: view_full_page (T2 vision) -----------------------------------
    def tool_view_full_page(self, page: int) -> Tuple[str, Optional[Dict]]:
        """Render a PDF page as PNG at 150 DPI. Returns (text, image_dict)."""
        if page < 1 or page > self.doc.page_count:
            return f"(page {page} out of range 1..{self.doc.page_count})", None
        pix = self.doc.load_page(page - 1).get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return (
            f"[Full page {page}: {pix.width}x{pix.height}px, {len(img_bytes) // 1024}KB]",
            {"data_base64": b64, "media_type": "image/png"},
        )


PHASE3_AGENT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_pdf",
        "description": (
            "Regex search across the full PDF text. Returns up to 8 (page, snippet) matches. "
            "Use targeted patterns like 'Cu.*Kα|radiation', 'step.*size|0\\.0\\d+', "
            "'space.*group|Fm.3m', 'crystal.*structure|fcc|bcc|hcp'."
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
            "Return XRD-related sentences from the full paper. "
            "Start here — scan parameters, radiation source, and software are usually reported in methods."
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
        "description": "Return the caption and nearby text blocks for a specific figure number.",
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
    # ---- T1 validation tools ----
    {
        "type": "function",
        "name": "get_table_text",
        "description": (
            "Extract tables from the PDF (pdfplumber). Returns pipe-delimited rows. "
            "Crystallographic data (lattice params, R-factors, occupancies) often live "
            "in tables that flat text garbles. Pass page=0 for all pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Page number (1-indexed). 0 = all pages.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_references_section",
        "description": (
            "Return the References / Bibliography section. Useful for cross-checking "
            "ICDD card numbers, COD/ICSD IDs, and cited CIF sources."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "validate_space_group",
        "description": (
            "Check whether the claimed space group is compatible with the claimed "
            "crystal system. Returns 'consistent' or 'CONTRADICTION' with the correct system. "
            "Covers all 230 ITA space groups: full Hermann-Mauguin symbol table "
            "(with common alternate settings) plus 1-230 number lookup. The crystal "
            "system may be given as a structure type (e.g. fcc, bcc, hcp) — it is "
            "normalized to the matching system automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "space_group": {
                    "type": "string",
                    "description": "H-M symbol (e.g. Fm-3m, P6_3/mmc)",
                },
                "crystal_system": {
                    "type": "string",
                    "description": "Claimed system (e.g. cubic, hexagonal)",
                },
                "space_group_number": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "IT number 1-230 if known (optional)",
                },
            },
            "required": ["space_group", "crystal_system"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "validate_lattice_params",
        "description": (
            "Check metric constraints for the crystal system. E.g. cubic requires "
            "a=b=c and all angles=90. Returns 'consistent' or 'VIOLATION' with specifics."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "crystal_system": {"type": "string"},
                "lattice_parameters": {
                    "type": "object",
                    "description": "Dict with keys a, b, c, alpha, beta, gamma (string values like '5.43 A').",
                },
            },
            "required": ["crystal_system", "lattice_parameters"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "check_wavelength_consistency",
        "description": (
            "Verify that the reported wavelength matches the radiation source. "
            "E.g. Cu Ka1 should be 1.5406 A. Detects mismatches and mislabeled lines."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "radiation": {
                    "type": "string",
                    "description": "Radiation type (e.g. 'Cu Ka', 'Mo Ka1')",
                },
                "wavelength": {
                    "type": "string",
                    "description": "Reported wavelength (e.g. '1.5406 A')",
                },
            },
            "required": ["radiation", "wavelength"],
            "additionalProperties": False,
        },
    },
    # ---- T2 vision tools ----
    {
        "type": "function",
        "name": "view_crop",
        "description": (
            "Show the Phase I crop image for an XRD figure. Use when text evidence "
            "is insufficient — the image reveals peaks, legend labels, axis ranges, "
            "Miller-index annotations, and ICDD stick-pattern overlays that may not "
            "appear in the paper text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fig_num": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Figure number to view",
                },
            },
            "required": ["fig_num"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "view_full_page",
        "description": (
            "Render a full PDF page as an image (150 DPI). Use when you need layout "
            "context: nearby tables, multi-panel figures, or caption positions that "
            "flat text extraction may lose."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Page number (1-indexed)",
                },
            },
            "required": ["page"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "finalize",
        "description": (
            "Submit the verification verdict for each field and END the loop. "
            "For each field in the candidate, report status (supported/unsupported/unclear), "
            "corrected_value (if evidence supports a better value), and evidence_span "
            "(exact substring from the PDF text)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verified_fields": {
                    "type": "object",
                    "description": (
                        "Dict mapping each candidate field name to "
                        "{candidate_value, status, corrected_value, evidence_span}."
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "candidate_value": {},
                            "status": {
                                "type": "string",
                                "enum": [
                                    "supported",
                                    "unsupported",
                                    "unclear",
                                ],
                            },
                            "corrected_value": {},
                            "evidence_span": {"type": "string"},
                        },
                        "required": [
                            "candidate_value",
                            "status",
                            "corrected_value",
                            "evidence_span",
                        ],
                    },
                },
            },
            "required": ["verified_fields"],
            "additionalProperties": False,
        },
    },
]


PHASE3_AGENT_SYSTEM_PROMPT = """
You are an XRD metadata audit agent. You are the FINAL quality gate before data is published.

You are given a CANDIDATE_JSON block containing XRD metadata fields extracted from earlier
pipeline stages. Your job: independently verify each field against the actual PDF text,
then run crystallographic consistency checks on the values.

For each field:
- "supported": the PDF text contains this value or a close match. Provide evidence_span.
- "unsupported": the PDF explicitly contradicts this value OR it appears nowhere in the paper.
- "unclear": the PDF mentions related terms but the specific value is ambiguous or conflicting.
- corrected_value: if the PDF or a consistency check supports a BETTER value, provide it.
- evidence_span: exact substring from the PDF text. Required for "supported" status.

Strategy — follow this order:
1. TEXT SEARCH: Start with get_methods_text — most XRD parameters are in Methods/Experimental.
2. TABLES: Call get_table_text — lattice params, R-factors, and occupancies often live in
   tables that flat text garbles. This is critical for verifying numeric values.
3. TARGETED SEARCH: For fields still unclear, use search_pdf with a targeted regex.
4. PAGE CONTEXT: Use get_page_text to read context around a search hit.
5. FIGURE CONTEXT: Use get_figure_context for figure-specific fields (captions, annotations).
6. REFERENCES: Use get_references_section to cross-check ICDD/JCPDS card numbers and COD/ICSD IDs.
7. CONSISTENCY CHECKS — run these on every candidate that has the relevant fields:
   - validate_space_group: check space group vs crystal system compatibility.
   - validate_lattice_params: check metric constraints (e.g. cubic a=b=c, angles=90).
   - check_wavelength_consistency: check radiation type vs wavelength value.
   If a consistency check returns CONTRADICTION, VIOLATION, or MISMATCH, mark the
   conflicting field as "unsupported" and provide the check result as evidence_span.
8. VISION (only if text evidence is insufficient for a field):
   - view_crop: see the actual XRD pattern — peaks, legend labels, axis ranges,
     Miller-index annotations, ICDD stick-pattern overlays.
   - view_full_page: see layout context — nearby tables, multi-panel figures.
   Use vision sparingly — it costs image tokens.  Prefer text tools first.
9. Call finalize once every field has a verdict.

Field-specific guidance:
- lattice_parameters: dict like {{"a": "5.43 A", "c": "4.95 A"}}. Search for
  "a =", "b =", "c =", "alpha", "beta", "gamma", "unit cell". Also call get_table_text
  because lattice params are almost always in a table.
- reported_phases: list of phase names or ICDD/JCPDS refs. Use search_pdf with
  "ICDD|JCPDS|PDF.*\\d{{2}}" and get_references_section for card numbers.
- profile_function: e.g. "pseudo-Voigt". Search "profile|Voigt|Pearson|Gaussian|TCH".
- peak_positions_2theta: list of 2theta values. Search "peak.*at|2.*=|reflection".
- fwhm: full-width-at-half-maximum. Search "FWHM|full width".
- space_group + crystal_structure: ALWAYS run validate_space_group on these two fields together.
- lattice_parameters + crystal_structure: ALWAYS run validate_lattice_params.
- radiation + radiation_wavelength: ALWAYS run check_wavelength_consistency.

Rules:
- Do NOT use outside knowledge. Only use what the PDF says + consistency checks.
- Do NOT guess. If uncertain, mark "unclear" rather than "supported".
- Budget: {max_steps} tool calls. Be thorough but efficient.
""".strip()


def _execute_phase3_tool(
    toolbox: Phase3AgentToolbox, name: str, args: Dict
) -> Tuple[str, Optional[Dict]]:
    """Execute a Phase 3 tool. Returns (text_result, optional_image_dict)."""
    try:
        # Vision tools return (str, image_dict)
        if name == "view_crop":
            return toolbox.tool_view_crop(int(args.get("fig_num", 0)))
        if name == "view_full_page":
            return toolbox.tool_view_full_page(int(args.get("page", 0)))
        # All other tools return str only -> wrap as (str, None)
        if name == "search_pdf":
            return toolbox.tool_search_pdf(str(args.get("pattern", ""))), None
        if name == "get_methods_text":
            return toolbox.tool_get_methods_text(), None
        if name == "get_page_text":
            return toolbox.tool_get_page_text(int(args.get("page", 0))), None
        if name == "get_figure_context":
            return (
                toolbox.tool_get_figure_context(int(args.get("fig_num", 0))),
                None,
            )
        if name == "get_table_text":
            return toolbox.tool_get_table_text(int(args.get("page", 0))), None
        if name == "get_references_section":
            return toolbox.tool_get_references_section(), None
        if name == "validate_space_group":
            return (
                toolbox.tool_validate_space_group(
                    str(args.get("space_group", "")),
                    str(args.get("crystal_system", "")),
                    int(args.get("space_group_number", 0)),
                ),
                None,
            )
        if name == "validate_lattice_params":
            return (
                toolbox.tool_validate_lattice_params(
                    str(args.get("crystal_system", "")),
                    args.get("lattice_parameters", {}),
                ),
                None,
            )
        if name == "check_wavelength_consistency":
            return (
                toolbox.tool_check_wavelength_consistency(
                    str(args.get("radiation", "")),
                    str(args.get("wavelength", "")),
                ),
                None,
            )
        return f"(error: unknown tool '{name}')", None
    except Exception as e:
        return f"(error: tool {name} failed: {e})", None


def _normalize_verified_fields(
    raw_fields: Dict, candidate_block: Dict
) -> Dict:
    """Normalize the finalize output to match verify_block_with_llm's schema."""
    cleaned = {}
    for field, candidate_value in candidate_block.items():
        entry = raw_fields.get(field, {})
        if not isinstance(entry, dict):
            entry = {}

        status = entry.get("status", "unclear")
        if status not in {"supported", "unsupported", "unclear"}:
            status = "unclear"

        corrected_value = entry.get(
            "corrected_value", "" if isinstance(candidate_value, str) else []
        )
        evidence_span = entry.get("evidence_span", "")
        if not isinstance(evidence_span, str):
            evidence_span = ""

        cleaned[field] = {
            "candidate_value": candidate_value,
            "status": status,
            "corrected_value": corrected_value,
            "evidence_span": evidence_span,
        }
    return cleaned


def run_phase3_verify_agent(
    candidate_block: Dict,
    doc: fitz.Document,
    pages_text: List[str],
    scope_label: str = "global",
    fig_num: Optional[int] = None,
    log_prefix: str = "",
    pdf_path: str = "",
    crop_dir: str = "",
) -> Optional[Dict]:
    """
    Agentic verification loop — works with ALL providers via ToolCaller.
    """
    provider = os.environ.get("VERIFY_PROVIDER", PROVIDER).strip().lower()
    model = os.environ.get("VERIFY_MODEL", MODEL).strip()

    from diffai.xrdreader.tool_calling import ToolCaller

    toolbox = Phase3AgentToolbox(
        doc, pages_text, pdf_path=pdf_path, crop_dir=crop_dir
    )

    caller = ToolCaller(provider, model, max_retries=PHASE3_AGENT_MAX_RETRIES)
    caller.set_system(
        PHASE3_AGENT_SYSTEM_PROMPT.format(max_steps=PHASE3_AGENT_MAX_STEPS)
    )

    cand_json = json.dumps(candidate_block, ensure_ascii=False)
    scope_hint = f"Scope: {scope_label} verification."
    if fig_num is not None:
        scope_hint += f" This is for Figure {fig_num}."

    caller.add_user_message(
        f"{scope_hint}\n\n"
        f"CANDIDATE_JSON:\n{cand_json}\n\n"
        "Search the PDF for evidence to verify or reject each field, then call finalize."
    )

    trace = []
    final = None

    for step in range(PHASE3_AGENT_MAX_STEPS):
        try:
            _t0 = time.time()
            tool_calls, _ = caller.call(PHASE3_AGENT_TOOLS)
            elapsed = time.time() - _t0

            from diffai.xrdreader.usage_tracker import get_tracker

            usage = caller.get_last_call_usage()
            get_tracker().log_call(
                phase="phase3_agent",
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                wall_seconds=elapsed,
            )
        except Exception as e:
            print(f"{log_prefix}[phase3_agent] API failed: {repr(e)}")
            break

        if not tool_calls:
            print(
                f"{log_prefix}[phase3_agent] step {step+1}: no tool calls; stopping."
            )
            break

        stop_loop = False
        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"]
            call_id = tc["call_id"]

            if name == "finalize":
                raw_fields = args.get("verified_fields", {})
                if not isinstance(raw_fields, dict):
                    raw_fields = {}
                normalized = _normalize_verified_fields(
                    raw_fields, candidate_block
                )
                final = {"verified_fields": normalized}
                trace.append(
                    {
                        "step": step + 1,
                        "tool": "finalize",
                        "args": {"field_count": len(normalized)},
                        "observation": "(loop ended)",
                    }
                )
                stop_loop = True
                break

            text_result, image = _execute_phase3_tool(toolbox, name, args)
            trace.append(
                {
                    "step": step + 1,
                    "tool": name,
                    "args": args,
                    "observation": text_result[:2000],
                }
            )
            if image:
                caller.add_tool_result_with_image(
                    call_id,
                    name,
                    text_result,
                    image["data_base64"],
                    image.get("media_type", "image/png"),
                )
            else:
                caller.add_tool_result(call_id, name, text_result)

        if stop_loop:
            break

    steps_used = len(trace)
    if final is None:
        print(
            f"{log_prefix}[phase3_agent] budget exhausted after {steps_used} steps."
        )
        fallback_fields = {}
        for field, candidate_value in candidate_block.items():
            fallback_fields[field] = {
                "candidate_value": candidate_value,
                "status": "unclear",
                "corrected_value": (
                    "" if isinstance(candidate_value, str) else []
                ),
                "evidence_span": "",
            }
        final = {"verified_fields": fallback_fields}

    # ---- Post-finalize deterministic safety net ----
    # Runs AFTER the agentic loop above (whether it finalized or exhausted its
    # budget), unconditionally and at zero LLM cost. This is the guaranteed
    # crystallographic consistency pass — it does not depend on the agent having
    # remembered to call the validate_* tools.
    final["verified_fields"] = _post_finalize_consistency_check(
        final["verified_fields"],
        candidate_block,
    )

    final["agent_trace"] = trace
    final["steps_used"] = steps_used
    return final


def choose_final_value(entry: Dict):
    """Pick the final value for a field (verified over candidate)."""
    corrected = entry.get("corrected_value")
    candidate = entry.get("candidate_value")

    if corrected not in ("", [], None):
        return corrected
    return candidate


def build_supported_only(verified_fields: Dict) -> Dict:
    """Keep only the fields the LLM marked as supported."""
    out = {}
    for field, entry in verified_fields.items():
        if entry.get("status") in {"supported", "unclear"}:
            out[field] = choose_final_value(entry)
    return out


def build_log_lists(verified_fields: Dict) -> Dict:
    """Split results into supported/unsupported/corrected lists."""
    verified = {}
    unverified = {}

    for field, entry in verified_fields.items():
        item = {
            "candidate_value": entry.get("candidate_value"),
            "corrected_value": entry.get("corrected_value"),
            "evidence_span": entry.get("evidence_span", ""),
            "status": entry.get("status", "unclear"),
        }

        if entry.get("status") == "supported":
            verified[field] = item
        else:
            unverified[field] = item

    return {
        "verified": verified,
        "unverified": unverified,
    }


def verify_global_block(
    clean_json: Dict,
    pages_text: List[str],
    doc: Optional[fitz.Document] = None,
    pdf_path: str = "",
    crop_dir: str = "",
    log_prefix: str = "",
) -> Dict:
    """Verify the paper-level (global) metadata block."""
    candidate = clean_json.get("global", {}) or {}
    if not candidate:
        return {
            "final_supported": {},
            "log": {"verified": {}, "unverified": {}},
        }

    verification = None

    if doc is not None:
        verification = run_phase3_verify_agent(
            candidate,
            doc,
            pages_text,
            scope_label="global (methods)",
            log_prefix=log_prefix,
            pdf_path=pdf_path,
            crop_dir=crop_dir,
        )

    if verification is None:
        context_text = build_global_context(
            pages_text, max_chars=MAX_GLOBAL_CONTEXT_CHARS_VERIFY
        )
        verification = verify_block_with_llm(candidate, context_text)

    verified_fields = verification["verified_fields"]
    agent_trace = verification.get("agent_trace", [])
    steps_used = verification.get("steps_used", 0)

    return {
        "final_supported": build_supported_only(verified_fields),
        "log": build_log_lists(verified_fields),
        "agent_trace": agent_trace,
        "steps_used": steps_used,
    }


def verify_figure_block(
    fig: Dict,
    pdf_path: str,
    doc: Optional[fitz.Document] = None,
    pages_text: Optional[List[str]] = None,
    crop_dir: str = "",
    log_prefix: str = "",
) -> Dict:
    """Verify one figure's metadata block."""
    candidate = fig.get("xrd_metadata", {}) or {}
    figure_number = fig.get("figure_number")
    page_number = fig.get("page")
    caption = fig.get("caption", "")

    figure_id = fig.get("figure_id", "")

    if not candidate:
        return {
            "figure_id": figure_id,
            "figure_number": figure_number,
            "page": page_number,
            "caption": caption,
            "final_supported": {},
            "log": {"verified": {}, "unverified": {}},
        }

    verification = None

    if doc is not None and pages_text is not None:
        verification = run_phase3_verify_agent(
            candidate,
            doc,
            pages_text,
            scope_label="figure",
            fig_num=figure_number if isinstance(figure_number, int) else None,
            log_prefix=log_prefix,
            pdf_path=pdf_path,
            crop_dir=crop_dir,
        )

    if verification is None:
        context_text = extract_figure_context(
            pdf_path=pdf_path,
            figure_number=figure_number,
            page_number=page_number,
            caption=caption,
            max_chars=MAX_FIGURE_CONTEXT_CHARS_VERIFY,
        )
        verification = verify_block_with_llm(candidate, context_text)

    verified_fields = verification["verified_fields"]
    agent_trace = verification.get("agent_trace", [])
    steps_used = verification.get("steps_used", 0)

    return {
        "figure_id": figure_id,
        "figure_number": figure_number,
        "page": page_number,
        "caption": caption,
        "final_supported": build_supported_only(verified_fields),
        "log": build_log_lists(verified_fields),
        "agent_trace": agent_trace,
        "steps_used": steps_used,
    }


def verify_one_clean_json(input_clean_json: Path):
    """Verify one `*__phase2_clean.json` -> `*__verified_final.json`."""
    try:
        clean_json = json.loads(input_clean_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[FAIL] Corrupt JSON file {input_clean_json.name}: {e}")
        return
    pdf_path = clean_json.get("pdf", "")

    if not pdf_path:
        raise ValueError(f"Missing pdf path in clean JSON: {input_clean_json}")

    from diffai.xrdreader.usage_tracker import set_current_pdf

    set_current_pdf(Path(str(pdf_path)).name)

    pages_text = read_pdf_pages(pdf_path)

    # Derive the crop directory from the PDF path (Phase I saves crops here)
    safe_stem = make_safe_stem(Path(str(pdf_path)).stem)
    crop_dir = str(PHASE1_DIR / f"{safe_stem}_xrd")

    # Open fitz doc for the agent toolbox; pass None when agentic mode is off
    # so verify functions fall through to single-shot verify_block_with_llm()
    doc = None
    if ENABLE_AGENTIC_PHASE3:
        doc = fitz.open(pdf_path)
    try:
        mode = "agentic" if ENABLE_AGENTIC_PHASE3 else "single-shot"
        log_prefix = f"[{input_clean_json.name}] "
        print(f"{log_prefix}Phase 3 mode: {mode}")
        global_result = verify_global_block(
            clean_json,
            pages_text,
            doc=doc,
            pdf_path=pdf_path,
            crop_dir=crop_dir,
            log_prefix=log_prefix,
        )

        final_json = {
            "pdf": clean_json.get("pdf", pdf_path),
            "title": clean_json.get("title", ""),
            "doi": clean_json.get("doi", ""),
            "author": clean_json.get("author", ""),
            "source_type": clean_json.get("source_type", ""),
            "arxiv_id": clean_json.get("arxiv_id", ""),
            "doi_source": clean_json.get("doi_source", ""),
            "published_doi": clean_json.get("published_doi", ""),
            "methods": global_result["final_supported"],
            "figures": [],
            "main_material": clean_json.get("main_material", ""),
        }

        log_json = {
            "pdf": clean_json.get("pdf", pdf_path),
            "title": clean_json.get("title", ""),
            "doi": clean_json.get("doi", ""),
            "author": clean_json.get("author", ""),
            "source_type": clean_json.get("source_type", ""),
            "arxiv_id": clean_json.get("arxiv_id", ""),
            "doi_source": clean_json.get("doi_source", ""),
            "published_doi": clean_json.get("published_doi", ""),
            "methods": global_result["log"],
            "methods_agent_trace": global_result.get("agent_trace", []),
            "methods_steps_used": global_result.get("steps_used", 0),
            "figures": [],
            "main_material": clean_json.get("main_material", ""),
        }

        for fig in clean_json.get("figures", []):
            fig_log_prefix = f"[{input_clean_json.name} fig{fig.get('figure_number', '?')}] "
            fig_result = verify_figure_block(
                fig,
                pdf_path,
                doc=doc,
                pages_text=pages_text,
                crop_dir=crop_dir,
                log_prefix=fig_log_prefix,
            )

            final_json["figures"].append(
                {
                    "figure_number": fig_result["figure_number"],
                    "page": fig_result["page"],
                    "caption": fig_result["caption"],
                    "material": fig.get("material", ""),
                    "xrd_metadata": fig_result["final_supported"],
                    "plot_info": fig.get("plot_info", {}),
                }
            )

            log_json["figures"].append(
                {
                    "figure_number": fig_result["figure_number"],
                    "page": fig_result["page"],
                    "caption": fig_result["caption"],
                    "material": fig.get("material", ""),
                    "plot_info": fig.get("plot_info", {}),
                    "verified": fig_result["log"]["verified"],
                    "unverified": fig_result["log"]["unverified"],
                    "agent_trace": fig_result.get("agent_trace", []),
                    "steps_used": fig_result.get("steps_used", 0),
                }
            )

    finally:
        if doc is not None:
            doc.close()

    pdf_value = clean_json.get("pdf", "")
    pdf_name = (
        Path(str(pdf_value)).stem
        if str(pdf_value).strip()
        else input_clean_json.stem
    )
    safe_base = make_safe_stem(pdf_name)

    output_dir = input_clean_json.parent
    output_final_json = (
        output_dir / f"{safe_base}__phase3_validated_FINAL.json"
    )
    output_log_json = output_dir / f"{safe_base}__phase3_validation_log.json"

    output_final_json.parent.mkdir(parents=True, exist_ok=True)
    output_log_json.parent.mkdir(parents=True, exist_ok=True)

    output_final_json.write_text(
        json.dumps(final_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    output_log_json.write_text(
        json.dumps(log_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"[OK] Final JSON written to: {output_final_json}")
    print(f"[OK] Log JSON written to: {output_log_json}")


def main():
    """Verify every `*__phase2_clean.json` in the run folder."""
    if not PHASE1_DIR.exists():
        raise FileNotFoundError(f"PHASE1_DIR not found: {PHASE1_DIR}")

    input_files = sorted(PHASE1_DIR.glob("*__phase2_clean.json"))
    if not input_files:
        raise FileNotFoundError(
            f"No *__phase2_clean.json found in: {PHASE1_DIR}"
        )

    for input_clean_json in input_files:
        try:
            verify_one_clean_json(input_clean_json)
        except Exception as e:
            print(f"[FAIL] {input_clean_json.name}: {repr(e)}")

    from diffai.xrdreader.usage_tracker import set_current_pdf as _clear_pdf

    _clear_pdf()


if __name__ == "__main__":
    main()
