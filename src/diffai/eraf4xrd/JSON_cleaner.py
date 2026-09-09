#!/usr/bin/env python3
"""JSON clean step -- normalize + slim the Phase II enriched JSON.

Flattens the {value: ...} verified blocks, de-duplicates materials, picks a
single main material, and keeps only the fields downstream needs. Reads each
`*__phase2_enriched.json` and writes `*__phase2_clean.json`.
"""

import json
from pathlib import Path

from diffai.eraf4xrd.config import PHASE1_DIR
from diffai.eraf4xrd.utils import make_safe_stem


def safe_get(d, *keys):
    """Walk nested dict keys, returning None if any level is missing."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k, None)
    return d


def extract_verified_block(block):
    """Flatten a verified block {k: {value: v}} into {k: v}."""
    out = {}
    if not isinstance(block, dict):
        return out

    for k, v in block.items():
        if isinstance(v, dict) and "value" in v:
            out[k] = v["value"]
    return out


def uniq_clean(items):
    """De-duplicate + whitespace-normalize a list of strings (order kept)."""
    out = []
    seen = set()
    for x in items:
        s = " ".join(str(x).split()).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def choose_single_material(fig):
    """Pick one material for a figure from its plot_info / regex candidates."""
    plot_info = fig.get("plot_info", {}) or {}
    phase2_block = fig.get("phase2_xrd_text", {}) or {}
    regex_candidate = phase2_block.get("regex_candidate", {}) or {}

    reported_materials = uniq_clean(
        plot_info.get("reported_materials", []) or []
    )
    material_systems = uniq_clean(
        regex_candidate.get("material_systems", []) or []
    )

    legend_materials = []
    for entry in plot_info.get("legend_entries", []) or []:
        if isinstance(entry, dict):
            m = str(entry.get("material", "")).strip()
            if m:
                legend_materials.append(m)
    legend_materials = uniq_clean(legend_materials)

    if material_systems:
        return material_systems[0]
    if reported_materials:
        return reported_materials[0]
    if legend_materials:
        return legend_materials[0]
    return ""


def choose_main_material(phase2):
    """Pick the paper's main material from Phase II fields + figures."""
    candidates = []

    for key in ["main_material", "material", "paper_material_system"]:
        v = str(phase2.get(key, "") or "").strip()
        if v:
            candidates.append(v)

    title = str(phase2.get("title", "") or "").strip()
    if title:
        candidates.append(title)

    abstract = str(phase2.get("abstract", "") or "").strip()
    if abstract:
        candidates.append(abstract)

    figures = phase2.get("xrd_figures", []) or []
    for fig in figures:
        m = choose_single_material(fig)
        if m:
            candidates.append(m)

    for x in candidates:
        x = " ".join(str(x).split()).strip()
        if x:
            return x
    return ""


def clean_global(phase2):
    """Flatten the global verified block, adding regex-candidate fields."""
    verified = safe_get(phase2, "global_xrd_text", "verified")
    block = extract_verified_block(verified)

    # Propagate new Phase II global fields if present
    regex_cand = safe_get(phase2, "global_xrd_text", "regex_candidate") or {}
    for new_field in (
        "lattice_parameters",
        "reported_phases",
        "profile_function",
        "peak_positions_2theta",
        "fwhm",
        # CIF-comparison fields — same fallback logic
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
    ):
        if new_field not in block:
            val = regex_cand.get(new_field)
            if val not in (None, "", [], {}):
                block[new_field] = val
    return block


def clean_figures(phase2):
    """Slim each XRD figure down to the fields downstream needs."""
    figures_out = []

    for fig in phase2.get("xrd_figures", []):
        verified = safe_get(fig, "phase2_xrd_text", "verified")
        clean_verified = extract_verified_block(verified)
        material = choose_single_material(fig)

        # Keep all XRD figures — they were already classified in Phase I

        # Preserve Phase 1 vision-extracted plot_info
        plot_info = fig.get("plot_info", {}) or {}
        clean_plot_info = {}
        if plot_info:
            clean_plot_info = {
                "x_axis_label": plot_info.get("x_axis_label", ""),
                "y_axis_label": plot_info.get("y_axis_label", ""),
                "legend_entries": plot_info.get("legend_entries", []),
                "reported_temperatures": plot_info.get(
                    "reported_temperatures", []
                ),
                "reported_materials": plot_info.get("reported_materials", []),
                "reported_phases": plot_info.get("reported_phases", []),
                "lattice_parameters": plot_info.get("lattice_parameters", {}),
                "profile_function": plot_info.get("profile_function", ""),
                "peak_positions_2theta": plot_info.get(
                    "peak_positions_2theta", []
                ),
                "fwhm": plot_info.get("fwhm", []),
                "notes": plot_info.get("notes", ""),
            }

        figures_out.append(
            {
                "figure_id": fig.get("figure_id", ""),
                "figure_number": fig.get("matched_fig_num"),
                "page": fig.get("page"),
                "caption": fig.get("matched_caption_text"),
                "material": material,
                "xrd_metadata": clean_verified,
                "plot_info": clean_plot_info,
                "is_multi_panel": (fig.get("plot_info") or {}).get(
                    "is_multi_panel", False
                ),
                "sub_panel_labels": (fig.get("plot_info") or {}).get(
                    "sub_panel_labels", []
                ),
                "metadata_provenance": fig.get("metadata_provenance", {}),
            }
        )

    return figures_out


def build_clean_json(phase2):
    """Assemble the final clean record (metadata + global + figures)."""
    return {
        # ---------- METADATA ----------
        "pdf": phase2.get("pdf_resolved", phase2.get("pdf")),
        "title": phase2.get("title", ""),
        "doi": phase2.get("doi", ""),
        "author": phase2.get("author", ""),
        "source_type": phase2.get("source_type", ""),
        "arxiv_id": phase2.get("arxiv_id", ""),
        "doi_source": phase2.get("doi_source", ""),
        "published_doi": phase2.get("published_doi", ""),
        "main_material": choose_main_material(phase2),
        # ---------- EXISTING ----------
        "global": clean_global(phase2),
        "figures": clean_figures(phase2),
    }


def clean_one_json(input_json: Path) -> Path:
    """Clean one enriched JSON and write its `*__phase2_clean.json`."""
    phase2 = json.loads(input_json.read_text(encoding="utf-8"))

    raw_pdf = phase2.get("pdf_resolved", phase2.get("pdf", "")) or ""
    if not str(raw_pdf).strip():
        raise ValueError(f"Missing pdf path in: {input_json}")
    pdf_path = Path(raw_pdf)

    safe_base = make_safe_stem(pdf_path.stem)
    clean = build_clean_json(phase2)

    output_json = input_json.with_name(f"{safe_base}__phase2_clean.json")
    output_json.write_text(
        json.dumps(clean, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"[OK] Clean JSON written to: {output_json}")
    return output_json


def main():
    """Clean every `*__phase2_enriched.json` in PHASE1_DIR."""
    if not PHASE1_DIR.exists():
        raise FileNotFoundError(f"PHASE1_DIR not found: {PHASE1_DIR}")

    input_files = sorted(PHASE1_DIR.glob("*__phase2_enriched.json"))
    if not input_files:
        raise FileNotFoundError(
            f"No *__phase2_enriched.json found in: {PHASE1_DIR}"
        )

    for input_json in input_files:
        clean_one_json(input_json)


if __name__ == "__main__":
    main()
