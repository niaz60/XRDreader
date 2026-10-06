# XRDreader — full CLI reference

Every command is `diffai-xrdreader [options]` (or, equivalently,
`python -m diffai.xrdreader.app [options]`). This is the complete reference — every flag and every combination.

> **New here?** Start with **[HOW_TO_RUN.md](HOW_TO_RUN.md)** — a short, friendly guide. Come back
> here when you want the full list of options and combinations.

**Before you start**
- **Preview any command for free:** add `--dry-run`. It prints the resolved configuration and exits
  — no API calls, no downloads. It works on *every* command in this file. Remove it to run for real.
- Use **Python 3.12+**, with the environment you installed into active (`conda activate …`).
  Type `diffai-xrdreader …` (or `python -m diffai.xrdreader.app …`). The Windows launcher
  `py -3.13` bypasses the active environment and will not find the package.
- **Results go to a fresh timestamped folder** by default: `xrdreader_output/<date>_<time>/`
  (each run is self-contained, with `documents/ · results/ · logs/` inside). Use `-o` to name it.
- **Set an API key first:** `$env:OPENAI_API_KEY="sk-..."` (PowerShell).

---

## Contents

1. [Preview and help](#1-preview-and-help)
2. [Choose which steps run](#2-choose-which-steps-run)
3. [Run on your own PDFs](#3-run-on-your-own-pdfs)
4. [How many PDFs to download](#4-how-many-pdfs-to-download)
5. [Search terms](#5-search-terms)
6. [Download sources](#6-download-sources)
7. [Choose the AI provider](#7-choose-the-ai-provider)
8. [Choose the model](#8-choose-the-model)
9. [Agentic vs single-pass](#9-agentic-vs-single-pass)
10. [License filter](#10-license-filter)
11. [Mix models across steps](#11-mix-models-across-steps)
12. [Advanced: set any config value](#12-advanced-set-any-config-value)
13. [Where results go (and resuming runs)](#13-where-results-go-and-resuming-runs)
14. [Ready-made recipes](#14-ready-made-recipes)
15. [Commands that should fail](#15-commands-that-should-fail)
- [Cleaning up](#cleaning-up)

---

## 0. Web interface

```bash
diffai-xrdreader --ui          # open the browser UI instead of running in the terminal
```

Runs from the current folder, so results land beside a CLI run's output.

---

## 1. Preview and help
Check a command without spending anything. `--dry-run` works on every command below.

```bash
diffai-xrdreader --help
diffai-xrdreader --full-run --dry-run
diffai-xrdreader --full-run -n 5 --sources arxiv,springer --dry-run
```

---

## 2. Choose which steps run
Turn individual pipeline steps on or off.

```bash
# default: download + Step 0 only
diffai-xrdreader

# everything (download -> Step 0 -> I -> II -> clean -> Step III)
diffai-xrdreader --full-run

# everything, but on PDFs you already have (see section 3)
diffai-xrdreader --full-run --skip-download

# download + run everything, but SKIP Step 0 screening (process EVERY downloaded PDF)
diffai-xrdreader --full-run --skip-screening

# just download, no screening (download only)
diffai-xrdreader --skip-screening

# explicit subsets via --steps  (names: download,step0,step1,step2,clean,step3)
diffai-xrdreader --steps download,step0            # just collect + screen
diffai-xrdreader --steps step1                     # figure ID only
diffai-xrdreader --steps step1,step2               # figure ID + metadata
diffai-xrdreader --steps step2,clean,step3         # enrich + clean + validate
diffai-xrdreader --steps step3                     # re-validate only
```
*Later steps read earlier steps' JSON from `results/`. Since each run defaults to a NEW timestamped
folder, running a later step alone finds nothing — point `-o` at the prior run's folder
([section 13](#13-where-results-go-and-resuming-runs)).*

---

## 3. Run on your own PDFs
Skip downloading and process a folder you point at (`-i`). Only the PDFs in that folder are used.

```bash
# process every PDF in a folder
diffai-xrdreader --full-run -i "C:\path\to\my_pdfs"

# the "3 of 5" case: drop the 3 you want into their own folder, point at it
diffai-xrdreader --full-run -i "C:\path\to\my_3_pdfs"
```
*`-i` / `--input-dir` auto-skips download; your other PDFs elsewhere stay untouched.*

> ⚠️ **`-i` feeds PDFs into step0/step1 only — it does NOT resume a later step.**
> Step II/III read the *JSON* an earlier phase wrote into a run's `results/`, not PDFs.
> To re-run a later step on a finished run, use `-o <that run's ROOT folder>`
> ([section 13](#13-where-results-go-and-resuming-runs)),
> e.g. `--steps step3 -o xrdreader_output\2026-08-07_144835`. Pointing `-i` at a run's
> `documents/` folder just starts a fresh empty run and the later step finds nothing to do.

---

## 4. How many PDFs to download
`-n` sets how many papers to download **per source**.

```bash
diffai-xrdreader --full-run -n 1     # smoke test
diffai-xrdreader --full-run -n 5
diffai-xrdreader --full-run -n 50    # large harvest
```
*With 3 sources, `-n 5` = 15 downloads.*

---

## 5. Search terms
What to search for — material (`--elements`) and technique (`--technique`), AND-combined.

```bash
diffai-xrdreader --full-run --elements "Cu OR Copper"
diffai-xrdreader --full-run --elements "Mo OR Molybdenum"
diffai-xrdreader --full-run --elements "Fe OR Cu"
diffai-xrdreader --full-run --elements ""                       # material-independent (generic)
diffai-xrdreader --full-run --technique "XRD OR PXRD"
diffai-xrdreader --full-run --elements "Al alloys" --technique "powder diffraction"
```

---

## 6. Download sources
Where to fetch PDFs from. Springer and Elsevier need their own API key.

```bash
diffai-xrdreader --full-run --sources arxiv
diffai-xrdreader --full-run --sources springer
diffai-xrdreader --full-run --sources elsevier
diffai-xrdreader --full-run --sources crossref
diffai-xrdreader --full-run --sources arxiv,springer
diffai-xrdreader --full-run --sources arxiv,springer,elsevier,crossref
```

---

## 7. Choose the AI provider
Switch the AI provider — `gpt`, `gemini`, `claude`, `grok`, or `together`. Each needs its own key.

```bash
diffai-xrdreader --full-run --provider gpt
diffai-xrdreader --full-run --provider gemini --model gemini-2.5-flash
diffai-xrdreader --full-run --provider claude --model claude-sonnet-4-5
diffai-xrdreader --full-run --provider grok   --model grok-4.1
# open-source models (Llama / Qwen / DeepSeek) via Together AI — needs TOGETHER_API_KEY
diffai-xrdreader --full-run --provider together --model "meta-llama/Llama-3.3-70B-Instruct-Turbo"
```
*`together` uses Together AI's OpenAI-compatible API — pass the full Together model id as `--model`.*

> ⚠️ **A bare `--full-run --provider together` with a text-only model (like Llama-3.3-70B)
> WILL FAIL at Step I**, which needs a *vision* model, and only *serverless* Together
> models run at all. Don't point `together` at a full run blindly — read
> [section 11](#11-mix-models-across-steps) first; it has the two rules and a working recipe.

---

## 8. Choose the model
Same provider, different model — the biggest speed/cost lever.

```bash
diffai-xrdreader --full-run --model gpt-5.2        # strongest, slowest
diffai-xrdreader --full-run --model gpt-4.1        # mid
diffai-xrdreader --full-run --model gpt-4.1-mini   # fast/cheap (recommended for testing)
diffai-xrdreader --full-run --model gpt-4.1-nano   # cheapest
```

---

## 9. Agentic vs single-pass
Agentic tool-loops (default) vs one AI call per step (much faster, a bit less thorough).

```bash
diffai-xrdreader --full-run                       # agentic (ReAct tool loops)
diffai-xrdreader --full-run --single-pass         # one LLM call per step
```

---

## 10. License filter
Keep everything, or only Creative-Commons-licensed downloads.

```bash
diffai-xrdreader --full-run --sources crossref
diffai-xrdreader --full-run --sources crossref --cc-only
```

---

## 11. Mix models across steps
Every step can use its **own** provider + model. The global `--provider`/`--model` set the
default; per-step `--set` overrides win. Put a cheap model on the easy steps and a strong
(or **vision**) model only where it's needed.

**Per-step keys** — set with `--set KEY=VALUE`:

| Step | Provider key | Model key | The model must be… |
|---|---|---|---|
| Step 0 — screening    | `PHASE0_PROVIDER` | `PHASE0_MODEL` | text — any model works |
| Step I — figure ID    | `PHASE1_PROVIDER` | `PHASE1_MODEL` | **vision / multimodal — REQUIRED** (it sends page + figure images) |
| Step II — metadata    | `PHASE2_PROVIDER` | `PHASE2_MODEL` | text — any model works |
| Step III — validation | `VERIFY_PROVIDER` | `VERIFY_MODEL` | **vision recommended** (its audit agent can open figures) |

Anything you don't override falls back to the global `--provider`/`--model` (then `config.py`).
`--dry-run` prints the resolved per-step providers/models — use it to check the mix.

### Two hard rules when choosing a model for a step
1. **Vision steps need a multimodal model.** Step I (and ideally Step III) send images.
   A **text-only** model → `400 Input validation error`.
   Text-only examples: `meta-llama/Llama-3.3-70B-Instruct-Turbo` and most `*-Instruct` LLMs.
   Vision examples: `gpt-4o`, `gemini-2.5-flash`, `claude-sonnet-4-5`, or a Together
   `*-Vision-*` / Llama-4 model.
2. **Open-access (Together) models must be SERVERLESS on _your_ account** — otherwise:
   `400 … Unable to access non-serverless model … create a dedicated endpoint`.
   Serverless availability is **per-account and changes over time**; a model that ran last
   month can require a paid dedicated endpoint now. **Test before a full run** (snippet below).
   Note: an account can have serverless *text* but **no serverless vision** — in that case
   run the open-source model on the text steps and a cloud model on Step I (recipe 2).

### Recipes

```bash
# 1) Cheapest sane all-GPT mix: nano to screen, gpt-4o to SEE figures, mini/4.1 for the rest
diffai-xrdreader --full-run \
  --set PHASE0_MODEL=gpt-4.1-nano --set PHASE1_MODEL=gpt-4o \
  --set PHASE2_MODEL=gpt-4.1-mini --set VERIFY_MODEL=gpt-4.1

# 2) Open-source where you can, cloud only where you must  <-- use this if your Together
#    account has NO serverless vision. Llama (text) does Steps 0/II; GPT-4o does the
#    vision Step I + validation.
diffai-xrdreader --full-run \
  --provider together --model "meta-llama/Llama-3.3-70B-Instruct-Turbo" \
  --set PHASE1_PROVIDER=gpt --set PHASE1_MODEL=gpt-4o \
  --set VERIFY_PROVIDER=gpt --set VERIFY_MODEL=gpt-4o

# 3) Claude overall, Gemini for validation only
diffai-xrdreader --full-run \
  --provider claude --model claude-sonnet-4-5 \
  --set VERIFY_PROVIDER=gemini --set VERIFY_MODEL=gemini-2.5-flash

# 4) All-Together / all-open-source: ONLY works if your account has serverless VISION.
#    Put a serverless vision model on Step I (+ Step III); text Llama elsewhere.
diffai-xrdreader --full-run \
  --provider together --model "meta-llama/Llama-3.3-70B-Instruct-Turbo" \
  --set PHASE1_MODEL="<a serverless vision model on your account>" \
  --set VERIFY_MODEL="<a serverless vision model on your account>"
```

### Test a Together model BEFORE a full run (serverless check, ~free)
A 1-token ping tells you instantly whether a model runs on your account:
```bash
python -c "import os; from openai import OpenAI; c=OpenAI(api_key=os.environ['TOGETHER_API_KEY'], base_url='https://api.together.xyz/v1'); c.chat.completions.create(model='META/MODEL-ID', messages=[{'role':'user','content':'hi'}], max_tokens=1); print('serverless OK')"
```
`serverless OK` → the model is reachable on your account. A `non-serverless` /
`model_not_available` error → you'd need a paid dedicated endpoint (or a different model).
(This proves reachability; that a *vision* model also accepts images is confirmed once Step I runs.)

---

## 12. Advanced: set any config value
`--set` sets any variable in `config.py` directly. Repeatable, applied last.

```bash
diffai-xrdreader --full-run --set PHASE3_AGENT_MAX_STEPS=4    # validation budget
diffai-xrdreader --full-run --set DISABLE_ALL_AGENTS=true     # == --single-pass
diffai-xrdreader --full-run --set TARGET_DOWNLOADS=3          # == -n 3
diffai-xrdreader --full-run --set REQUIRE_CC_LICENSE=true     # == --cc-only
```

---

## 13. Where results go (and resuming runs)
Choose the output folder. Default = a fresh timestamped folder, so runs never overwrite each other.

```bash
# default: ./xrdreader_output/<date>_<time>/   (auto-isolates every run)
diffai-xrdreader --full-run -n 1

# custom named root
diffai-xrdreader --full-run -n 1 -o mo_run
diffai-xrdreader --full-run -n 1 -o "D:\xrd_results\cu_batch"

# re-run a later step on a PREVIOUS run — point -o at that run's folder
diffai-xrdreader --steps step3 -o xrdreader_output\2026-08-07_143045
```
*Each root contains `documents/ · results/ · logs/`. Different `-o` = a separate, self-contained run.*

### Re-validate only (Step III on a previous run), optionally with a different validator

Point `-o` at a finished run's ROOT (its `results/` must hold `*__phase2_clean.json`). Step III
reads that JSON and writes `*__phase3_validated_FINAL.json` + `*__phase3_validation_log.json`
back into the SAME folder. Nothing upstream re-runs — no re-download, no re-extraction.

```bash
# 1) Re-validate with the SAME model the run used
diffai-xrdreader --steps step3 -o "xrdreader_output\2026-08-07_144835"

# 2) Second-opinion validator: a DIFFERENT LLM just for Step III (VERIFY_PROVIDER/VERIFY_MODEL)
diffai-xrdreader --steps step3 -o "xrdreader_output\2026-08-07_144835" \
  --set VERIFY_PROVIDER=claude --set VERIFY_MODEL=claude-sonnet-4-5

# 3) Cheap second-opinion validator (vision-capable — Step III can open figures)
diffai-xrdreader --steps step3 -o "xrdreader_output\2026-08-07_144835" \
  --set VERIFY_PROVIDER=gemini --set VERIFY_MODEL=gemini-2.5-flash
```
*`--provider/--model` set the default; `VERIFY_*` overrides **just the validator**
(see [section 11](#11-mix-models-across-steps)). Use a **multimodal** model — the Step III audit
agent may inspect a figure, and a text-only model would `400` if it does.*

> ⚠️ Re-running Step III **overwrites** that run's `*__phase3_validated_FINAL.json` and
> `*__phase3_validation_log.json`. To compare two validators side-by-side, copy the run
> folder first and validate the copy:
> ```bash
> Copy-Item -Recurse "xrdreader_output\2026-08-07_144835" "xrdreader_output\144835_claudeval"
> diffai-xrdreader --steps step3 -o "xrdreader_output\144835_claudeval" --set VERIFY_PROVIDER=claude --set VERIFY_MODEL=claude-sonnet-4-5
> ```

---

## 14. Ready-made recipes
Useful real-world combinations, copy-paste ready.

```bash
# Fast dev iteration (cheapest, quickest)
diffai-xrdreader --full-run -n 1 --single-pass --model gpt-4.1-mini

# Production-quality harvest into a named folder
diffai-xrdreader --full-run -n 20 --model gpt-5.2 -o cu_production

# Process YOUR own PDFs, full quality
diffai-xrdreader --full-run -i "C:\path\to\my_pdfs"

# Fastest-per-paper model (per the paper: Claude Sonnet)
diffai-xrdreader --full-run -n 5 --provider claude --model claude-sonnet-4-5

# CC-only large collection from multiple sources
diffai-xrdreader --full-run -n 50 --sources arxiv,crossref --cc-only -o cc_harvest

# Mo study, cheap, arxiv only, named run
diffai-xrdreader --full-run -n 3 --elements "Mo OR Molybdenum" --sources arxiv --model gpt-4.1-mini -o mo_test
```

---

## 15. Commands that should fail
These should each print a one-line `error: …` and exit non-zero — handy for testing input validation.

```bash
diffai-xrdreader --sources reddit          # unknown source
diffai-xrdreader --steps bogus             # unknown step
diffai-xrdreader --provider chatgpt        # invalid provider choice
diffai-xrdreader --set JUSTAKEY            # --set without '='
diffai-xrdreader -n five                   # -n not an integer
diffai-xrdreader --turbo                   # unknown flag
```

---

## Cleaning up
Each run is its own timestamped folder, so you rarely need to "reset" — just delete the ones you
don't want. To clear all runs:
```bash
python -c "import shutil; shutil.rmtree('xrdreader_output', ignore_errors=True)"
```
