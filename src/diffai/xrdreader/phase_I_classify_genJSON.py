"""Phase I -- figure detection + XRD classification (vision LLM).

For each kept PDF, finds candidate figures (embedded rasters + rendered vector
regions), then uses a vision model -- optionally an agentic tool loop
(Phase1AgentToolbox) -- to decide which figures are XRD and whether they match
the paper's main material. Also pulls basic metadata (title, material, arXiv /
CrossRef ids). Writes `*__phase1_raw.json` plus the figure crops per PDF.
"""

# ======================================================================================
# 0) Imports
# ======================================================================================
import base64
import difflib
import hashlib
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import numpy as np
import pdfplumber
from openai import OpenAI
from PIL import Image, ImageDraw

from diffai.xrdreader.utils import make_safe_stem

logging.getLogger("pdfminer").setLevel(logging.ERROR)

# ======================================================================================
# 1) EDIT HERE: Paths + knobs
# ======================================================================================
from diffai.xrdreader.config import (  # noqa: E402
    BASE_SLEEP_SEC,
    DPI_CROP,
    DPI_PAGE,
    ENABLE_AGENTIC_PHASE1,
    GRID,
    KEEP_ONLY_MATCHED_AND_XRD,
    MAX_API_RETRIES,
    MAX_CROP_AREA_FRAC,
    MAX_VEC_PAGE_FRAC,
    MERGE_IOU_THR,
    MERGE_PAD_PX,
    MIN_COMPONENT_AREA,
    MIN_CROP_AREA_FRAC,
    OUT_ROOT,
    PDF_DIR,
    PHASE1_INPUT_PDF_DIR,
    PHASE1_MIN_AGENT_SCORE_TO_CALL,
    PHASE1_MIN_RENDERED_CROP_BYTES,
    PHASE1_SKIP_TEXTLESS_VECTOR_CANDIDATES,
    PHASE1_USE_CALL_CACHE,
    SAVE_DEBUG_HIGHLIGHT,
    SKIP_SIMULATED_ONLY_XRD,
)

# ======================================================================================

# Agent loop knobs (env-overridable)
PHASE1_AGENT_MAX_STEPS = int(os.getenv("PHASE1_AGENT_MAX_STEPS", "6"))
PHASE1_AGENT_MAX_RETRIES = int(os.getenv("PHASE1_AGENT_MAX_RETRIES", "3"))


def get_phase1_provider() -> str:
    return (
        os.getenv("PHASE1_PROVIDER", os.getenv("PROVIDER", "gpt"))
        .strip()
        .lower()
    )


def get_phase1_model() -> str:
    return os.getenv("PHASE1_MODEL", os.getenv("MODEL", "gpt-5.2")).strip()


def normalize_title_for_crossref(text: str) -> str:
    """Lowercase + strip punctuation from a title for matching."""
    text = (text or "").lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def title_similarity_for_crossref(a: str, b: str) -> float:
    """Fuzzy ratio (0..1) between two normalized titles."""
    na = normalize_title_for_crossref(a)
    nb = normalize_title_for_crossref(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def normalize_author_tokens_for_crossref(author_value: str):
    """Split an author string into lowercase surname tokens."""
    raw = re.split(r"[;,]|\band\b", author_value or "", flags=re.IGNORECASE)
    out = []
    for item in raw:
        item = re.sub(r"\s+", " ", item).strip()
        if not item:
            continue
        parts = item.split()
        if parts:
            out.append(parts[-1].lower())
    return out


def author_overlap_score_for_crossref(a: str, b: str) -> float:
    """Fraction of shared surname tokens between two author lists."""
    a_set = set(normalize_author_tokens_for_crossref(a))
    b_set = set(normalize_author_tokens_for_crossref(b))
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / max(len(a_set), len(b_set))


def lookup_crossref_metadata_for_pdf(
    title_value: str, author_value: str
) -> Dict[str, str]:
    """Query CrossRef by title+authors and return the best match.

    Scores candidates on title similarity + author overlap; returns
    {title, doi, author, ...} only when the top score clears 0.80, else {}.
    """
    title_value = (title_value or "").strip()
    if not title_value:
        return {}

    query_text = title_value
    if author_value:
        query_text = f"{title_value} {author_value}"

    params = {
        "query.bibliographic": query_text,
        "rows": 5,
    }
    mailto = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    if mailto and "@" in mailto:
        params["mailto"] = mailto

    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}

    items = obj.get("message", {}).get("items", []) or []
    best = {}
    best_score = 0.0

    for item in items:
        item_titles = item.get("title", []) or []
        item_title = str(item_titles[0] if item_titles else "").strip()
        author_list = item.get("author", []) or []
        item_authors = "; ".join(
            " ".join(
                filter(
                    None,
                    [
                        str(a.get("given", "")).strip(),
                        str(a.get("family", "")).strip(),
                    ],
                )
            ).strip()
            for a in author_list
            if isinstance(a, dict)
        )
        title_score = title_similarity_for_crossref(title_value, item_title)
        author_score = author_overlap_score_for_crossref(
            author_value, item_authors
        )
        total_score = 0.80 * title_score + 0.20 * author_score

        if total_score > best_score:
            best_score = total_score
            best = {
                "title": item_title or title_value,
                "doi": str(item.get("DOI", "") or "").strip(),
                "author": item_authors or author_value,
                "doi_source": "crossref_fallback",
                "match_confidence": round(total_score, 4),
            }

    if best and best.get("doi") and best_score >= 0.80:
        return best
    return {}


def extract_arxiv_id_from_text(text: str, fallback_text: str = "") -> str:
    """Find an arXiv id (e.g. 2604.00793) in text, else in the filename."""
    import re

    # Match arXiv IDs like 2604.00793 or 2604.00793v1
    m = re.search(r"\b\d{4}\.\d{4,5}(v\d+)?\b", text)
    if m:
        return m.group(0)

    # fallback: check filename/path
    m = re.search(r"\b\d{4}\.\d{4,5}(v\d+)?\b", fallback_text)
    if m:
        return m.group(0)

    return ""


MAIN_MATERIAL_PROMPT = """
Return ONLY valid JSON. No markdown.

You are given a paper title and abstract.

Task:
- Identify the main material, alloy, compound, or material system that is the primary subject of the paper.
- Expand abbreviations when possible using the provided context.
- If the paper is mainly about a multi-component system, return that system as written or clearly expanded.
- Do not return processing methods, techniques, or instruments unless they are actually the material.
- If the main material is unclear, return an empty string.

Return exactly:
{
  "main_material": ""
}
""".strip()


