# How to run ERAF4XRD

ERAF4XRD extracts X-ray diffraction (XRD) data — **figures and metadata** — from scientific PDFs.
It downloads papers, screens them for XRD content, finds the XRD figures, extracts the metadata,
links figures to metadata, and automatically double-checks the result.

**Contents**
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Options](#options)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)

---

## Installation

Requires **Python 3.12+**. A fresh conda environment is recommended:

```bash
# 1. create and activate an environment
conda create -n eraf4xrd python=3.13 -y
conda activate eraf4xrd

# 2. install ERAF4XRD (run from the project folder, where pyproject.toml lives)
pip install .
```

`pip install .` installs ERAF4XRD with all its dependencies and adds the `diffai-eraf4xrd`
command. Run it either way — they are identical:

```bash
diffai-eraf4xrd --help                     # the installed command
py -3.13 -m diffai.eraf4xrd.app --help     # works without installing
```

> **Cross-platform note:** the examples below use `py -3.13 -m diffai.eraf4xrd.app …`, which is
> the **Windows** Python launcher. On **macOS / Linux** that `py` command does not exist — use the
> installed `diffai-eraf4xrd …` command instead (recommended, identical on every OS), or replace
> `py -3.13` with `python3`, e.g. `python3 -m diffai.eraf4xrd.app …`.

---

## Quick start

```bash
py -3.13 -m diffai.eraf4xrd.app
```

By default it downloads **2 PDFs** from arXiv and **only screens them** for XRD content — it does
**not** extract figures or metadata. Add options (see [Options](#options)) to do more.

> 💡 **Preview any command for free:** add `--dry-run`. It prints what *would* happen — which steps
> run, which model, where results go — then stops. No downloads, no API calls, no cost.

---

## Configuration

Set your key(s) as environment variables before running. In PowerShell:

```bash
$env:OPENAI_API_KEY="sk-..."      # required — or a GEMINI / ANTHROPIC / XAI / TOGETHER key
$env:UNPAYWALL_EMAIL="you@email.com"   # only if you download from CrossRef
$env:SPRINGER_API_KEY="..."       # only if you download from Springer
```

You always need **one AI provider key**. Beyond that, **each download source needs its own
credential** — except **arXiv**, which is fully open:

| `--sources` value | Credential needed |
|---|---|
| `arxiv` | none — fully open (best for a quick test) |
| `crossref` | `UNPAYWALL_EMAIL` — any real email (required by the Unpaywall API) |
| `springer` | `SPRINGER_API_KEY` |
| `elsevier` | `ELSEVIER_API_KEY` |

> Tip: instead of setting an environment variable, you can pass any credential inline, e.g.
> `--set UNPAYWALL_EMAIL=you@email.com`.

---

## Options

| Option | What it does |
|---|---|
| `--full-run` | Run the whole pipeline: download → screen → figures → metadata → check |
| `--steps step0,step1` | Run only certain steps (`download,step0,step1,step2,clean,step3`) |
| `--skip-download` | Skip downloading; use PDFs already in the output folder |
| `--skip-screening` | Skip Step 0 screening — run the later steps on **every** downloaded PDF |
| `-i "C:\my\pdfs"` | Use PDFs you already have in a folder (skips downloading) |
| `-n 5` | How many papers to download per source |
| `--elements "Cu OR Copper"` | The material / element to search for |
| `--technique "XRD OR PXRD"` | The technique to search for (AND-combined with `--elements`) |
| `--sources arxiv` | Where to download from (`arxiv`, `springer`, `elsevier`, `crossref`) |
| `--cc-only` | Keep only Creative-Commons-licensed downloads |
| `--provider gpt` | Which AI provider (`gpt`, `gemini`, `claude`, `grok`, `together`) |
| `--model gpt-4o` | Which model |
| `--single-pass` | One AI call per step instead of the agentic loop (faster, cheaper) |
| `--set KEY=VALUE` | Set any advanced config value directly (repeatable) |
| `-o "my_run"` | Put results in a folder you name |
| `--dry-run` | Preview a command without running it (no cost) |
| `--help` | List every option |

---

## Examples

### Run the full pipeline
```bash
py -3.13 -m diffai.eraf4xrd.app --full-run
```

### Use PDFs you already have
Put your PDFs in a folder and point at it — no downloading, only those files get processed.
```bash
py -3.13 -m diffai.eraf4xrd.app --full-run -i "C:\my\pdfs"
```
To run on only some of the PDFs in a folder (say 3 of 5), put those in their own folder and point
`-i` at it.

### Choose what to download
```bash
py -3.13 -m diffai.eraf4xrd.app --full-run -n 5 --elements "Mo OR Molybdenum" --sources arxiv
```
`-n` is per source — `-n 5` with two sources downloads 10.

### Pick the AI model
Cheaper/faster for testing, stronger for real runs.
```bash
py -3.13 -m diffai.eraf4xrd.app --full-run --model gpt-4.1-mini                     # fast & cheap
py -3.13 -m diffai.eraf4xrd.app --full-run --provider claude --model claude-sonnet-4-5
```

### Run only certain steps
The steps, in order: **download → step0** (screen) **→ step1** (figures) **→ step2** (data)
**→ clean → step3** (check).
```bash
py -3.13 -m diffai.eraf4xrd.app --steps step0,step1
```
> Later steps need the earlier steps' results. Each run saves to a **new** folder, so running a
> later step on its own finds nothing — to continue a previous run, use `-o` (below).

### Where results are saved
Every run makes its own dated folder, so runs never overwrite each other:
```
eraf4xrd_output/2026-08-07_143022/
    documents/   ← the PDFs
    results/     ← all the JSON output
    logs/        ← the run log
```
Name the folder yourself instead:
```bash
py -3.13 -m diffai.eraf4xrd.app --full-run -o cu_run
```

### Resume a previous run (e.g. re-check only)
Point `-o` at the earlier run's folder. Example — re-run just the final data-check:
```bash
py -3.13 -m diffai.eraf4xrd.app --steps step3 -o "eraf4xrd_output\2026-08-07_143022"
```
Want a second opinion from a different model? Give that step its own validator:
```bash
py -3.13 -m diffai.eraf4xrd.app --steps step3 -o "eraf4xrd_output\2026-08-07_143022" \
  --set VERIFY_PROVIDER=claude --set VERIFY_MODEL=claude-sonnet-4-5
```

### Mix models across steps
Give each step its own model — a cheap one to screen, a strong one to read figures. Two rules:

1. **Reading figures needs a model that can *see* images.** Step 1 (and ideally step 3) look at
   pictures; a text-only model (like Llama-3.3-70B) will error there — use `gpt-4o`, `gemini`,
   `claude`, or a vision model for step 1.
2. **Open-source models (via `together`) must be available on your account.** Some require a paid
   dedicated endpoint. If you see a "non-serverless" error, pick a different model.

A combo that works well — open-source for the text steps, a cloud model for the figure step:
```bash
py -3.13 -m diffai.eraf4xrd.app --full-run \
  --provider together --model "meta-llama/Llama-3.3-70B-Instruct-Turbo" \
  --set PHASE1_PROVIDER=gpt --set PHASE1_MODEL=gpt-4o
```

---

## Troubleshooting

| You see… | What it means | What to do |
|---|---|---|
| `Input validation error` during step 1 | Your model can't see images | Use a vision model for step 1: `--set PHASE1_MODEL=gpt-4o` |
| `non-serverless model … dedicated endpoint` | That open-source model isn't free-to-call on your account | Pick a different model, or enable it on together.ai |
| It prints "Skipping…" and nothing runs | A later step couldn't find earlier results | Point `-o` at the previous run's folder |
| It downloads papers again | No `-i`, and downloading is on | Use `-i "C:\my\pdfs"` to reuse PDFs you already have |
| `ERROR: UNPAYWALL_EMAIL is not set` (or `..._API_KEY not set`) | That `--sources` choice needs a credential | Set it (see [Configuration](#configuration)) or pass `--set UNPAYWALL_EMAIL=you@email.com`; or use `--sources arxiv` (needs none) |
| Not sure what a command will do | — | Add `--dry-run` to preview it for free |
| `[WinError 206] … filename … too long` while installing (Windows only) | Your install folder path exceeds Windows' 260-character limit | Install from a short path like `C:\xrd`, or enable Windows long paths (set `LongPathsEnabled=1`) |

---

*Want every possible flag and combination? See [CLI_REFERENCE.md](CLI_REFERENCE.md) — the full reference.*
