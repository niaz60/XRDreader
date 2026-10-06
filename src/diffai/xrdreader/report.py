"""Build a single-page HTML report of what a run actually did.

Every run already writes the evidence: Step 0's screening verdicts, the figure
crops, the agent traces each step records, the usage report. This module reads
those files and lays them out as one page -- which documents were kept and why,
which crops were classified as diffraction patterns, what metadata came out, and
every tool each agent called.

Written automatically at the end of a run (config.WRITE_RUN_REPORT), or built for
an existing run with ``diffai-xrdreader --report <run folder>``.

By default the page points at the crops already on disk, which keeps it around
100 KB but means it has to stay in its run folder. ``embed=True`` bakes the
images into the file instead, so it can be sent to someone, at the cost of size.
"""

import base64
import glob
import io
import json
import os
import re
import urllib.parse

# The template carries the page's markup, styling and behaviour; this module only
# supplies its data. It ships inside the package (see pyproject package-data).
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")

PHASE_TO_STEP = {
    "phase0": "s0",
    "phase1": "s1", "phase1_agent": "s1",
    "phase2": "s2", "phase2_agent": "s2",
    "phase3": "s3", "phase3_agent": "s3",
}
STEP_KEYS = ("s0", "s1", "s2", "s3")
STEP_FLAG = (
    ("RUN_DOWNLOAD", "download"), ("RUN_PHASE0_FILTER", "step0"),
    ("RUN_PHASE1", "step1"), ("RUN_PHASE2", "step2"),
    ("RUN_JSON_CLEAN_AGENT", "clean"), ("RUN_JSON_VERIFY_AGENT", "step3"),
)
CROP_PX, CROP_Q = 760, 76   # embedded figure crops: must stay readable as a plot
PAGE_PX, PAGE_Q = 620, 62   # embedded page overlays: only need to read as a page
MAX_OBS = 400               # characters kept from each tool's observation
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# locating a run
# ---------------------------------------------------------------------------
def find_run(path):
    """Resolve `path` to a run directory, which is one holding results/.

    Accepts the run itself, or any folder above it: the default
    ``xrdreader_output/<timestamp>/`` layout and folders named with -o are both
    found. The most recently written run wins when several match.
    """
    root = os.path.abspath(path)
    if os.path.isdir(os.path.join(root, "results")):
        return root
    patterns = [
        ("xrdreader_output", "*"), ("*", "xrdreader_output", "*"),
        ("*", "*", "xrdreader_output", "*"), ("*",), ("*", "*"),
    ]
    found = []
    for pat in patterns:
        for cand in sorted(glob.glob(os.path.join(root, *pat))):
            if os.path.isdir(os.path.join(cand, "results")) and cand not in found:
                found.append(cand)
    if not found:
        raise FileNotFoundError(
            "No run found under %s. Point --report at a run folder, or at one "
            "containing xrdreader_output/." % root
        )
    return max(found, key=lambda r: os.path.getmtime(os.path.join(r, "results")))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _load(path, default=None):
    """Read a JSON file, returning `default` when it is absent or unreadable."""
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _first(pattern):
    """The first path matching `pattern`, or "" when nothing matches."""
    hits = glob.glob(pattern)
    return hits[0] if hits else ""


def _data_uri(path, max_px, quality):
    """Re-encode an image small enough to live inside the page."""
    raw = open(path, "rb").read()
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        if im.width > max_px:
            im = im.convert("RGB").resize(
                (max_px, int(im.height * max_px / im.width)), Image.LANCZOS
            )
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        if buf.tell() < len(raw):
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass  # Pillow missing or the image is unusual: fall back to the original bytes
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _img_src(path, base, embed, max_px, quality):
    """A relative link for a report living in the run folder, or an embedded copy."""
    if embed:
        return _data_uri(path, max_px, quality)
    rel = os.path.relpath(path, base).replace(os.sep, "/")
    return urllib.parse.quote(rel)


def _trim_trace(trace):
    """Keep the tool, its arguments and a readable slice of what came back."""
    return [
        {
            "tool": s.get("tool", ""),
            "args": s.get("args") or {},
            "obs": (s.get("observation") or "")[:MAX_OBS],
        }
        for s in (trace or [])
    ]


def _stem(res):
    """The sanitised stem naming a document's output files.

    Step 0 records the original download name in source_pdf, but every later file
    is named after the sanitised stem, which _source_file carries.
    """
    src = res.get("_source_file") or ""
    if src:
        return re.split(r"__phase0", os.path.basename(src))[0]
    return os.path.basename(res.get("source_pdf", "")).rsplit(".pdf", 1)[0]


