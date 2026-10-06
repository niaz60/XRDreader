"""Shared helpers used across every XRDreader phase.

Filename sanitizing (make_pdf_safe_title / make_safe_stem), a Windows/Unicode-
safe logger, directory helpers, a retrying binary writer, thin PyMuPDF /
pdfplumber / PIL wrappers for reading and rendering PDFs, and the download /
confidence provenance loggers.
"""

import json as _json
import os
import re
import time
from datetime import datetime

RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


def make_pdf_safe_title(
    title: str, fallback: str, unique_id: str = "", max_title_len: int = 80
) -> str:
    """Turn a paper title into a filesystem-safe, unique PDF filename stem.

    Strips LaTeX/markup and characters that are illegal in filenames, then
    optionally appends a sanitized unique id (DOI/arXiv id) so two papers
    with the same title don't overwrite each other. Falls back to `fallback`
    when nothing usable is left.
    """
    text = title or fallback

    # strip markup, then replace characters that are unsafe in filenames
    text = re.sub(r"\$.*?\$", "_", text)  # inline LaTeX math: $...$
    text = re.sub(r"ce_[A-Za-z0-9_]+", "_", text)  # drop "ce_..." tokens
    text = re.sub(r"<[^>]+>", "_", text)  # HTML / XML tags
    text = re.sub(r"[{}[\]()]", "_", text)  # brackets, braces, parens
    text = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", text)  # illegal in filenames
    text = re.sub(r"[^A-Za-z0-9._ -]", "_", text)  # keep safe chars only
    text = re.sub(r"\s+", " ", text).strip()  # collapse whitespace
    text = re.sub(r"_+", "_", text)  # collapse underscores
    text = text.strip("._ ")  # trim leading/trailing . _ space

    if not text:
        text = fallback

    safe_unique_id = ""
    if unique_id:
        safe_unique_id = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", unique_id)
        safe_unique_id = re.sub(r"[^A-Za-z0-9._-]", "_", safe_unique_id)
        safe_unique_id = re.sub(r"_+", "_", safe_unique_id).strip("._ ")

    if safe_unique_id:
        allowed_title_len = max(20, max_title_len - len(safe_unique_id) - 2)
        text = text[:allowed_title_len].rstrip(" ._")
        safe_title = f"{text}__{safe_unique_id}"
    else:
        safe_title = text[:max_title_len].rstrip(" ._")

    if not safe_title:
        safe_title = fallback

    if safe_title.upper() in RESERVED_FILENAMES:
        safe_title = f"file_{safe_title}"

    return safe_title


def make_safe_stem(name: str, max_len: int = 60) -> str:
    """Sanitize an arbitrary name into a short, safe file stem.

    Same character cleanup as make_pdf_safe_title, but instead of appending a
    unique id it hashes (sha1) any over-long name so the stem stays under
    max_len while staying unique.
    """
    text = str(name or "").strip()

    text = re.sub(r"\$.*?\$", "_", text)
    text = re.sub(r"ce_[A-Za-z0-9_]+", "_", text)
    text = re.sub(r"<[^>]+>", "_", text)
    text = re.sub(r"[{}[\]()]", "_", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", text)
    text = re.sub(r"[^A-Za-z0-9._ -]", "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"_+", "_", text)
    text = text.strip("._ ")

    if not text:
        text = "paper"

    if len(text) > max_len:
        h = (
            __import__("hashlib")
            .sha1(text.encode("utf-8", errors="ignore"))
            .hexdigest()[:10]
        )
        text = text[: max_len - 11].rstrip(" ._") + "_" + h

    if text.upper() in RESERVED_FILENAMES:
        text = f"file_{text}"

    return text


# -------------------------------------------------------------------
# GLOBAL LOG FILE HANDLE
# -------------------------------------------------------------------
_log_file = None  # set by main via set_log_file()


def set_log_file(fh):
    """Register the open file handle that log() should also write to."""
    global _log_file
    _log_file = fh


# -------------------------------------------------------------------
# LOGGING (WINDOWS/UNICODE SAFE)
# -------------------------------------------------------------------
# Credentials that must never reach a log file or the console. Read from the
# environment at call time rather than from config, so a key set later (for
# example by the web UI before it launches a run) is still covered, and so
# utils does not depend on config being imported first.
_SECRET_ENV_VARS = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "TOGETHER_API_KEY",
    "SPRINGER_API_KEY",
    "ELSEVIER_API_KEY",
    "MP_API_KEY",
    # Not a credential, but Unpaywall takes it as a URL query parameter, so
    # it lands in request URLs and error text like a key would. It is the
    # user's personal address and does not belong in a shared log.
    "UNPAYWALL_EMAIL",
)


def redact_secrets(message: str) -> str:
    """Replace any configured API key appearing in *message*.

    Third-party libraries put credentials into their own error text -- the
    Springer client logs the request params, and a requests HTTPError carries
    the full URL including the api_key query parameter. Logging such an
    exception wrote the caller's key into the run log, so every line is
    scrubbed here rather than at each call site.
    """
    if not message:
        return message
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name)
        # 8 characters guards against a short or placeholder value matching
        # ordinary words in the message.
        if value and len(value) >= 8 and value in message:
            message = message.replace(value, f"<{name} redacted>")
    return message