def extract_main_material_with_llm(
    client: Any, title: str, abstract: str
) -> str:
    """Ask the LLM for the paper's main material from title + abstract."""
    title = str(title or "").strip()
    abstract = str(abstract or "").strip()

    if not title and not abstract:
        return ""

    prompt = (
        MAIN_MATERIAL_PROMPT
        + "\n\nTITLE:\n"
        + title
        + "\n\nABSTRACT:\n"
        + abstract
    )

    provider = get_phase1_provider()
    model = get_phase1_model()

    try:
        if provider in {"gpt", "grok"}:
            _t0 = time.time()
            resp = client.responses.create(
                model=model,
                input=prompt,
            )
            from diffai.xrdreader.usage_tracker import get_tracker

            get_tracker().log_call(
                phase="phase1",
                model=model,
                resp=resp,
                wall_seconds=time.time() - _t0,
                tool_name="extract_main_material",
            )
            text = getattr(resp, "output_text", "") or ""
            obj = _safe_json_loads(text)
            if isinstance(obj, dict):
                return str(obj.get("main_material", "") or "").strip()

        elif provider == "gemini":
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                return ""
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            _t0 = time.time()
            resp = _http_post_json(url, payload, {})
            from diffai.xrdreader.usage_tracker import log_http_call

            log_http_call(
                "phase1",
                model,
                "gemini",
                resp,
                wall_seconds=time.time() - _t0,
                tool_name="extract_main_material",
            )
            text = ""
            for cand in resp.get("candidates", []) or []:
                content = cand.get("content", {}) or {}
                for part in content.get("parts", []) or []:
                    if "text" in part:
                        text += part["text"]
            obj = _safe_json_loads(text)
            if isinstance(obj, dict):
                return str(obj.get("main_material", "") or "").strip()
        elif provider == "claude":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                return ""
            url = "https://api.anthropic.com/v1/messages"
            payload = {
                "model": model,
                "max_tokens": 1000,
                "temperature": 0.0,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
            }
            resp = _http_post_json(
                url,
                payload,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            text = ""
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            obj = _safe_json_loads(text)
            if isinstance(obj, dict):
                return str(obj.get("main_material", "") or "").strip()

    except Exception:
        return ""

    return ""


def extract_basic_metadata_from_pdf(
    client: Any, pdf_path: Path
) -> Dict[str, str]:
    """Extract basic metadata (title, author, DOI, abstract, arXiv id, ...).

    Reads the PDF's own metadata + first-page text, guesses the main material,
    and backfills a DOI via CrossRef when it is missing.
    """
    title = ""
    author = ""
    doi = ""
    abstract = ""
    main_material = ""
    doi_source = ""
    published_doi = ""
    source_type = ""
    arxiv_id = ""

    def _clean_line(s: str) -> str:
        s = re.sub(r"\s+", " ", str(s or "")).strip()
        return s

    def _find_abstract_text(text: str) -> str:
        lines = [_clean_line(ln) for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]

        start_idx = None
        for i, ln in enumerate(lines):
            low = ln.lower().strip()
            if low in {"abstract", "summary"} or low.startswith("abstract "):
                start_idx = i
                break

        if start_idx is None:
            m = re.search(
                r"(?is)\babstract\b[:\s-]*(.+?)(?=\n\s*(?:keywords?|introduction|1\.|i\.)\b|\Z)",
                text,
            )
            if m:
                return _clean_line(m.group(1))[:4000]
            return ""

        collected = []
        first = lines[start_idx]
        first_after = re.sub(r"(?i)^abstract[:\s-]*", "", first).strip()
        if first_after:
            collected.append(first_after)

        for ln in lines[start_idx + 1 :]:
            low = ln.lower().strip()
            if low in {"keywords", "introduction"}:
                break
            if re.match(r"^(keywords?)\b", low):
                break
            if re.match(r"^(introduction)\b", low):
                break
            if re.match(r"^(1\.|i\.)\s+", low):
                break
            collected.append(ln)

        return _clean_line(" ".join(collected))[:4000]

    def _guess_main_material(title_text: str, abstract_text: str) -> str:
        corpus = f"{title_text}\n{abstract_text}"

        patterns = [
            r"\b([A-Z][a-z]?(?:\d+[A-Za-z0-9\-]*)?(?:\s+[A-Z][a-z]?(?:\d+[A-Za-z0-9\-]*)?)*)\s+(?:alloy|alloys|nanoparticles|thin films|film|powder|ceramics|ceramic|oxide|oxides|composite|composites|system|systems)\b",
            r"\b([A-Z][a-z]?(?:[-/][A-Z][a-z]?)+(?:\s*[A-Za-z0-9\-]+)?)\b",
            r"\b([A-Z][a-z]?\d*[A-Za-z0-9\-]*(?:/[A-Z][a-z]?\d*[A-Za-z0-9\-]*)+)\b",
            r"\b([A-Z][a-z]?(?:\d+[A-Za-z0-9\-]*)?(?:[A-Z][a-z]?(?:\d+[A-Za-z0-9\-]*)?){1,5})\b",
        ]

        bad = {
            "XRD",
            "PXRD",
            "PDF",
            "DOI",
            "TEM",
            "SEM",
            "XPS",
            "Rietveld",
            "Cu K",
            "Mo K",
            "Kα",
            "K a",
            "AI",
            "ML",
        }

        for pat in patterns:
            for m in re.finditer(pat, corpus):
                cand = _clean_line(m.group(1)).strip(" ,.;:-")
                if not cand:
                    continue
                if len(cand) < 2 or len(cand) > 120:
                    continue
                if cand in bad:
                    continue
                if re.fullmatch(r"\d+(?:\.\d+)?", cand):
                    continue
                return cand

        return ""

    doc = fitz.open(str(pdf_path))
    try:
        meta = doc.metadata or {}
        title = str(meta.get("title", "") or "").strip()
        author = str(meta.get("author", "") or "").strip()

        text_chunks = []
        for i in range(min(3, doc.page_count)):
            try:
                text_chunks.append(doc.load_page(i).get_text("text") or "")
            except Exception:
                continue
        text_sample = "\n".join(text_chunks)

        try:
            arxiv_id = extract_arxiv_id_from_text(
                text_sample, fallback_text=str(pdf_path)
            )
        except NameError:
            arxiv_id = ""

        doi_match = re.search(
            r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text_sample, re.IGNORECASE
        )
        if doi_match:
            doi = doi_match.group(0).rstrip(".,;:) ]}")

        if arxiv_id:
            source_type = "arxiv"
            doi_source = "arxiv"
            published_doi = doi
            arxiv_id_base = re.sub(r"v\d+$", "", arxiv_id, flags=re.IGNORECASE)
            doi = f"10.48550/arXiv.{arxiv_id_base}"

        # Always try text-based title extraction; keep the longer result
        text_title = ""
        lines = [
            re.sub(r"\s+", " ", ln).strip() for ln in text_sample.splitlines()
        ]
        lines = [ln for ln in lines if ln]
        title_start = -1
        for i, ln in enumerate(lines[:20]):
            low = ln.lower()
            if low.startswith("arxiv:") or low.startswith("doi"):
                continue
            if re.fullmatch(r"[A-Za-z ,.-]+", ln) and len(ln.split()) <= 6:
                continue
            if len(ln) >= 20:
                title_start = i
                break
        if title_start >= 0:
            # Grab the title line and any continuation lines
            # (title often wraps when it contains chemical formulas)
            title_parts = [lines[title_start]]
            for j in range(title_start + 1, min(title_start + 5, len(lines))):
                next_ln = lines[j]
                next_low = next_ln.lower().strip()
                # Stop at known section markers
                if next_low in {
                    "abstract",
                    "summary",
                    "introduction",
                    "keywords",
                }:
                    break
                if re.match(r"^(abstract|keywords?|introduction)\b", next_low):
                    break
                # Stop at author-like lines (names with commas, affiliations)
                if re.search(
                    r"\b(university|department|institute|school|laboratory)\b",
                    next_low,
                ):
                    break
                # Stop if it looks like an author line (multiple commas, email)
                if "@" in next_ln or next_ln.count(",") >= 3:
                    break
                # Stop at very short filler lines
                if len(next_ln) < 3:
                    break
                # This line is likely a title continuation
                title_parts.append(next_ln)
                # Stop if the combined title no longer ends with a preposition/article
                combined = " ".join(title_parts).strip()
                last_word = combined.rstrip(".,;:").split()[-1].lower()
                if last_word not in {
                    "in",
                    "of",
                    "for",
                    "with",
                    "by",
                    "and",
                    "or",
                    "the",
                    "a",
                    "an",
                    "on",
                    "at",
                    "to",
                    "from",
                }:
                    break
            text_title = " ".join(title_parts).strip()[:500]
        # Use text-extracted title if it's longer than PDF metadata title
        if len(text_title) > len(title):
            title = text_title

        abstract = _find_abstract_text(text_sample)
        main_material = extract_main_material_with_llm(client, title, abstract)
        if not main_material:
            main_material = _guess_main_material(title, abstract)

    finally:
        doc.close()

    if title:
        crossref_meta = lookup_crossref_metadata_for_pdf(title, author)
        if crossref_meta:
            if not doi:
                doi = crossref_meta.get("doi", doi)
                doi_source = crossref_meta.get("doi_source", doi_source)
            if not author:
                author = crossref_meta.get("author", author)
            # Use CrossRef title if longer (PDF metadata often truncates)
            cr_title = crossref_meta.get("title", "")
            if len(cr_title) > len(title):
                title = cr_title

    return {
        "title": title,
        "doi": doi,
        "author": author,
        "abstract": abstract,
        "main_material": main_material,
        "source_type": source_type,
        "arxiv_id": arxiv_id,
        "doi_source": doi_source,
        "published_doi": published_doi,
    }


# ======================================================================================
# 2) Prompt (single call does BOTH: match + XRD + plot info)
# ======================================================================================
FIG_MATCH_AND_XRD_PROMPT = (
    "Return ONLY valid JSON. No markdown.\n\n"
    "You are given TWO images:\n"
    "A) a full PDF page with a red rectangle highlighting a region\n"
    "B) the cropped region itself\n\n"
    "TASK 1) Figure/caption matching\n"
    "- If the region is part of a paper figure, identify the Figure number.\n"
    "- The caption may be above/below the figure OR in a nearby column.\n"
    "- Return the caption text as it appears on the page (copy it; do not paraphrase).\n"
    "- If you cannot see a figure label/caption on the page, set matched_fig_num=null.\n\n"
    "TASK 2) Is the crop an XRD/PXRD plot?\n"
    "- Typical cues: x-axis 2θ (2theta) or angle; y-axis intensity/counts/a.u.; peaks; reference ticks; Rietveld fit.\n\n"
    "TASK 2b) If XRD: determine the data type.\n"
    '- Set xrd_data_type to one of: "experimental", "simulated", "both", or "unclear".\n'
    '- "simulated" means the plot shows ONLY computed/calculated/DFT/theoretical/modeled diffraction patterns.\n'
    '- "experimental" means the plot shows measured/observed/collected diffraction data (or has no qualifier, which usually means experimental).\n'
    '- "both" means the plot contains both experimental and simulated curves (e.g. Rietveld fit with observed + calculated, or experimental curves alongside ICDD/reference stick patterns).\n'
    '- "unclear" if you cannot determine the data type from the visible information.\n'
    "- Look at legend labels, axis annotations, caption text, and curve labels for cues like 'simulated', 'calculated', 'DFT', 'theoretical', 'modeled', 'observed', 'experimental', 'measured'.\n\n"
    "TASK 3) If XRD: extract plot info visible in the crop.\n"
    "- legend_entries: each legend item with label, color (simple name), linestyle, temperature (if any), material (if any).\n"
    "- x_axis_label, y_axis_label.\n"
    "- reported_temperatures: temperatures explicitly shown.\n"
    "- reported_materials: materials explicitly shown.\n"
    "- reported_phases: phases explicitly indicated, including symbol mapping like * = ZnO.\n"
    "- notes: short notes.\n\n"
    "CRITICAL RULES:\n"
    "- Do NOT guess.\n"
    "- Do NOT invent numbers or labels not visible.\n"
    "- If not visible, leave empty.\n\n"
    "Return JSON with exactly these keys:\n"
    "{\n"
    '  "matched_fig_num": int or null,\n'
    '  "matched_caption_text": "",\n'
    '  "confidence_match": number (0..1),\n'
    '  "reason_match": "",\n'
    '  "is_xrd": bool,\n'
    '  "confidence_xrd": number (0..1),\n'
    '  "xrd_data_type": "experimental" | "simulated" | "both" | "unclear",\n'
    '  "plot_info": {\n'
    '    "x_axis_label": "",\n'
    '    "y_axis_label": "",\n'
    '    "legend_entries": [\n'
    '      {"label":"", "color":"", "linestyle":"", "temperature":"", "material":""}\n'
    "    ],\n"
    '    "reported_temperatures": [""],\n'
    '    "reported_materials": [""],\n'
    '    "reported_phases": [""],\n'
    '    "is_multi_panel": false,\n'
    '    "sub_panel_labels": [],\n'
    '    "notes": ""\n'
    "  }\n"
    "}\n"
    "\n"
    "MULTI-PANEL DETECTION:\n"
    "- Set is_multi_panel=true if the figure contains multiple separate XRD plots\n"
    "  (e.g., labeled (a), (b), (c) or stacked with different axis frames).\n"
    '- List the sub-panel labels in sub_panel_labels (e.g. ["a", "b", "c"]).\n'
    "- A single plot with multiple overlaid curves is NOT multi-panel.\n"
)

AGENTIC_FIG_MATCH_AND_XRD_PROMPT = (
    FIG_MATCH_AND_XRD_PROMPT + "\nADDITIONAL LOCAL CONTEXT RULES:\n"
    "- You may use the provided LOCAL_TEXT only as weak supporting context.\n"
    "- LOCAL_TEXT may be incomplete or noisy because it comes from nearby PDF text extraction.\n"
    "- Prefer what is visually present in the images.\n"
    "- Use LOCAL_TEXT mainly to improve figure-number matching and to avoid false positives.\n"
    "- Do NOT copy values from LOCAL_TEXT into plot_info unless they are also visible in the figure/crop.\n"
)

CALL_CACHE_DIR = OUT_ROOT / "_agent_cache_phase1"


def page_has_xrd_like_text(page_pl) -> bool:
    """True if the page's text mentions XRD-ish terms."""
    txt = (page_pl.extract_text() or "").lower()
    tokens = [
        "xrd",
        "pxrd",
        "x-ray diffraction",
        "x ray diffraction",
        "2θ",
        "2theta",
        "theta",
        "intensity",
        "rietveld",
        "diffract",
    ]
    return any(tok in txt for tok in tokens)


def bbox_type(b, rasters, vec_regions) -> str:
    """Classify a box as raster / vector / mixed / unknown by overlap."""
    in_raster = any(iou(b, r) > 0 for r in rasters)
    in_vector = any(iou(b, v) > 0 for v in vec_regions)
    if in_raster and in_vector:
        return "mixed"
    if in_raster:
        return "raster"
    if in_vector:
        return "vector"
    return "unknown"


def extract_local_text_near_bbox(
    doc: fitz.Document,
    page_index0: int,
    bbox_pl: Tuple[float, float, float, float],
    max_chars: int = 1200,
) -> str:
    """Return the text words that sit around a figure's bbox."""
    page = doc.load_page(page_index0)
    x0, y0, x1, y1 = bbox_pl
    pad_x = max(20.0, 0.08 * (x1 - x0))
    pad_y = max(20.0, 0.08 * (y1 - y0))
    words = page.get_text("words")
    keep = []
    for w in words:
        if len(w) < 5:
            continue
        wx0, wy0, wx1, wy1, txt = (
            float(w[0]),
            float(w[1]),
            float(w[2]),
            float(w[3]),
            str(w[4]),
        )
        if wx1 < x0 - pad_x or wx0 > x1 + pad_x:
            continue
        if wy1 < y0 - 80 - pad_y or wy0 > y1 + 120 + pad_y:
            continue
        keep.append((wy0, wx0, txt))
    keep.sort(key=lambda z: (z[0], z[1]))
    out = " ".join(t for _, _, t in keep)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:max_chars]


