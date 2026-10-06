"""Phase 0 -- LLM screening that keeps only XRD-relevant PDFs.

Two modes, chosen by ENABLE_AGENTIC_PHASE0:
- agentic (run_phase0_agent): the model drives a small tool loop over the PDF
  (abstract, page text, regex search, table of contents) then calls `finalize`.
- single-shot (screen_pdf_single_shot): evidence is gathered locally and sent
  in one call.
Decisions are cached per (pdf, model). Each screened PDF is copied into
PHASE0_KEEP_DIR / PHASE0_REJECT_DIR alongside a `*__phase0.json` record.
"""

import hashlib
import json
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz
from openai import OpenAI

from diffai.xrdreader.config import (
    ENABLE_AGENTIC_PHASE0,
    MODEL,
    PDF_DIR,
    PHASE0_KEEP_DIR,
    PHASE0_REJECT_DIR,
)
from diffai.xrdreader.utils import humanize_llm_error, log, make_safe_stem


# ======================================================================================
# Provider / model selection (unchanged interface)
# ======================================================================================
def get_phase0_provider() -> str:
    return (
        os.getenv("PHASE0_PROVIDER", os.getenv("PROVIDER", "gpt"))
        .strip()
        .lower()
    )


def get_phase0_model() -> str:
    return os.getenv("PHASE0_MODEL", os.getenv("MODEL", MODEL)).strip()


def build_client() -> Any:
    """Build the LLM client for the Phase 0 provider.

    gemini/claude return None here -- they use raw HTTP inside ToolCaller.
    """
    provider = get_phase0_provider()
    if provider == "gpt":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing OPENAI_API_KEY env var")
        return OpenAI(api_key=api_key)
    if provider == "grok":
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing XAI_API_KEY env var")
        return OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    if provider == "together":
        api_key = os.environ.get("TOGETHER_API_KEY")
        if not api_key:
            raise RuntimeError("Missing TOGETHER_API_KEY env var")
        return OpenAI(api_key=api_key, base_url="https://api.together.xyz/v1")
    if provider in {"gemini", "claude"}:
        return None
    raise RuntimeError(f"Unsupported Phase 0 provider: {provider}")