def log(message: str):
    """Print and (if a log file is open) record one timestamped line.

    Windows/Unicode-safe: if a character can't be encoded, it retries with an
    ASCII-replaced copy instead of crashing the run. API keys are redacted
    before anything is written.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {redact_secrets(str(message))}"

    try:
        print(line)
    except UnicodeEncodeError:
        try:
            safe_line = line.encode("ascii", "replace").decode("ascii")
            print(safe_line)
        except Exception:
            pass

    if _log_file:
        try:
            _log_file.write(line + "\n")
            _log_file.flush()
        except UnicodeEncodeError:
            try:
                safe_line = line.encode("ascii", "replace").decode("ascii")
                _log_file.write(safe_line + "\n")
                _log_file.flush()
            except Exception:
                pass
        except Exception:
            pass


# -------------------------------------------------------------------
# DIRECTORIES
# -------------------------------------------------------------------
def ensure_dir(path: str, clean: bool = False):
    """Create `path` if it doesn't exist; if clean=True, wipe it first."""
    import shutil

    if clean and os.path.exists(path):
        log(f"Cleaning directory: {path}")
        shutil.rmtree(path)

    os.makedirs(path, exist_ok=True)
    log(f"Directory ready: {path}")


def get_source_subdir(base_dir: str, source_name: str) -> str:
    """Return (creating if needed) a per-source subfolder under base_dir."""
    safe_source = re.sub(r'[<>:"/\\|?*]', "_", source_name)
    subdir = os.path.join(base_dir, safe_source)
    os.makedirs(subdir, exist_ok=True)
    return subdir


# -------------------------------------------------------------------
# SAFE WRITE
# -------------------------------------------------------------------
def safe_write(path: str, data: bytes, retries: int = 5, delay: float = 1.0):
    """Write bytes to `path`, retrying if the file is briefly locked.

    Windows can hold a short-lived lock (antivirus, cloud sync); retry a few
    times on PermissionError before giving up.
    """
    for attempt in range(retries):
        try:
            with open(path, "wb") as f:
                f.write(data)
            return True
        except PermissionError as e:
            log(f"File locked ({e}); retrying {attempt+1}/{retries}...")
            time.sleep(delay)
        except Exception as e:
            log(f"Write error: {e}")
            return False

    log(f"Failed to write file after {retries} retries: {path}")
    return False


# -------------------------------------------------------------------
# PDF UTILITIES
# -------------------------------------------------------------------
import fitz  # noqa: E402  (PyMuPDF)
import pdfplumber  # noqa: E402
from PIL import Image  # noqa: E402


def get_image_page_numbers(pdf_path: str):
    """
    Returns a list of image index + page number pairs.
    This only finds EMBEDDED raster images.
    """
    doc = fitz.open(pdf_path)
    results = []

    for page_number in range(len(doc)):
        page = doc[page_number]
        for img_index, _ in enumerate(page.get_images(full=True)):
            results.append({"img_index": img_index, "page": page_number + 1})

    doc.close()
    return results


def extract_images_from_pdf(pdf_path: str, out_dir: str):
    """
    Extract embedded raster images only.
    DOES NOT capture vector plots.
    """
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    extracted = []

    for page_index in range(len(doc)):
        page = doc[page_index]
        imgs = page.get_images(full=True)

        for i, img in enumerate(imgs):
            xref = img[0]
            pix = fitz.Pixmap(doc, xref)

            if pix.n >= 5:
                pix = fitz.Pixmap(fitz.csRGB, pix)

            img_path = os.path.join(out_dir, f"p{page_index+1}_i{i+1}.png")
            pix.save(img_path)
            extracted.append((img_path, page_index + 1))

    doc.close()
    return extracted


def is_valid_figure_image(img_path, min_w=150, min_h=150):
    """
    Reject tiny masks, icons, or junk images.
    """
    try:
        img = Image.open(img_path)
        w, h = img.size
        return w >= min_w and h >= min_h
    except Exception:
        return False