def build_agent_plan(
    page_pl,
    bbox_pl,
    page_area: float,
    rasters,
    vec_regions,
    crop_path: Path,
    local_text: str,
) -> Dict[str, Any]:
    """Score a figure candidate and decide whether to call the LLM on it.

    Combines signals (vector/mixed source, area, crop size, nearby XRD text)
    into a 0..1 score and a should_call flag, and picks the prompt to use.
    """
    frac = bbox_area(bbox_pl) / page_area if page_area > 0 else 0.0
    source = bbox_type(bbox_pl, rasters, vec_regions)
    crop_bytes = crop_path.stat().st_size if crop_path.exists() else 0
    local_l = (local_text or "").lower()

    signals = 0
    reasons = []

    if source in {"vector", "mixed"}:
        signals += 1
        reasons.append("vector_or_mixed_candidate")
    if frac >= 0.03:
        signals += 1
        reasons.append("reasonable_area")
    if crop_bytes >= PHASE1_MIN_RENDERED_CROP_BYTES:
        signals += 1
        reasons.append("nontrivial_crop")
    if any(
        tok in local_l
        for tok in [
            "fig",
            "figure",
            "2θ",
            "2theta",
            "intensity",
            "xrd",
            "pxrd",
            "rietveld",
            "diffraction",
        ]
    ):
        signals += 2
        reasons.append("nearby_text_support")
    elif page_has_xrd_like_text(page_pl):
        signals += 1
        reasons.append("page_text_support")

    score = min(1.0, signals / 5.0)
    should_call = True
    if ENABLE_AGENTIC_PHASE1:
        if (
            source == "vector"
            and PHASE1_SKIP_TEXTLESS_VECTOR_CANDIDATES
            and not local_text.strip()
        ):
            should_call = False
            reasons.append("skip_textless_vector_candidate")
        if crop_bytes < PHASE1_MIN_RENDERED_CROP_BYTES:
            should_call = False
            reasons.append("skip_tiny_render")
        if score < PHASE1_MIN_AGENT_SCORE_TO_CALL:
            should_call = False
            reasons.append("below_agent_score_threshold")

    prompt = FIG_MATCH_AND_XRD_PROMPT
    if ENABLE_AGENTIC_PHASE1 and local_text.strip():
        prompt = (
            AGENTIC_FIG_MATCH_AND_XRD_PROMPT
            + "\nLOCAL_TEXT:\n"
            + local_text
            + "\n"
        )

    return {
        "should_call": should_call,
        "score": round(score, 4),
        "source": source,
        "reasons": reasons,
        "crop_bytes": crop_bytes,
        "local_text": local_text,
        "prompt": prompt,
    }