def _pretty_title(res):
    """A readable title for a document that never reached Step I."""
    base = os.path.basename(res.get("source_pdf", "")).rsplit(".pdf", 1)[0]
    base = re.sub(r"__\d{4}\.\d{4,5}v?\d*$", "", base)   # arXiv id suffix
    base = re.sub(r"__10\..*$", "", base)                # doi suffix
    return base.replace("_", " ").strip()


def _describe_run(manifest, run):
    """Rebuild the command this run is equivalent to, from the manifest it wrote."""
    sw = manifest.get("master_switches") or {}
    dl = manifest.get("download_sources") or {}
    se = manifest.get("search") or {}
    mo = manifest.get("model") or {}

    on = [flag for key, flag in STEP_FLAG if sw.get(key)]
    parts = ["diffai-xrdreader", "--full-run" if len(on) == len(STEP_FLAG) else "--steps " + ",".join(on)]
    srcs = [k[4:].lower() for k in ("USE_ARXIV", "USE_SPRINGER", "USE_ELSEVIER", "USE_CROSSREF") if dl.get(k)]
    if srcs:
        parts.append("--sources " + ",".join(srcs))
    if dl.get("TARGET_DOWNLOADS"):
        parts.append("-n %s" % dl["TARGET_DOWNLOADS"])
    if se.get("ELEMENTS"):
        parts.append('--elements "%s"' % se["ELEMENTS"])
    if se.get("TECHNIQUE"):
        parts.append('--technique "%s"' % se["TECHNIQUE"])
    if mo.get("PROVIDER"):
        parts.append("--provider %s" % mo["PROVIDER"])
    if mo.get("MODEL"):
        parts.append("--model %s" % mo["MODEL"])
    if dl.get("REQUIRE_CC_LICENSE"):
        parts.append("--cc-only")
    if (manifest.get("agentic") or {}).get("DISABLE_ALL_AGENTS"):
        parts.append("--single-pass")

    label = "Run " + os.path.basename(run)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", manifest.get("timestamp", ""))
    if m:
        label = "Run of %d %s %s, %s:%s" % (
            int(m.group(3)), MONTHS[int(m.group(2)) - 1], m.group(1), m.group(4), m.group(5)
        )
    return label, " ".join(parts)