# ======================================================================================
# Agent knobs
# ======================================================================================
PHASE0_MAX_STEPS = int(os.getenv("PHASE0_MAX_STEPS", "6"))
PHASE0_MAX_API_RETRIES = int(os.getenv("PHASE0_MAX_API_RETRIES", "3"))
PHASE0_BASE_SLEEP_SEC = float(os.getenv("PHASE0_BASE_SLEEP_SEC", "1.0"))
PHASE0_USE_CACHE = os.getenv("PHASE0_USE_CACHE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PHASE0_CACHE_DIR = PHASE0_KEEP_DIR.parent / "_agent_cache_phase0"


# ======================================================================================
# Safe JSON parsing (handles markdown fences / trailing prose)
# ======================================================================================
def _extract_json_object(s: str) -> str:
    """Pull the first complete {...} JSON object out of a model reply.

    Handles markdown fences / trailing prose by brace-counting (ignoring
    braces inside strings) rather than a naive regex.
    """
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
        obj = _extract_json_object(s)
        return json.loads(obj) if obj else {}
    except Exception:
        return {}


def _sleep_backoff(attempt: int):
    time.sleep(PHASE0_BASE_SLEEP_SEC * (2**attempt) + random.random() * 0.25)


# ======================================================================================
# Tools — pure Python functions over an open fitz.Document
# Each tool returns a short string (the "observation" the model will see next turn).
# ======================================================================================
class Phase0Toolbox:
    """
    Holds the open PDF and exposes tool implementations.
    Kept stateless from the agent's POV; stateful only for caching/short-circuit.
    """

    def __init__(self, pdf_path: Path, max_pages: int = 200):
        self.pdf_path = pdf_path
        self.doc = fitz.open(str(pdf_path))
        self.n_pages = min(len(self.doc), max_pages)
        self._page_text_cache: Dict[int, str] = {}

    def close(self):
        try:
            self.doc.close()
        except Exception:
            pass

    # ---- helpers ------------------------------------------------------------
    def _page_text(self, page_1indexed: int) -> str:
        if page_1indexed in self._page_text_cache:
            return self._page_text_cache[page_1indexed]
        idx = page_1indexed - 1
        if idx < 0 or idx >= self.n_pages:
            return ""
        try:
            txt = self.doc[idx].get_text("text") or ""
        except Exception:
            txt = ""
        self._page_text_cache[page_1indexed] = txt
        return txt

    # ---- tool: get_abstract -------------------------------------------------
    def tool_get_abstract(self) -> str:
        """Return the abstract if found, else the top of page 1."""
        # Try first 3 pages; look for "abstract" anchor, then collect until stop token.
        joined = "\n".join(
            self._page_text(i) for i in range(1, min(4, self.n_pages + 1))
        )
        m = re.search(
            r"(?is)\babstract\b[:\s\-]*\n?(.+?)(?=\n\s*(?:keywords?|introduction|1\.|i\.)\b|\Z)",
            joined,
        )
        if m:
            out = re.sub(r"\s+", " ", m.group(1)).strip()
            return out[:2500] if out else "(no abstract text extracted)"
        # fallback: first ~1500 chars of page 1
        p1 = self._page_text(1)
        return re.sub(r"\s+", " ", p1)[:1500] or "(empty page 1)"

    # ---- tool: get_page_text -----------------------------------------------
    def tool_get_page_text(self, page: int, max_chars: int = 3000) -> str:
        """Return one 1-indexed page's cleaned text (length-bounded)."""
        if not isinstance(page, int):
            return "(error: page must be int)"
        if page < 1 or page > self.n_pages:
            return f"(error: page {page} out of range 1..{self.n_pages})"
        txt = self._page_text(page)
        if not txt.strip():
            return f"(page {page} empty)"
        return re.sub(r"\s+", " ", txt).strip()[:max_chars]

    # ---- tool: search_pdf ---------------------------------------------------
    def tool_search_pdf(
        self, pattern: str, max_hits: int = 8, snippet_chars: int = 160
    ) -> str:
        """Regex-search all pages; return (page, snippet) hits as JSON."""
        if not pattern or not isinstance(pattern, str):
            return "(error: empty pattern)"
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"(error: bad regex: {e})"

        hits = []
        for p in range(1, self.n_pages + 1):
            txt = self._page_text(p)
            if not txt:
                continue
            for m in rx.finditer(txt):
                a = max(0, m.start() - snippet_chars // 2)
                b = min(len(txt), m.end() + snippet_chars // 2)
                snip = re.sub(r"\s+", " ", txt[a:b]).strip()
                hits.append({"page": p, "snippet": snip})
                if len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                break

        if not hits:
            return "(no matches)"
        return json.dumps(hits, ensure_ascii=False)

    # ---- tool: get_toc ------------------------------------------------------
    def tool_get_toc(self) -> str:
        """Return the table of contents, or scanned section headings."""
        try:
            toc = self.doc.get_toc() or []
        except Exception:
            toc = []
        if not toc:
            # Fallback: scan for common section headings on first pages
            joined = "\n".join(
                self._page_text(i) for i in range(1, min(6, self.n_pages + 1))
            )
            headings = re.findall(
                r"(?im)^\s*(abstract|introduction|methods?|experimental|results?|discussion|conclusions?)\b.*$",
                joined,
            )
            if headings:
                return json.dumps(
                    [{"heading": h} for h in headings[:12]], ensure_ascii=False
                )
            return "(no table of contents available)"
        return json.dumps(
            [
                {"level": lvl, "title": title, "page": page}
                for lvl, title, page in toc[:30]
            ],
            ensure_ascii=False,
        )


# ======================================================================================
# Tool schema given to the model
# ======================================================================================
PHASE0_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_abstract",
        "description": "Return the paper's abstract text if detectable, otherwise the first ~1500 chars of page 1.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_page_text",
        "description": "Return the text of a specific page (1-indexed). Use to inspect a targeted page after a search hit.",
        "parameters": {
            "type": "object",
            "properties": {"page": {"type": "integer", "minimum": 1}},
            "required": ["page"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_pdf",
        "description": (
            "Regex search across the full PDF. Returns up to 8 (page, snippet) matches as JSON. "
            "Use for XRD-related terms like 'XRD|PXRD|X[- ]?ray diffraction|2θ|2theta|diffractogram|Rietveld'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_toc",
        "description": "Return the table of contents / section headings if available.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "finalize",
        "description": (
            "Submit the final screening decision and END the loop. "
            "decision must be 'keep' or 'reject'. confidence is 0..1. "
            "reason should cite evidence you gathered via the other tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["keep", "reject"]},
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "reason": {"type": "string"},
            },
            "required": ["decision", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
]


PHASE0_SYSTEM_PROMPT = """
You are an XRD (X-ray diffraction) pre-screening agent. Your goal is to decide whether a scientific PDF
contains enough XRD content to be worth sending to a downstream figure-extraction pipeline.

KEEP the PDF if XRD / PXRD / powder X-ray diffraction / 2θ plots / diffractograms /
Rietveld refinement are a substantive part of the paper (not merely a passing mention
or a one-line reference to another study).

REJECT if the paper is about a different topic, or if XRD only appears in citations,
acknowledgements, or as a tangential mention.

Strategy:
1. Start with get_abstract. Often enough to decide.
2. If unclear, use search_pdf with a broad XRD regex.
3. If search hits are ambiguous (e.g. only 1-2 matches, or matches are in references),
   inspect the specific pages with get_page_text before deciding.
4. Call finalize exactly once when you are confident, OR when you have gathered enough
   evidence that further tool calls are unlikely to change your decision.

Budget: {max_steps} tool calls total. Be efficient. If you run out of budget without
calling finalize, the system will default to reject with low confidence.

Return tool calls. Do not return plain text except inside a finalize call's `reason`.
""".strip()


# ======================================================================================
# Agent loop
# ======================================================================================
def _cache_key(pdf_path: Path, model: str) -> str:
    """Content+model hash used as the cache filename for a PDF."""
    h = hashlib.sha1()
    try:
        h.update(
            pdf_path.read_bytes()[:1_000_000]
        )  # first 1MB is plenty to key on
    except Exception:
        h.update(str(pdf_path).encode("utf-8"))
    h.update(model.encode("utf-8"))
    h.update(str(PHASE0_MAX_STEPS).encode("utf-8"))
    return h.hexdigest()


def _load_cached(pdf_path: Path, model: str) -> Optional[Dict]:
    """Return a cached Phase 0 result for this PDF+model, or None."""
    if not PHASE0_USE_CACHE:
        return None
    try:
        PHASE0_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp = PHASE0_CACHE_DIR / f"{_cache_key(pdf_path, model)}.json"
        if cp.exists():
            return json.loads(cp.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _save_cached(pdf_path: Path, model: str, payload: Dict) -> None:
    """Write this PDF's Phase 0 result into the agent cache."""
    if not PHASE0_USE_CACHE:
        return
    try:
        PHASE0_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp = PHASE0_CACHE_DIR / f"{_cache_key(pdf_path, model)}.json"
        cp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _call_model_with_retry(
    client: Any, model: str, input_messages: List[Dict], tools: List[Dict]
) -> Any:
    """Call the model, retrying with backoff on any error."""
    last_err = None
    for attempt in range(PHASE0_MAX_API_RETRIES + 1):
        try:
            return client.responses.create(
                model=model,
                input=input_messages,
                tools=tools,
            )
        except Exception as e:
            last_err = e
            log(
                f"[phase0] API retry {attempt+1}/{PHASE0_MAX_API_RETRIES+1}: {repr(e)}"
            )
            _sleep_backoff(attempt)
    raise last_err


def _execute_tool(toolbox: Phase0Toolbox, name: str, args: Dict) -> str:
    """Dispatch one tool call to the toolbox; return its observation string."""
    try:
        if name == "get_abstract":
            return toolbox.tool_get_abstract()
        if name == "get_page_text":
            return toolbox.tool_get_page_text(int(args.get("page", 0)))
        if name == "search_pdf":
            return toolbox.tool_search_pdf(str(args.get("pattern", "")))
        if name == "get_toc":
            return toolbox.tool_get_toc()
        return f"(error: unknown tool {name})"
    except Exception as e:
        return f"(error: tool {name} failed: {e})"


def run_phase0_agent(client: Any, pdf_path: Path) -> Dict:
    """
    Agentic screening loop — works with ALL providers via ToolCaller.
    """
    provider = get_phase0_provider()
    model = get_phase0_model()

    try:
        toolbox = Phase0Toolbox(pdf_path)
    except Exception as e:
        return {
            "decision": "reject",
            "confidence": 0.0,
            "reason": f"Unreadable PDF: {e}",
            "agent_trace": [],
            "steps_used": 0,
        }

    try:
        from diffai.xrdreader.tool_calling import ToolCaller

        caller = ToolCaller(
            provider,
            model,
            max_retries=PHASE0_MAX_API_RETRIES,
            base_sleep=PHASE0_BASE_SLEEP_SEC,
        )
        caller.set_system(
            PHASE0_SYSTEM_PROMPT.format(max_steps=PHASE0_MAX_STEPS)
        )
        caller.add_user_message(
            f"Screen the PDF named: {pdf_path.name}\n"
            "Begin by gathering evidence with tools, then call finalize."
        )

        trace = []
        final = None
        nudge_count = 0
        max_nudges = 2

        for step in range(PHASE0_MAX_STEPS):
            _t0 = time.time()
            tool_calls, _ = caller.call(PHASE0_TOOLS)
            elapsed = time.time() - _t0

            # Log usage
            from diffai.xrdreader.usage_tracker import get_tracker

            usage = caller.get_last_call_usage()
            get_tracker().log_call(
                phase="phase0",
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                wall_seconds=elapsed,
                pdf_name=f"{make_safe_stem(pdf_path.stem)}.pdf",
            )

            if not tool_calls:
                # Nudge is scoped to the `together` provider ONLY (open-source models
                # like Llama/Qwen sometimes reply in plain text instead of calling a
                # tool). Strong models (gpt/claude/gemini/grok) always call tools, so
                # they take the original path below (stop -> default reject) unchanged.
                if provider == "together" and nudge_count < max_nudges:
                    nudge_count += 1
                    log(
                        f"[phase0] step {step+1}: no tool calls; sending nudge #{nudge_count}"
                    )
                    caller.add_user_message(
                        "You must use the available tools. Do NOT respond with plain text. "
                        "Call get_abstract or search_pdf to gather evidence, then call "
                        "finalize with your decision. Use tool calls now."
                    )
                    trace.append(
                        {
                            "step": step + 1,
                            "tool": "(nudge)",
                            "args": {},
                            "observation": f"Nudge #{nudge_count}: model returned text instead of tools",
                        }
                    )
                    continue
                log(
                    f"[phase0] step {step+1}: no tool calls after {nudge_count} nudges; stopping."
                )
                break

            stop_loop = False
            for tc in tool_calls:
                name = tc["name"]
                args = tc["arguments"]
                call_id = tc["call_id"]

                if name == "finalize":
                    decision = (
                        str(args.get("decision", "reject")).strip().lower()
                    )
                    if decision not in {"keep", "reject"}:
                        decision = "reject"
                    # Robust confidence parsing: some models (e.g. Llama) omit the
                    # confidence field even when it's marked required in the schema.
                    raw_conf = args.get("confidence")
                    if raw_conf is not None:
                        try:
                            confidence = max(0.0, min(1.0, float(raw_conf)))
                        except Exception:
                            confidence = 0.0
                    else:
                        # Fallback: try to extract a float from the reason text
                        import re as _re

                        _m = _re.search(
                            r"confidence[:\s]*([01]\.?\d*)",
                            str(args.get("reason", "")),
                            _re.IGNORECASE,
                        )
                        if _m:
                            try:
                                confidence = max(
                                    0.0, min(1.0, float(_m.group(1)))
                                )
                            except Exception:
                                confidence = 0.75
                        else:
                            # Model provided a decision + reason but no confidence score
                            confidence = 0.75
                        log(
                            f"[phase0] finalize missing confidence field; inferred {confidence:.2f}"
                        )
                    reason = str(args.get("reason", "")).strip()
                    final = {
                        "decision": decision,
                        "confidence": confidence,
                        "reason": reason,
                    }
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

                observation = _execute_tool(toolbox, name, args)
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
            if provider == "together":
                # Together-ONLY fallback. A weak open-source model (Llama/Qwen) that
                # kept replying in plain text even after nudges, or gathered evidence
                # but never committed, would otherwise be blind-rejected. Instead fall
                # back to SINGLE-PASS screening: it pre-gathers the abstract + XRD
                # keyword hits locally and asks for ONE JSON decision, so the model
                # never has to drive a tool loop. Stops Llama rejecting everything
                # just because it won't call tools. Other providers are untouched.
                log(
                    f"[phase0] no finalize after {steps_used} steps; "
                    f"falling back to single-pass screening."
                )
                fb = screen_pdf_single_shot(pdf_path)
                final = {
                    "decision": fb["decision"],
                    "confidence": fb["confidence"],
                    "reason": f"[single-pass fallback] {fb['reason']}",
                }
                trace.append(
                    {
                        "step": steps_used + 1,
                        "tool": "(single-pass-fallback)",
                        "args": {},
                        "observation": str(fb.get("reason", ""))[:1000],
                    }
                )
                steps_used = len(trace)
            else:
                # Original behavior for gpt/claude/gemini/grok -- unchanged.
                final = {
                    "decision": "reject",
                    "confidence": 0.0,
                    "reason": f"Budget exhausted after {steps_used} tool calls.",
                }

        result = {**final, "agent_trace": trace, "steps_used": steps_used}

        # Confidence calibration log
        from diffai.xrdreader.utils import log_confidence

        log_confidence(
            phase="phase0",
            task="xrd_screening",
            confidence=float(result.get("confidence", 0)),
            outcome=result.get("decision", ""),
            pdf_name=pdf_path.name,
            extra={"mode": "agentic", "steps": steps_used},
        )
        return result

    finally:
        toolbox.close()


# ======================================================================================
# Single-pass (non-agentic) screening
# ======================================================================================
PHASE0_SINGLE_SHOT_PROMPT = """
You are an XRD (X-ray diffraction) pre-screening system. Decide whether a scientific PDF
contains enough XRD content to be worth sending to a downstream figure-extraction pipeline.

KEEP the PDF if XRD / PXRD / powder X-ray diffraction / 2θ plots / diffractograms /
Rietveld refinement are a substantive part of the paper (not merely a passing mention
or a one-line reference to another study).

REJECT if the paper is about a different topic, or if XRD only appears in citations,
acknowledgements, or as a tangential mention.

Below is the ABSTRACT (or first page text) and a KEYWORD SEARCH summary from the PDF.
Based on this evidence, return a JSON object with exactly these keys:
  {"decision": "keep" or "reject", "confidence": 0.0-1.0, "reason": "..."}

Return ONLY valid JSON. No markdown fences, no extra text.
""".strip()


def screen_pdf_single_shot(pdf_path: Path) -> Dict:
    """
    Non-agentic single-pass screening: gathers evidence locally, sends ONE LLM call.
    Returns same dict shape as run_phase0_agent: {decision, confidence, reason, agent_trace, steps_used}.
    """
    provider = get_phase0_provider()
    model = get_phase0_model()

    try:
        toolbox = Phase0Toolbox(pdf_path)
    except Exception as e:
        return {
            "decision": "reject",
            "confidence": 0.0,
            "reason": f"Unreadable PDF: {e}",
            "agent_trace": [],
            "steps_used": 0,
        }

    try:
        abstract = toolbox.tool_get_abstract()
        search_result = toolbox.tool_search_pdf(
            r"XRD|PXRD|X[- ]?ray diffraction|2θ|2theta|diffractogram|Rietveld|powder.diffract"
        )
        toc = toolbox.tool_get_toc()
    finally:
        toolbox.close()

    evidence_text = (
        f"ABSTRACT:\n{abstract}\n\n"
        f"KEYWORD SEARCH HITS:\n{search_result}\n\n"
        f"TABLE OF CONTENTS:\n{toc}"
    )

    from diffai.xrdreader.tool_calling import ToolCaller

    caller = ToolCaller(provider, model, max_retries=PHASE0_MAX_API_RETRIES)
    caller.add_user_message(PHASE0_SINGLE_SHOT_PROMPT + "\n\n" + evidence_text)

    try:
        _t0 = time.time()
        tool_calls, text_response = caller.call([])
        elapsed = time.time() - _t0

        from diffai.xrdreader.usage_tracker import get_tracker

        usage = caller.get_usage()
        get_tracker().log_call(
            phase="phase0",
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            wall_seconds=elapsed,
            pdf_name=f"{make_safe_stem(pdf_path.stem)}.pdf",
        )

        raw = text_response or ""
        obj = _safe_json_loads(raw)

        decision = str(obj.get("decision", "reject")).lower()
        if decision not in ("keep", "reject"):
            decision = "reject"

        confidence = float(obj.get("confidence", 0.0))
        reason = str(obj.get("reason", raw[:500]))

        result = {
            "decision": decision,
            "confidence": confidence,
            "reason": reason,
            "agent_trace": [
                {"mode": "single_shot", "evidence_length": len(evidence_text)}
            ],
            "steps_used": 1,
        }
        from diffai.xrdreader.utils import log_confidence

        log_confidence(
            phase="phase0",
            task="xrd_screening",
            confidence=confidence,
            outcome=decision,
            pdf_name=pdf_path.name,
            extra={"mode": "single_shot"},
        )
        return result
    except Exception as e:
        return {
            "decision": "reject",
            "confidence": 0.0,
            "reason": f"Single-shot LLM call failed: {e}",
            "agent_trace": [],
            "steps_used": 0,
        }


# ======================================================================================
# Back-compat shim: same signature as the old one-shot function.
# Keeps any external caller working.
# ======================================================================================
def phase0_decision(client: Any, evidence: dict) -> dict:
    """
    Backwards-compatible wrapper. If called with a dict carrying 'pdf_path', uses the
    agent loop; otherwise degrades gracefully to reject-with-reason.
    """
    pdf_path_str = (evidence or {}).get("pdf_path") or (evidence or {}).get(
        "pdf_name"
    )
    if not pdf_path_str:
        return {
            "decision": "reject",
            "confidence": 0.0,
            "reason": "phase0_decision called without a pdf path in evidence dict.",
        }
    pdf_path = Path(pdf_path_str)
    if ENABLE_AGENTIC_PHASE0:
        result = run_phase0_agent(client, pdf_path)
    else:
        result = screen_pdf_single_shot(pdf_path)
    return {
        "decision": result["decision"],
        "confidence": result["confidence"],
        "reason": result["reason"],
    }


def extract_phase0_evidence(pdf_path: Path, max_pages: int = 8) -> dict:
    """
    Preserved for back-compat with any external script that imported this.
    The agent itself no longer uses this bulk-sampled blob — it gathers evidence
    adaptively via tools.
    """
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        return {
            "pdf_name": pdf_path.name,
            "pdf_path": str(pdf_path),
            "error": str(e),
            "sampled_text": "",
        }

    try:
        chunks = []
        for i in range(min(max_pages, len(doc))):
            text = doc[i].get_text("text") or ""
            chunks.append(f"\n--- PAGE {i+1} ---\n{text[:4000]}")
        return {
            "pdf_name": pdf_path.name,
            "pdf_path": str(pdf_path),
            "sampled_text": "\n".join(chunks)[:20000],
        }
    finally:
        doc.close()


# ======================================================================================
# Main — same IO contract as the original
# ======================================================================================
def main():
    """Screen every PDF in PDF_DIR and sort it into keep/reject.

    For each PDF: reuse a cached decision if present, else run the agentic or
    single-shot screener; copy the PDF into the keep/reject folder and write a
    `*__phase0.json` with the decision + agent trace.
    """
    PHASE0_KEEP_DIR.mkdir(parents=True, exist_ok=True)
    PHASE0_REJECT_DIR.mkdir(parents=True, exist_ok=True)

    # provider = get_phase0_provider() # commented-out because we don't need it here; the client is built below
    client = build_client()  # None for gemini/claude
    model = get_phase0_model()

    mode = "agentic" if ENABLE_AGENTIC_PHASE0 else "single-shot"
    log(f"Phase 0 mode: {mode}")

    errors = 0
    screened = 0

    for pdf_path in sorted(PDF_DIR.rglob("*.pdf")):
        if not pdf_path.exists():
            continue

        log(f"Phase 0 screening: {pdf_path.name}")
        screened += 1

        cached = _load_cached(pdf_path, model)
        try:
            if cached is not None:
                result = cached
                log(f"Phase 0 cache hit: {pdf_path.name}")
            elif ENABLE_AGENTIC_PHASE0:
                result = run_phase0_agent(client, pdf_path)
                _save_cached(pdf_path, model, result)
            else:
                result = screen_pdf_single_shot(pdf_path)
                _save_cached(pdf_path, model, result)
        except Exception as e:
            friendly = humanize_llm_error(e)
            log(f"[phase0] screening error for {pdf_path.name}: {friendly}")
            # 'error' is deliberately not 'reject'. A missing or invalid API
            # key raises here for every PDF, and recording that as a screening
            # decision produced a run that rejected the whole corpus, wrote a
            # plausible consolidated log and exited 0 -- an infrastructure
            # failure indistinguishable from a scientific result.
            result = {
                "decision": "error",
                "confidence": 0.0,
                "reason": f"Phase 0 screening error: {friendly}",
                "agent_trace": [],
                "steps_used": 0,
            }
            errors += 1

        keep = result["decision"] == "keep"
        out_dir = PHASE0_KEEP_DIR if keep else PHASE0_REJECT_DIR

        safe_base = make_safe_stem(pdf_path.stem)

        try:
            shutil.copy2(pdf_path, out_dir / f"{safe_base}.pdf")
        except Exception as e:
            log(f"[phase0] copy failed for {pdf_path.name}: {e}")

        out_payload = {
            "source_pdf": str(pdf_path),
            "decision": {
                "decision": result["decision"],
                "confidence": result["confidence"],
                "reason": result["reason"],
            },
            "agent_trace": result.get("agent_trace", []),
            "steps_used": result.get("steps_used", 0),
            "model": model,
            "max_steps": PHASE0_MAX_STEPS,
        }

        with open(
            out_dir / f"{safe_base}__phase0.json", "w", encoding="utf-8"
        ) as f:
            json.dump(out_payload, f, indent=2, ensure_ascii=False)

        log(
            f"Phase 0 result -> {pdf_path.name}: "
            f"{result['decision'].upper()} | "
            f"confidence={result['confidence']:.2f} | "
            f"steps={result.get('steps_used', 0)} | "
            f"reason={result['reason']}"
        )

    if errors:
        log(f"Phase 0: {errors} of {screened} PDF(s) could not be screened.")
    if screened and errors == screened:
        # Every single PDF failed, so this is the pipeline's own problem --
        # almost always a missing or invalid API key -- not a property of the
        # corpus. Fail loudly instead of reporting a clean run with nothing
        # kept.
        raise RuntimeError(
            f"Phase 0 failed on all {screened} PDF(s); no document could be "
            "screened. Check the API key and model for the selected "
            "provider. See the log above for the underlying error."
        )


if __name__ == "__main__":
    main()