def _cache_key_for_call(
    prompt: str, highlighted_page_png: Path, crop_png: Path
) -> str:
    """Hash of provider+model+prompt+page+crop, used as the call-cache key."""
    h = hashlib.sha1()
    h.update(get_phase1_provider().encode("utf-8"))
    h.update(get_phase1_model().encode("utf-8"))
    h.update(prompt.encode("utf-8", errors="ignore"))
    h.update(highlighted_page_png.read_bytes())
    h.update(crop_png.read_bytes())
    return h.hexdigest()


def cached_call_llm_json(
    client: Any,
    prompt: str,
    highlighted_page_png: Path,
    crop_png: Path,
    log_prefix="",
) -> Dict:
    """call_llm_json with an on-disk cache keyed by the images + prompt."""
    if not PHASE1_USE_CALL_CACHE:
        return call_llm_json(
            client,
            prompt,
            highlighted_page_png,
            crop_png,
            log_prefix=log_prefix,
        )

    key = _cache_key_for_call(prompt, highlighted_page_png, crop_png)
    cache_path = CALL_CACHE_DIR / f"{key}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    obj = call_llm_json(
        client, prompt, highlighted_page_png, crop_png, log_prefix=log_prefix
    )
    # Created lazily: importing this module must not touch the filesystem,
    # or anything that merely inspects it leaves a stray run folder behind.
    CALL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return obj


# ======================================================================================
# 3) Geometry helpers
# ======================================================================================
def extract_bboxes(objs):
    """Pull (x0, top, x1, bottom) boxes from pdfplumber objects."""
    out = []
    for o in objs:
        if all(k in o for k in ("x0", "top", "x1", "bottom")):
            out.append(
                (
                    float(o["x0"]),
                    float(o["top"]),
                    float(o["x1"]),
                    float(o["bottom"]),
                )
            )
    return out


def bbox_area(b):
    """Area of a box (0 if degenerate)."""
    x0, y0, x1, y1 = b
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def iou(a, b):
    """Intersection-over-union of two boxes."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    denom = bbox_area(a) + bbox_area(b) - inter
    return inter / denom if denom > 0 else 0.0


def union(a, b):
    """Smallest box that covers both a and b."""
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def merge_boxes(boxes, thr):
    """Greedily merge overlapping boxes (IoU > thr) until stable."""
    out = []
    for b in boxes:
        merged = False
        for k in range(len(out)):
            if iou(b, out[k]) > thr:
                out[k] = union(out[k], b)
                merged = True
                break
        if not merged:
            out.append(b)

    changed = True
    while changed:
        changed = False
        new = []
        for b in out:
            merged = False
            for k in range(len(new)):
                if iou(b, new[k]) > thr:
                    new[k] = union(new[k], b)
                    merged = True
                    changed = True
                    break
            if not merged:
                new.append(b)
        out = new
    return out


def clip_bbox_to_page(b, page):
    """Clip a box to the page; None if it collapses to nothing."""
    x0, y0, x1, y1 = b
    px0, py0, px1, py1 = page.bbox
    x0 = max(px0, x0)
    y0 = max(py0, y0)
    x1 = min(px1, x1)
    y1 = min(py1, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


# ======================================================================================
# 4) Vector region detection via grid + connected components
# ======================================================================================
def connected_components(mask: np.ndarray):
    """Find 4-connected True blobs in a mask -> (r0, c0, r1, c1, area)."""
    H, W = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    comps = []
    for r in range(H):
        for c in range(W):
            if not mask[r, c] or seen[r, c]:
                continue
            stack = [(r, c)]
            seen[r, c] = True
            r0 = r1 = r
            c0 = c1 = c
            area = 0
            while stack:
                rr, cc = stack.pop()
                area += 1
                r0, r1 = min(r0, rr), max(r1, rr)
                c0, c1 = min(c0, cc), max(c1, cc)
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = rr + dr, cc + dc
                    if (
                        0 <= nr < H
                        and 0 <= nc < W
                        and mask[nr, nc]
                        and not seen[nr, nc]
                    ):
                        seen[nr, nc] = True
                        stack.append((nr, nc))
            comps.append((r0, c0, r1, c1, area))
    return comps


def compute_vec_regions(page_pl):
    """Find vector-graphic regions (candidate plots) via a primitive grid."""
    w, h = page_pl.width, page_pl.height
    page_area = w * h

    vec_prim = (
        extract_bboxes(page_pl.lines)
        + extract_bboxes(page_pl.rects)
        + extract_bboxes(page_pl.curves)
    )
    if not vec_prim:
        return []

    grid = np.zeros((GRID, GRID), dtype=bool)
    for x0, y0, x1, y1 in vec_prim:
        c0 = int(np.clip(x0 / w * GRID, 0, GRID - 1))
        c1 = int(np.clip(x1 / w * GRID, 0, GRID - 1))
        r0 = int(np.clip(y0 / h * GRID, 0, GRID - 1))
        r1 = int(np.clip(y1 / h * GRID, 0, GRID - 1))
        grid[r0 : r1 + 1, c0 : c1 + 1] = True

    comps = [
        c for c in connected_components(grid) if c[4] >= MIN_COMPONENT_AREA
    ]

    vec_regions = []
    for r0, c0, r1, c1, _ in comps:
        r0 = max(0, r0 - MERGE_PAD_PX)
        c0 = max(0, c0 - MERGE_PAD_PX)
        r1 = min(GRID - 1, r1 + MERGE_PAD_PX)
        c1 = min(GRID - 1, c1 + MERGE_PAD_PX)

        x0 = (c0 / GRID) * w
        x1 = ((c1 + 1) / GRID) * w
        y0 = (r0 / GRID) * h
        y1 = ((r1 + 1) / GRID) * h
        b = (x0, y0, x1, y1)

        if (bbox_area(b) / page_area) <= MAX_VEC_PAGE_FRAC:
            vec_regions.append(b)

    return vec_regions


# ======================================================================================
# 5) Rendering helpers
# ======================================================================================
def render_page_png_with_highlight(
    doc: fitz.Document,
    page_index0: int,
    bbox_pl: Tuple[float, float, float, float],
    out_path: Path,
    dpi: int,
):
    """
    Robust mapping:
    - Use page.rect to scale pdfplumber coords -> pixmap pixels.
    - Works better when CropBox/rotation is involved.
    Assumption: pdfplumber x is from left; y is from top of visible page.
    """
    page = doc.load_page(page_index0)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # Page size in points (visible rect)
    rect = page.rect
    pw = float(rect.width)
    ph = float(rect.height)

    x0, y0, x1, y1 = bbox_pl

    # Scale pdf points -> pixels based on rendered pix size
    sx = pix.width / pw if pw > 0 else zoom
    sy = pix.height / ph if ph > 0 else zoom

    x0p = int(round(x0 * sx))
    x1p = int(round(x1 * sx))
    y0p = int(round(y0 * sy))
    y1p = int(round(y1 * sy))

    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [x0p, y0p, x1p, y1p],
        outline=(255, 0, 0),
        width=max(2, int(2 * max(sx, sy))),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def render_crop_png_pdfplumber(
    page_pl,
    bbox_pdf: Tuple[float, float, float, float],
    out_path: Path,
    dpi: int,
):
    """Render a bbox crop of a page to a PNG at the given dpi."""
    crop = page_pl.crop(bbox_pdf, strict=False)
    img = crop.to_image(resolution=dpi)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))


# ======================================================================================
# 6) LLM call helpers
# ======================================================================================
def _sleep_backoff(attempt: int):
    """Sleep with exponential backoff + jitter before a retry."""
    time.sleep(BASE_SLEEP_SEC * (2**attempt) + random.random() * 0.25)


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


def _safe_json_loads(s: str) -> Dict:
    """Parse the first JSON object in `s`; return {} on any failure."""
    try:
        s = _extract_json_object(s)
        return json.loads(s) if s else {}
    except Exception:
        return {}


def _data_url_png(path: Path) -> str:
    """Return a PNG file as a base64 data: URL."""
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _png_part(path: Path) -> Dict[str, str]:
    """Return a PNG as a {mime_type, data_base64} dict."""
    return {
        "mime_type": "image/png",
        "data_base64": base64.b64encode(path.read_bytes()).decode("utf-8"),
    }


def _http_post_json(url: str, payload: Dict, headers: Dict[str, str]) -> Dict:
    """POST JSON and return the parsed response (raises on HTTP error)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {error_body[:500]}") from e