# ---------------------------------------------------------------------------
# assembling the data the page renders
# ---------------------------------------------------------------------------
def _collect(run, base, embed, overlays):
    """Read one run's outputs into the structure the template expects."""
    results = os.path.join(run, "results")
    usage = _load(os.path.join(results, "usage_report.json"), {}) or {}
    timing = _load(os.path.join(results, "per_pdf_timing.json"), {}) or {}
    screen = _load(os.path.join(results, "phase0_consolidated.json"), {"results": []})
    manifest = _load(os.path.join(results, "run_manifest.json"), {}) or {}
    per_pdf = timing.get("per_pdf", {})

    papers = []
    for res in screen.get("results", []):
        stem = _stem(res)
        decision = res.get("decision", {})

        key = stem + ".pdf"
        if key not in per_pdf:
            key = next((k for k in per_pdf if k.startswith(stem[:40])), key)
        rec = per_pdf.get(key, {})

        steps = {k: {"cost": 0.0, "calls": 0, "sec": 0.0, "traces": []} for k in STEP_KEYS}
        for phase, v in (rec.get("phases") or {}).items():
            k = PHASE_TO_STEP.get(phase)
            if not k or not isinstance(v, dict):
                continue
            steps[k]["cost"] += v.get("cost", v.get("cost_usd", 0)) or 0
            steps[k]["calls"] += v.get("calls", v.get("requests", 0)) or 0
            steps[k]["sec"] += v.get("wall", v.get("wall_seconds", 0)) or 0
        steps["s0"]["traces"] = [_trim_trace(res.get("agent_trace"))]

        final = _load(_first(os.path.join(results, stem + "*phase3_validated_FINAL.json")), {}) or {}
        vlog = _load(_first(os.path.join(results, stem + "*phase3_validation_log.json")), {}) or {}
        raw = _load(_first(os.path.join(results, stem + "*phase1_raw.json")), {}) or {}
        rich = _load(_first(os.path.join(results, stem + "*phase2_enriched.json")), {}) or {}

        # every crop Step I considered, flagged by whether it survived classification
        cands = []
        detected = raw.get("xrd_figures", []) or []
        for png in sorted(glob.glob(os.path.join(results, stem + "_candidates", "*.png"))):
            m = re.search(r"page_(\d+)", os.path.basename(png))
            page = int(m.group(1)) if m else 0
            hit = next((f for f in detected if int(f.get("page", 0)) == page), None)
            is_xrd = bool(hit and hit.get("is_xrd"))
            overlay = os.path.join(results, stem + "_debug",
                                   os.path.basename(png).replace(".png", "__HIGHLIGHT.png"))
            cands.append({
                "page": page,
                "img": _img_src(png, base, embed, CROP_PX, CROP_Q),
                "overlay": (_img_src(overlay, base, embed, PAGE_PX, PAGE_Q)
                            if overlays and os.path.exists(overlay) else ""),
                "is_xrd": is_xrd,
                "conf": str(hit.get("xrd_confidence")) if hit and hit.get("xrd_confidence") is not None else "",
                "note": (
                    (hit.get("match_reason") or "") if is_xrd
                    else ("Cropped from page %d and ruled out as a diffraction pattern, "
                          "so it never reached Step II." % page)
                ),
            })

        figures = []
        vfigs = vlog.get("figures", []) or []
        for i, fg in enumerate(final.get("figures", []) or []):
            vf = vfigs[i] if i < len(vfigs) else {}
            page = fg.get("page")
            figures.append({
                "n": fg.get("figure_number"),
                "page": page,
                "caption": fg.get("caption", ""),
                "metadata": fg.get("xrd_metadata") or {},
                "plot_info": fg.get("plot_info") or {},
                "verified": {
                    k: {
                        "candidate": v.get("candidate_value"),
                        "corrected": v.get("corrected_value"),
                        "evidence": (v.get("evidence_span") or "")[:300],
                    }
                    for k, v in (vf.get("verified") or {}).items()
                },
                "s3_trace": _trim_trace(vf.get("agent_trace")),
            })
            for src, key_ in ((detected, "s1"), (rich.get("xrd_figures", []) or [], "s2")):
                match = next((f for f in src if f.get("page") == page), {})
                steps[key_]["traces"].append(_trim_trace(match.get("agent_trace")))
        if vlog.get("methods_agent_trace"):
            steps["s3"]["traces"].append(_trim_trace(vlog["methods_agent_trace"]))
        steps["s3"]["traces"].extend(f["s3_trace"] for f in figures)

        papers.append({
            "title": final.get("title") or raw.get("title") or _pretty_title(res),
            "doi": final.get("published_doi") or final.get("doi") or "",
            "source": final.get("source_type") or raw.get("source_type") or "",
            "decision": decision.get("decision", "?"),
            "confidence": decision.get("confidence", ""),
            "reason": decision.get("reason", ""),
            "cost": rec.get("total_cost", 0),
            "calls": rec.get("total_calls", 0),
            "steps": steps,
            "candidates": cands,
            "figures": figures,
        })

    papers.sort(key=lambda p: (p["decision"] != "keep", -p["cost"]))
    kept = [p for p in papers if p["decision"] == "keep"]
    grand = usage.get("grand_total", {})
    label, command = _describe_run(manifest, run)

    return {
        "run": {
            "label": label,
            "folder": os.path.basename(run),
            "command": command,
            "n_pdfs": len(papers),
            "kept": len(kept),
            "calls": grand.get("requests", 0),
            "cost": grand.get("cost_usd", 0),
            "wall": grand.get("pipeline_wall_seconds", grand.get("wall_seconds", 0)),
            "avg_kept_cost": (sum(p["cost"] for p in kept) / len(kept)) if kept else 0,
            "foot": "Written by XRDreader from this run's own output files: the screening "
                    "decisions, the figure crops, the agent traces each step recorded, and "
                    "the usage report.",
        },
        "papers": papers,
    }


def build_report(run_dir, out_path=None, embed=False, overlays=True):
    """Write the report for `run_dir` and return the path it was written to.

    With embed=False (the default) the page links to the crops in the run folder,
    so it must stay beside them; with embed=True the images are baked in and the
    file can be moved or sent on its own.
    """
    run = find_run(run_dir)
    out = os.path.abspath(out_path or os.path.join(run, "report.html"))
    base = os.path.dirname(out)

    data = {"runs": [_collect(run, base, embed, overlays)]}
    with io.open(TEMPLATE, encoding="utf-8") as fh:
        page = fh.read().replace("/*__DATA__*/", json.dumps(data, ensure_ascii=False))

    # The template is the page body; a file opened from disk needs a real document
    # around it, and a charset, or the browser guesses and mangles the text.
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>"
        "</head><body>" + page + "</body></html>"
    )
    with io.open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    return out