# -------------------------------------------------------------------
# NEW: TEXT-BASED XRD PAGE DETECTION (pdfplumber)
# -------------------------------------------------------------------
def find_xrd_candidate_pages(pdf_path: str):
    """
    Scan PDF TEXT to identify pages likely containing XRD.
    Returns a sorted list of 1-based page numbers.
    """
    keywords = [
        "xrd",
        "x-ray diffraction",
        "x ray diffraction",
        "pxrd",
        "powder diffraction",
        "2θ",
        "2theta",
        "cu kα",
        "cu ka",
    ]

    candidate_pages = set()

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").lower()
            if any(k in text for k in keywords):
                candidate_pages.add(i + 1)

    return sorted(candidate_pages)


# -------------------------------------------------------------------
# NEW: FULL PAGE RENDERING (VECTOR → IMAGE)
# -------------------------------------------------------------------
def render_pdf_page(
    pdf_path: str,
    page_num: int,
    out_dir: str,
    dpi: int = 300,
):
    """
    Render a FULL PDF page to an image.
    This is REQUIRED for vector XRD plots.
    """
    os.makedirs(out_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    page = doc[page_num - 1]

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)

    out_path = os.path.join(out_dir, f"page_{page_num}.png")
    pix.save(out_path)

    doc.close()
    return out_path


# ---------------------------------------------------------------------------
# Improvement 7: Confidence calibration logger
# ---------------------------------------------------------------------------
# Download provenance logger
# ---------------------------------------------------------------------------
_PROVENANCE_PATH = None


def log_download(
    pdf_filename: str,
    source: str,
    url: str = "",
    doi: str = "",
    title: str = "",
    extra: dict = None,
):
    """Append one download record to outputs/download_provenance.json."""
    global _PROVENANCE_PATH
    if _PROVENANCE_PATH is None:
        from diffai.xrdreader.config import OUT_ROOT

        _PROVENANCE_PATH = OUT_ROOT / "download_provenance.json"

    record = {
        "pdf_filename": pdf_filename,
        "source": source,
        "url": url,
        "doi": doi,
        "title": title,
        "timestamp": datetime.now().isoformat(),
    }
    if extra:
        record.update(extra)

    try:
        _PROVENANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if _PROVENANCE_PATH.exists():
            existing = _json.loads(
                _PROVENANCE_PATH.read_text(encoding="utf-8")
            )
        existing.append(record)
        _PROVENANCE_PATH.write_text(
            _json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass  # non-critical


def log_confidence(
    phase: str,
    task: str,
    confidence: float,
    outcome: str = "",
    pdf_name: str = "",
    extra: dict = None,
):
    """
    Append one confidence record to the JSONL calibration log.

    Call this after every prediction where the LLM returns a confidence score.
    `outcome` is filled in later during verification (supported/unsupported/correct/incorrect).
    """
    from diffai.xrdreader.config import (
        CONFIDENCE_LOG_PATH,
        ENABLE_CONFIDENCE_LOGGING,
    )

    if not ENABLE_CONFIDENCE_LOGGING:
        return

    record = {
        "timestamp": datetime.now().isoformat(),
        "phase": phase,
        "task": task,
        "confidence": round(confidence, 4),
        "outcome": outcome,
        "pdf_name": pdf_name,
    }
    if extra:
        record.update(extra)

    try:
        CONFIDENCE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIDENCE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # non-critical — don't crash the pipeline


def humanize_llm_error(exc: Exception) -> str:
    """Turn a raw provider/SDK exception into a short, actionable message.

    Recognizes the common LLM failure modes; falls back to the original text
    (trimmed) for anything unrecognized.
    """
    msg = str(exc)
    low = msg.lower()
    if "non-serverless" in low or "model_not_available" in low:
        return (
            "that model isn't available on your account on-demand — it needs a paid "
            "dedicated endpoint, or choose a serverless model (see together.ai/models)."
        )
    if "input validation error" in low or (
        "image" in low and "not support" in low
    ):
        return (
            "this model can't read images. Steps that look at figures (Step I, and "
            "sometimes Step III) need a VISION model — e.g. --set PHASE1_MODEL=gpt-4o."
        )
    if (
        "401" in msg
        or "unauthorized" in low
        or "invalid api key" in low
        or "incorrect api key" in low
    ):
        return "the API key was rejected — check the key for this provider is set and valid."
    if "429" in msg or "rate limit" in low:
        return "hit the provider's rate limit — wait a moment and retry, or slow the run down."
    if "timed out" in low or "timeout" in low:
        return (
            "the model call timed out. Raise the budget with --set LLM_TIMEOUT=900, "
            "or switch to a faster model."
        )
    return msg[:300]