def build_client() -> Any:
    """Build the LLM client for the Phase 1 provider.

    gemini/claude return None here (they use raw HTTP for vision).
    """
    provider = get_phase1_provider()
    if provider == "gpt":
        assert os.environ.get(
            "OPENAI_API_KEY"
        ), "Missing OPENAI_API_KEY env var"
        return OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    if provider == "grok":
        assert os.environ.get("XAI_API_KEY"), "Missing XAI_API_KEY env var"
        return OpenAI(
            api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1"
        )
    if provider == "together":
        api_key = os.environ.get("TOGETHER_API_KEY")
        assert api_key, "Missing TOGETHER_API_KEY env var"
        return OpenAI(api_key=api_key, base_url="https://api.together.xyz/v1")
    if provider in {"gemini", "claude"}:
        return None
    raise ValueError(f"Unsupported provider: {provider}")


def call_llm_json(
    client: Any,
    prompt: str,
    highlighted_page_png: Path,
    crop_png: Path,
    log_prefix="",
) -> Dict:
    """Send the prompt + two images to the model and parse its JSON reply.

    GPT/Grok/Together use the OpenAI SDK; Gemini/Claude use raw HTTP. Retries
    with backoff; returns the parsed dict (or {}).
    """
    last_err = None
    provider = get_phase1_provider()

    for attempt in range(MAX_API_RETRIES + 1):
        try:
            if provider in {"gpt", "grok"}:
                _t0 = time.time()
                resp = client.responses.create(
                    model=get_phase1_model(),
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {
                                    "type": "input_image",
                                    "image_url": _data_url_png(
                                        highlighted_page_png
                                    ),
                                },
                                {
                                    "type": "input_image",
                                    "image_url": _data_url_png(crop_png),
                                },
                            ],
                        }
                    ],
                    temperature=0.0,
                )
                from diffai.xrdreader.usage_tracker import get_tracker

                get_tracker().log_call(
                    phase="phase1",
                    model=get_phase1_model(),
                    resp=resp,
                    wall_seconds=time.time() - _t0,
                    tool_name="call_llm_json",
                )
                txt = (getattr(resp, "output_text", "") or "").strip()
                return _safe_json_loads(txt)

            if provider == "together":
                _t0 = time.time()
                resp = client.chat.completions.create(
                    model=get_phase1_model(),
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": _data_url_png(
                                            highlighted_page_png
                                        )
                                    },
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": _data_url_png(crop_png)
                                    },
                                },
                            ],
                        }
                    ],
                    temperature=0.0,
                )
                from diffai.xrdreader.usage_tracker import (
                    get_tracker as _get_tracker,
                )

                _tracker = _get_tracker()
                usage = getattr(resp, "usage", None)
                _in = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
                _out = (
                    getattr(usage, "completion_tokens", 0) or 0 if usage else 0
                )
                _tracker.log_call(
                    phase="phase1",
                    model=get_phase1_model(),
                    input_tokens=_in,
                    output_tokens=_out,
                    wall_seconds=time.time() - _t0,
                    tool_name="call_llm_json",
                )
                txt = (resp.choices[0].message.content or "").strip()
                return _safe_json_loads(txt)

            if provider == "gemini":
                api_key = os.environ.get("GEMINI_API_KEY")
                if not api_key:
                    raise RuntimeError("Missing GEMINI_API_KEY env var")
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{get_phase1_model()}:generateContent?key={api_key}"
                payload = {
                    "generationConfig": {
                        "temperature": 0.0,
                        "responseMimeType": "application/json",
                    },
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {"text": prompt},
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": _png_part(
                                            highlighted_page_png
                                        )["data_base64"],
                                    }
                                },
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": _png_part(crop_png)[
                                            "data_base64"
                                        ],
                                    }
                                },
                            ],
                        }
                    ],
                }
                _t0 = time.time()
                obj = _http_post_json(url, payload, headers={})
                from diffai.xrdreader.usage_tracker import log_http_call

                log_http_call(
                    "phase1",
                    get_phase1_model(),
                    "gemini",
                    obj,
                    wall_seconds=time.time() - _t0,
                    tool_name="call_llm_json",
                )
                txt = ""
                for cand in obj.get("candidates", []):
                    content = cand.get("content", {})
                    for part in content.get("parts", []):
                        if "text" in part:
                            txt += part["text"]
                return _safe_json_loads(txt)

            if provider == "claude":
                api_key = os.environ.get("ANTHROPIC_API_KEY")
                if not api_key:
                    raise RuntimeError("Missing ANTHROPIC_API_KEY env var")
                url = "https://api.anthropic.com/v1/messages"
                payload = {
                    "model": get_phase1_model(),
                    "max_tokens": 4000,
                    "temperature": 0.0,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": _png_part(
                                            highlighted_page_png
                                        )["data_base64"],
                                    },
                                },
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": _png_part(crop_png)[
                                            "data_base64"
                                        ],
                                    },
                                },
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
                    "phase1",
                    get_phase1_model(),
                    "claude",
                    obj,
                    wall_seconds=time.time() - _t0,
                    tool_name="call_llm_json",
                )
                txt = ""
                for block in obj.get("content", []):
                    if block.get("type") == "text":
                        txt += block.get("text", "")
                return _safe_json_loads(txt)

            raise ValueError(f"Unsupported provider: {provider}")

        except Exception as e:
            last_err = e
            print(
                f"{log_prefix}[{provider}_retry] attempt={attempt+1}/{MAX_API_RETRIES+1} err={repr(e)}"
            )
            _sleep_backoff(attempt)
    raise last_err


def match_and_xrd_from_images(
    client: Any,
    highlighted_page_png: Path,
    crop_png: Path,
    prompt: Optional[str] = None,
    log_prefix="",
) -> Dict:
    """Classify one figure crop (match + XRD) via the cached LLM call."""
    return cached_call_llm_json(
        client,
        prompt or FIG_MATCH_AND_XRD_PROMPT,
        highlighted_page_png,
        crop_png,
        log_prefix=log_prefix,
    )


# ======================================================================================
# 6b) Agentic loop — tools + perceive-act cycle for figure classification
# ======================================================================================


class Phase1AgentToolbox:
    """
    Holds the open PDF + current candidate context and exposes tool implementations.
    Created per-candidate, closed after the agent loop finishes.
    """

    def __init__(
        self,
        doc: fitz.Document,
        pdf_pl,
        page_index0: int,
        bbox_pl: Tuple[float, float, float, float],
        page_pl,
    ):
        self.doc = doc
        self.pdf_pl = pdf_pl
        self.page_index0 = page_index0
        self.bbox_pl = bbox_pl
        self.page_pl = page_pl
        self.n_pages = doc.page_count
        self._page_text_cache: Dict[int, str] = {}

    def _page_text(self, page_1indexed: int) -> str:
        """Return (and cache) the raw text of one 1-indexed page."""
        if page_1indexed in self._page_text_cache:
            return self._page_text_cache[page_1indexed]
        idx = page_1indexed - 1
        if idx < 0 or idx >= self.n_pages:
            return ""
        try:
            txt = self.doc.load_page(idx).get_text("text") or ""
        except Exception:
            txt = ""
        self._page_text_cache[page_1indexed] = txt
        return txt

    # ---- tool: get_nearby_text ------------------------------------------------
    def tool_get_nearby_text(self, expand_px: int = 80) -> str:
        """Return text near the candidate box on the current page."""
        expand_px = max(0, min(expand_px, 300))
        try:
            txt = extract_local_text_near_bbox(
                self.doc,
                self.page_index0,
                self.bbox_pl,
                max_chars=2000,
            )
            if not txt.strip():
                return "(no text found near the candidate region)"
            return txt
        except Exception as e:
            return f"(error: {e})"

    # ---- tool: get_page_text -------------------------------------------------
    def tool_get_page_text(self, page: int, max_chars: int = 3500) -> str:
        """Return one 1-indexed page's cleaned text (length-bounded)."""
        if not isinstance(page, int):
            return "(error: page must be int)"
        if page < 1 or page > self.n_pages:
            return f"(error: page {page} out of range 1..{self.n_pages})"
        txt = self._page_text(page)
        if not txt.strip():
            return f"(page {page} is empty)"
        return re.sub(r"\s+", " ", txt).strip()[:max_chars]

    # ---- tool: find_captions_on_page ------------------------------------------
    def tool_find_captions_on_page(self, page: Optional[int] = None) -> str:
        """Find figure/table captions on a page; return them as JSON."""
        target_page = page if isinstance(page, int) else (self.page_index0 + 1)
        if target_page < 1 or target_page > self.n_pages:
            return f"(error: page {target_page} out of range)"
        try:
            fitz_page = self.doc.load_page(target_page - 1)
            blocks = fitz_page.get_text("blocks") or []
        except Exception as e:
            return f"(error: {e})"

        cap_re = re.compile(
            r"(fig(?:ure|\.)\s*\d+|table\s*\d+)\s*[:.\-]?\s*",
            re.IGNORECASE,
        )
        captions = []
        for b in blocks:
            if len(b) < 5:
                continue
            txt = str(b[4] or "").strip()
            m = cap_re.search(txt)
            if m:
                short = re.sub(r"\s+", " ", txt)[:300]
                captions.append({"label": m.group(1).strip(), "text": short})

        if not captions:
            return "(no figure/table captions found on this page)"
        return json.dumps(captions, ensure_ascii=False)

    # ---- tool: check_adjacent_page -------------------------------------------
    def tool_check_adjacent_page(self, offset: int) -> str:
        """Return the previous/next page's text (offset -1 or +1)."""
        if offset not in (-1, 1):
            return "(error: offset must be -1 or +1)"
        target = self.page_index0 + 1 + offset  # 1-indexed
        if target < 1 or target > self.n_pages:
            return f"(no page {target}; document has {self.n_pages} pages)"
        return self.tool_get_page_text(target)

    # ---- tool: expand_crop (text-only feedback) --------------------------------
    def tool_expand_crop(self, pad_pt: int = 30) -> str:
        """Report any extra text revealed by padding the crop region."""
        pad_pt = max(5, min(pad_pt, 150))
        x0, y0, x1, y1 = self.bbox_pl
        page = self.page_pl
        nx0 = max(0, x0 - pad_pt)
        ny0 = max(0, y0 - pad_pt)
        nx1 = min(float(page.width), x1 + pad_pt)
        ny1 = min(float(page.height), y1 + pad_pt)

        try:
            txt = extract_local_text_near_bbox(
                self.doc,
                self.page_index0,
                (nx0, ny0, nx1, ny1),
                max_chars=2500,
            )
        except Exception:
            txt = ""

        if not txt.strip():
            return f"(expanded region by {pad_pt}pt on each side, but no additional text found)"
        return f"Expanded region by {pad_pt}pt. Text visible in expanded area:\n{txt}"


PHASE1_AGENT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_nearby_text",
        "description": (
            "Get text blocks near the candidate bounding box on the current page. "
            "Returns nearby text that may include captions, axis labels, or discussion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expand_px": {
                    "type": "integer",
                    "description": "Extra padding (default 80) to widen the search area",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_page_text",
        "description": "Return the full text of a specific page (1-indexed).",
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
        "name": "find_captions_on_page",
        "description": (
            "Find all figure and table captions on the current page (default) or a specified page. "
            "Use when you see a figure region but need to identify its number/caption."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Page to search (default: current page)",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "check_adjacent_page",
        "description": (
            "Get text from a neighboring page. Useful when a caption might be on the previous "
            "or next page, or when the figure discussion continues across pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "offset": {
                    "type": "integer",
                    "description": "-1 for previous page, +1 for next page",
                    "enum": [-1, 1],
                },
            },
            "required": ["offset"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "expand_crop",
        "description": (
            "Examine a wider area around the candidate region. Returns text visible in the "
            "expanded zone. Use if the crop seems to cut off axis labels, legends, or captions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pad_pt": {
                    "type": "integer",
                    "description": "Padding in PDF points to add on each side (5-150)",
                    "minimum": 5,
                    "maximum": 150,
                },
            },
            "required": ["pad_pt"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "finalize",
        "description": (
            "Submit the final classification for this candidate region. This ends the agent loop. "
            "Set is_xrd to true only if the crop is clearly an XRD/PXRD plot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "matched_fig_num": {
                    "type": ["integer", "null"],
                    "description": "Figure number if matched, null if unknown",
                },
                "matched_caption_text": {"type": "string"},
                "confidence_match": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "reason_match": {"type": "string"},
                "is_xrd": {"type": "boolean"},
                "confidence_xrd": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "xrd_data_type": {
                    "type": "string",
                    "enum": ["experimental", "simulated", "both", "unclear"],
                    "description": (
                        "Whether the XRD data is experimental (measured/observed), "
                        "simulated (calculated/DFT/theoretical/modeled), both, or unclear. "
                        "Set to 'unclear' if not XRD or cannot determine."
                    ),
                },
                "plot_info": {
                    "type": "object",
                    "description": (
                        "XRD plot metadata. Include x_axis_label, y_axis_label, "
                        "legend_entries, reported_temperatures, reported_materials, "
                        "reported_phases, is_multi_panel, sub_panel_labels, notes. "
                        "Leave empty if not XRD."
                    ),
                },
            },
            "required": [
                "matched_fig_num",
                "matched_caption_text",
                "confidence_match",
                "reason_match",
                "is_xrd",
                "confidence_xrd",
                "xrd_data_type",
                "plot_info",
            ],
            "additionalProperties": False,
        },
    },
]


PHASE1_AGENT_SYSTEM_PROMPT = """
You are an XRD figure-classification agent analyzing a candidate region of a scientific PDF.

You are given TWO images:
A) A full PDF page with a RED rectangle highlighting a candidate region
B) The cropped candidate region itself

Your goal:
1) Determine if the crop is part of a figure. If so, identify the figure number and copy
   the caption text exactly as it appears on the page.
2) Determine if the crop is an XRD / PXRD plot.
   Typical cues: x-axis labeled 2θ (2theta) or diffraction angle; y-axis intensity/counts/a.u.;
   sharp peaks; reference tick marks; Rietveld fit overlay.
3) If XRD: determine xrd_data_type — is the data experimental, simulated, both, or unclear?
   - "experimental": measured/observed/collected data, or no qualifier (default assumption).
   - "simulated": ONLY computed/calculated/DFT/theoretical/modeled patterns.
   - "both": contains both experimental and simulated curves (e.g. Rietveld observed+calculated,
     experimental data with ICDD/JCPDS reference stick patterns).
   - "unclear": cannot determine from visible information.
   Look at legend labels, caption text, axis annotations, and curve labels for cues.
4) If XRD: extract plot metadata visible in the crop — axis labels, legend entries (with color,
   linestyle, temperature, material), reported temperatures, reported materials, reported phases
   (including symbol mappings like * = ZnO), and any notes.
5) If XRD: detect if the figure has multiple separate sub-panels (e.g. (a), (b), (c) with
   different axis frames). Set is_multi_panel=true and list sub_panel_labels. Multiple
   overlaid curves in a single plot is NOT multi-panel.

Strategy:
- Start by examining the images carefully. Often that is enough to decide.
- If the figure number or caption is unclear, call find_captions_on_page or get_nearby_text.
- If you need more context about the paper (e.g. to confirm the figure is XRD rather than
  another diffraction technique), use get_page_text.
- If the crop seems to cut off axis labels or legends, use expand_crop.
- If the caption might be on a neighboring page, use check_adjacent_page.
- Call finalize exactly once when you are confident.

CRITICAL RULES:
- Do NOT guess or invent labels, numbers, or materials not visible in the images or tool outputs.
- If a field is not visible, leave it as empty string or empty list.
- Budget: {max_steps} tool calls. Be efficient. If you exhaust the budget without calling
  finalize, the system defaults to is_xrd=false and matched_fig_num=null.
""".strip()


def _execute_phase1_tool(
    toolbox: Phase1AgentToolbox, name: str, args: Dict
) -> str:
    """Dispatch one Phase 1 tool call to the toolbox; return its observation."""
    try:
        if name == "get_nearby_text":
            return toolbox.tool_get_nearby_text(int(args.get("expand_px", 80)))
        if name == "get_page_text":
            return toolbox.tool_get_page_text(int(args.get("page", 0)))
        if name == "find_captions_on_page":
            pg = args.get("page")
            return toolbox.tool_find_captions_on_page(
                int(pg) if pg is not None else None
            )
        if name == "check_adjacent_page":
            return toolbox.tool_check_adjacent_page(int(args.get("offset", 0)))
        if name == "expand_crop":
            return toolbox.tool_expand_crop(int(args.get("pad_pt", 30)))
        return f"(error: unknown tool '{name}')"
    except Exception as e:
        return f"(error: tool {name} failed: {e})"


def _parse_finalize_args(args: Dict) -> Dict:
    """Normalize a finalize tool call's arguments into the standard Phase 1 JSON shape."""
    matched_fig_num = args.get("matched_fig_num")
    if matched_fig_num is not None:
        try:
            matched_fig_num = int(matched_fig_num)
        except (TypeError, ValueError):
            matched_fig_num = None

    def _float01(v, default=0.0):
        try:
            return max(0.0, min(1.0, float(v)))
        except Exception:
            return default

    xrd_data_type = (
        str(args.get("xrd_data_type", "unclear") or "unclear").strip().lower()
    )
    if xrd_data_type not in {"experimental", "simulated", "both", "unclear"}:
        xrd_data_type = "unclear"

    return {
        "matched_fig_num": matched_fig_num,
        "matched_caption_text": str(
            args.get("matched_caption_text", "") or ""
        ).strip(),
        "confidence_match": _float01(args.get("confidence_match")),
        "reason_match": str(args.get("reason_match", "") or "").strip(),
        "is_xrd": bool(args.get("is_xrd", False)),
        "confidence_xrd": _float01(args.get("confidence_xrd")),
        "xrd_data_type": xrd_data_type,
        "plot_info": (
            args.get("plot_info")
            if isinstance(args.get("plot_info"), dict)
            else {}
        ),
    }


def run_phase1_agent(
    client: Any,
    doc: fitz.Document,
    pdf_pl,
    page_index0: int,
    page_pl,
    bbox_pl: Tuple[float, float, float, float],
    highlighted_page_png: Path,
    crop_png: Path,
    log_prefix: str = "",
) -> Dict:
    """
    Agentic figure-classification loop — works with ALL providers via ToolCaller.
    """
    provider = get_phase1_provider()
    model = get_phase1_model()

    # For providers that don't support vision in tool-calling mode,
    # fall back to single-shot vision call
    # Vision-capable models only for the agent loop.
    # Gemini/Claude use their own HTTP vision call via single-shot.
    # Grok-3 doesn't support images — also fall back to single-shot.
    VISION_MODELS_GROK = {"grok-2-vision", "grok-2-vision-1212"}
    if provider in ("gemini", "claude"):
        obj = match_and_xrd_from_images(
            client, highlighted_page_png, crop_png, log_prefix=log_prefix
        )
        obj["agent_trace"] = []
        obj["steps_used"] = 0
        return obj
    if provider == "grok" and model not in VISION_MODELS_GROK:
        print(
            f"{log_prefix}[WARNING] Model '{model}' does not support images. "
            f"Use grok-2-vision for Phase 1, or switch to GPT/Gemini/Claude. Skipping candidate."
        )
        return {
            "matched_fig_num": None,
            "matched_caption_text": "",
            "confidence_match": 0.0,
            "reason_match": f"Model {model} does not support vision.",
            "is_xrd": False,
            "confidence_xrd": 0.0,
            "xrd_data_type": "unclear",
            "plot_info": {},
            "agent_trace": [],
            "steps_used": 0,
        }

    import base64

    from diffai.xrdreader.tool_calling import ToolCaller

    toolbox = Phase1AgentToolbox(doc, pdf_pl, page_index0, bbox_pl, page_pl)

    caller = ToolCaller(provider, model, max_retries=PHASE1_AGENT_MAX_RETRIES)
    caller.set_system(
        PHASE1_AGENT_SYSTEM_PROMPT.format(max_steps=PHASE1_AGENT_MAX_STEPS)
    )

    # Send images on first turn
    hl_b64 = base64.b64encode(highlighted_page_png.read_bytes()).decode(
        "ascii"
    )
    crop_b64 = base64.b64encode(crop_png.read_bytes()).decode("ascii")
    caller.add_user_message(
        f"Analyze candidate region on page {page_index0 + 1}. "
        "Image A is the full page with the red highlight. "
        "Image B is the cropped candidate region. "
        "Gather evidence with tools if needed, then call finalize.",
        images=[
            {"data_base64": hl_b64, "media_type": "image/png"},
            {"data_base64": crop_b64, "media_type": "image/png"},
        ],
    )

    trace = []
    final = None

    for step in range(PHASE1_AGENT_MAX_STEPS):
        _t0 = time.time()
        tool_calls, _ = caller.call(PHASE1_AGENT_TOOLS)
        elapsed = time.time() - _t0

        from diffai.xrdreader.usage_tracker import get_tracker

        usage = caller.get_last_call_usage()
        get_tracker().log_call(
            phase="phase1_agent",
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            wall_seconds=elapsed,
        )

        if not tool_calls:
            print(
                f"{log_prefix}[agent] step {step+1}: no tool calls; stopping."
            )
            break

        stop_loop = False
        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"]
            call_id = tc["call_id"]

            if name == "finalize":
                final = _parse_finalize_args(args)
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

            observation = _execute_phase1_tool(toolbox, name, args)
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
            f"{log_prefix}[agent] budget exhausted after {steps_used} steps."
        )
        final = {
            "matched_fig_num": None,
            "matched_caption_text": "",
            "confidence_match": 0.0,
            "reason_match": f"Budget exhausted after {steps_used} steps.",
            "is_xrd": False,
            "confidence_xrd": 0.0,
            "xrd_data_type": "unclear",
            "plot_info": {},
        }

    final["agent_trace"] = trace
    final["steps_used"] = steps_used
    return final


def _sanitize_trace(trace):
    """Strip non-serializable objects from agent trace."""
    if not trace:
        return []
    clean = []
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        clean.append(
            {
                "step": entry.get("step", 0),
                "tool": str(entry.get("tool", "")),
                "observation": str(entry.get("observation", ""))[:500],
            }
        )
    return clean


# ======================================================================================
# 7) Process one PDF
# ======================================================================================
def process_pdf(client: Any, pdf_path: Path):
    """Detect candidate figures in one PDF, classify each, write its JSON.

    For every page: find raster + vector candidate boxes, merge them, render a
    highlighted page + crop, run the agent (or a single vision call) to decide
    match / is_xrd, and collect the kept records into `*__phase1_raw.json`.
    """
    from diffai.xrdreader.usage_tracker import set_current_pdf

    set_current_pdf(pdf_path.name)

    safe_base = make_safe_stem(pdf_path.stem)
    cand_dir = OUT_ROOT / f"{safe_base}_candidates"
    xrd_dir = OUT_ROOT / f"{safe_base}_xrd"
    dbg_dir = OUT_ROOT / f"{safe_base}_debug"  # highlighted page images
    cand_dir.mkdir(parents=True, exist_ok=True)
    xrd_dir.mkdir(parents=True, exist_ok=True)
    if SAVE_DEBUG_HIGHLIGHT:
        dbg_dir.mkdir(parents=True, exist_ok=True)

    out_records = []  # ONLY matched+XRD entries if KEEP_ONLY_MATCHED_AND_XRD

    doc = fitz.open(str(pdf_path))
    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        print(f"[SKIP] Encrypted: {pdf_path.name}")
        return None

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            n_pages = min(len(pdf.pages), doc.page_count)

            for page_index in range(n_pages):
                page_pl = pdf.pages[page_index]
                w, h = page_pl.width, page_pl.height
                page_area = w * h

                rasters = extract_bboxes(page_pl.images)
                vec_regions = compute_vec_regions(page_pl)

                candidates = rasters + vec_regions
                if not candidates:
                    continue

                merged = merge_boxes(candidates, thr=MERGE_IOU_THR)

                for j, b in enumerate(merged, start=1):
                    b = clip_bbox_to_page(b, page_pl)
                    if b is None:
                        continue

                    frac = bbox_area(b) / page_area if page_area > 0 else 0.0
                    if frac < MIN_CROP_AREA_FRAC or frac > MAX_CROP_AREA_FRAC:
                        continue

                    crop_path = (
                        cand_dir / f"page_{page_index+1:03d}_cand_{j:02d}.png"
                    )
                    render_crop_png_pdfplumber(page_pl, b, crop_path, DPI_CROP)

                    if SAVE_DEBUG_HIGHLIGHT:
                        hl_path = (
                            dbg_dir
                            / f"page_{page_index+1:03d}_cand_{j:02d}__HIGHLIGHT.png"
                        )
                        render_page_png_with_highlight(
                            doc, page_index, b, hl_path, DPI_PAGE
                        )
                    else:
                        # still need a "page image" for the prompt; render lightweight
                        hl_path = (
                            cand_dir
                            / f"page_{page_index+1:03d}_cand_{j:02d}__HIGHLIGHT.png"
                        )
                        render_page_png_with_highlight(
                            doc, page_index, b, hl_path, dpi=96
                        )

                    local_text = extract_local_text_near_bbox(
                        doc, page_index, b
                    )
                    agent_plan = build_agent_plan(
                        page_pl,
                        b,
                        page_area,
                        rasters,
                        vec_regions,
                        crop_path,
                        local_text,
                    )

                    log_prefix = f"[{pdf_path.name} p{page_index+1} c{j}] "
                    if not agent_plan["should_call"]:
                        print(
                            f"{log_prefix}SKIP agent_score={agent_plan['score']:.2f} reasons={agent_plan['reasons']}"
                        )
                        continue

                    if ENABLE_AGENTIC_PHASE1:
                        # ---- Agentic loop: model picks tools, iterates ----
                        obj = run_phase1_agent(
                            client,
                            doc,
                            pdf,
                            page_index,
                            page_pl,
                            b,
                            hl_path,
                            crop_path,
                            log_prefix=log_prefix,
                        )
                    else:
                        # ---- Legacy single-shot call ----
                        obj = match_and_xrd_from_images(
                            client,
                            hl_path,
                            crop_path,
                            prompt=agent_plan["prompt"],
                            log_prefix=log_prefix,
                        )

                    matched_fig_num = obj.get("matched_fig_num", None)
                    matched_caption_text = (
                        obj.get("matched_caption_text", "") or ""
                    ).strip()
                    is_xrd = bool(obj.get("is_xrd", False))
                    xrd_data_type = (
                        str(obj.get("xrd_data_type", "unclear") or "unclear")
                        .strip()
                        .lower()
                    )
                    if xrd_data_type not in {
                        "experimental",
                        "simulated",
                        "both",
                        "unclear",
                    }:
                        xrd_data_type = "unclear"

                    rec = {
                        "pdf": str(pdf_path),
                        "page": page_index + 1,
                        "cand_index": j,
                        "bbox_pdf": [b[0], b[1], b[2], b[3]],
                        "area_frac": frac,
                        "candidate_path": str(crop_path),
                        "matched_fig_num": matched_fig_num,
                        "matched_caption_text": matched_caption_text,
                        "match_confidence": obj.get("confidence_match", None),
                        "match_reason": obj.get("reason_match", ""),
                        "is_xrd": is_xrd,
                        "xrd_confidence": obj.get("confidence_xrd", None),
                        "xrd_data_type": xrd_data_type,
                        "plot_info": (
                            obj.get("plot_info", {}) if is_xrd else {}
                        ),
                        "debug_highlight_page_png": str(hl_path),
                        "agent_decision": {
                            "score": agent_plan["score"],
                            "source": agent_plan["source"],
                            "reasons": agent_plan["reasons"],
                            "crop_bytes": agent_plan["crop_bytes"],
                            "used_local_text": bool(
                                agent_plan["local_text"].strip()
                            ),
                        },
                        "agent_trace": _sanitize_trace(
                            obj.get("agent_trace", [])
                        ),
                        "steps_used": obj.get("steps_used", 0),
                    }

                    if KEEP_ONLY_MATCHED_AND_XRD:
                        if (matched_fig_num is None) or (not is_xrd):
                            continue

                    if (
                        SKIP_SIMULATED_ONLY_XRD
                        and is_xrd
                        and xrd_data_type == "simulated"
                    ):
                        print(
                            f"{log_prefix}SKIP simulated-only XRD fig={matched_fig_num}"
                        )
                        continue

                    if is_xrd:
                        if isinstance(matched_fig_num, int):
                            kept_name = f"page_{page_index+1:03d}_fig_{matched_fig_num:03d}_cand_{j:02d}.png"
                        else:
                            kept_name = (
                                f"page_{page_index+1:03d}_xrd_{j:02d}.png"
                            )
                        kept_path = xrd_dir / kept_name
                        kept_path.write_bytes(crop_path.read_bytes())
                        rec["xrd_path"] = str(kept_path)

                    out_records.append(rec)
                    print(
                        f"{log_prefix}KEEP fig={matched_fig_num} XRD={is_xrd} type={xrd_data_type} steps={rec.get('steps_used', 0)}"
                    )

    finally:
        doc.close()

    basic_meta = extract_basic_metadata_from_pdf(client, pdf_path)

    out_json = OUT_ROOT / f"{safe_base}__phase1_raw.json"
    out_json.write_text(
        json.dumps(
            {
                "pdf": str(pdf_path),
                "title": basic_meta.get("title", ""),
                "doi": basic_meta.get("doi", ""),
                "author": basic_meta.get("author", ""),
                "abstract": basic_meta.get("abstract", ""),
                "main_material": basic_meta.get("main_material", ""),
                "source_type": basic_meta.get("source_type", ""),
                "arxiv_id": basic_meta.get("arxiv_id", ""),
                "doi_source": basic_meta.get("doi_source", ""),
                "published_doi": basic_meta.get("published_doi", ""),
                "xrd_figures": out_records,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(
        f"[DONE] {pdf_path.name}: kept_records={len(out_records)} -> {out_json.name}"
    )
    return out_json


# ======================================================================================
# 8) Main
# ======================================================================================
def main(pdf_dir=None):
    """Run Phase 1 over every input PDF and write per-PDF figure JSON.

    `pdf_dir` is the folder to read. pipeline.py passes the folder it already
    selected (Phase 0's kept set when screening ran, the full download folder
    when `--skip-screening` was used). When called standalone it defaults to the
    kept set, falling back to the download folder if the kept set is empty.

    Calls process_pdf on each PDF; a failure on one is logged and skipped.
    """
    # EDIT HERE: ensure you set the correct API key env var for the selected provider
    provider = get_phase1_provider()
    model = get_phase1_model()
    print(f"[INFO] Phase 1 using provider={provider}, model={model}")
    client = build_client()

    # Created here rather than at import time: importing this module must
    # not touch the filesystem, or anything that merely inspects it (Sphinx,
    # a linter, a test collector) leaves a stray timestamped run folder
    # behind in the working directory.
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    # The caller (pipeline.py) already decided which folder to use, based on
    # whether screening ran this session. Honour that instead of re-deciding --
    # the two used to disagree, and `--skip-screening` after a run that rejected
    # everything would crash here on an existing-but-empty phase0_kept_pdfs.
    if pdf_dir is not None:
        phase1_pdf_dir = Path(pdf_dir)
    else:
        # Standalone use: prefer the kept folder, but only if it actually holds
        # PDFs -- an empty one means fall back to the full download folder.
        phase1_pdf_dir = (
            PHASE1_INPUT_PDF_DIR
            if list(PHASE1_INPUT_PDF_DIR.rglob("*.pdf"))
            else PDF_DIR
        )
    pdfs = sorted(phase1_pdf_dir.rglob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(
            f"No PDFs found in: {phase1_pdf_dir.resolve()}"
        )

    for p in pdfs:
        p = p.resolve()
        try:
            process_pdf(client, p)
        except Exception as e:
            from diffai.xrdreader.utils import humanize_llm_error

            print(f"[FAILED] {p.name}: {humanize_llm_error(e)}")
            if os.getenv("XRDREADER_DEBUG"):
                import traceback

                traceback.print_exc()
            continue

    from diffai.xrdreader.usage_tracker import set_current_pdf as _clear_pdf

    _clear_pdf()


if __name__ == "__main__":
    main()
