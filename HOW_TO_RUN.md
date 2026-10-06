# How to run XRDreader

XRDreader extracts X-ray diffraction data from scientific papers: it downloads them, screens
them for XRD content, finds the XRD figures, extracts the crystallographic metadata, and
validates the result.

> Never used a command line before? **[TUTORIAL.md](TUTORIAL.md)** walks through the same
> thing one step at a time, showing what you should see after every command.

- [Requirements](#requirements) · [Install](#install) · [Run it](#run-it) · [Options](#options)
  · [Examples](#examples) · [Sources](#sources) · [Web interface](#web-interface)
  · [Troubleshooting](#troubleshooting)

---

## Requirements

| | |
|---|---|
| **conda** | <https://docs.conda.io/en/latest/miniconda.html> |
| **git** | <https://git-scm.com/downloads> (or download the repository as a ZIP) |
| **An AI key** | [OpenAI](https://platform.openai.com/api-keys) (default) · [Gemini](https://aistudio.google.com/app/apikey) · [Claude](https://console.anthropic.com/settings/keys) |

Python 3.12+ is required; the install below creates it for you.

> **On Windows, the simplest shell is the "Anaconda Powershell Prompt"** from the Start menu.
> It is the only one that knows `conda` without setup; a plain PowerShell window reports
> `conda : The term 'conda' is not recognized`.
>
> To use any other terminal instead, including the one inside VS Code, run `conda init
> powershell` once from the Anaconda prompt and reopen the terminal. If PowerShell then refuses
> to load your profile, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

---

## Install

```bash
git clone https://github.com/niaz60/XRDreader.git
cd XRDreader
conda create -n xrdreader python=3.13 -y
conda activate xrdreader
pip install .
```

`pip install .` installs the project **in the current folder**, which is why `cd` comes first.
To install from anywhere, give the path instead of `.`:

```bash
pip install "C:\path\to\XRDreader"
```

Verify:

```bash
diffai-xrdreader --help
```

---

## Run it

**Run XRDreader from any folder you like — you do not need to be in the project folder.**
Results are written into the folder you run from, so use a folder of your own:

```bash
mkdir C:\my_xrd_work
cd C:\my_xrd_work
diffai-xrdreader --full-run --sources arxiv -n 1
```

That downloads one arXiv paper, screens it, extracts and validates its XRD metadata. Expect 2-3
minutes and $0.25-$0.35.

Each run writes a new timestamped folder, so runs never overwrite each other:

```
xrdreader_output/2026-08-07_143022/
    documents/   downloaded PDFs
    results/     JSON output, figure crops
    logs/        run log
```

The file ending `__phase3_validated_FINAL.json` holds the final extracted data. Use `-o NAME`
to name the folder instead.

**Every run also writes `report.html` in that folder.** Double-click it to see what happened: which
papers were kept or rejected and why, the figures it found, the metadata it pulled out, and every
tool its agents called. It opens in your browser with no server and no network. Keep it in its
folder, since it reads the images beside it, or rebuild it anywhere with
`diffai-xrdreader --report FOLDER --embed-report` to get a copy you can send to someone.

**Two things reset when you open a new terminal** — repeat them each session:

```bash
conda activate xrdreader
$env:OPENAI_API_KEY="sk-..."
```

**Preview any command for free** by adding `--dry-run`. It prints the resolved configuration —
steps, model, output folder — and exits without downloading or calling the AI.

> Use `diffai-xrdreader` or `python -m diffai.xrdreader.app`. Do **not** use `py -3.13`: the
> Windows launcher ignores the active conda environment and reports `ModuleNotFoundError`.

---

## Options

| Option | Description |
|---|---|
| `--full-run` | Run every step: download → screen → figures → metadata → validate |
| `--steps LIST` | Run only these steps: `download,step0,step1,step2,clean,step3` |
| `--skip-download` | Use PDFs already in the output folder |
| `--skip-screening` | Skip screening; run later steps on every downloaded PDF |
| `-i DIR` | Use PDFs from your own folder (disables downloading) |
| `-n N` | Papers to download **per source** (default 2) |
| `--elements STR` | Material search terms (default `Cu OR Copper`) |
| `--technique STR` | Technique search terms |
| `--sources LIST` | `arxiv`, `springer`, `elsevier`, `crossref` |
| `--cc-only` | Keep only Creative-Commons-licensed papers |
| `--provider NAME` | `gpt`, `gemini`, `claude`, `grok`, `together` |
| `--model NAME` | Model name, e.g. `gpt-4.1-mini` |
| `--single-pass` | One AI call per step instead of letting it check its own work |
| `--set KEY=VALUE` | Set any config value directly (repeatable) |
| `-o DIR` | Name the output folder |
| `--dry-run` | Print the resolved configuration and exit — no cost |
| `--report DIR` | Build `report.html` for an earlier run and exit |
| `--embed-report` | Put the images inside the report so it can be sent on its own |
| `--no-report` | Do not write `report.html` when the run finishes |
| `--ui` | Launch the web interface |
| `--help` | Full option list |

---

## Examples

```bash
# your own PDFs instead of downloading
diffai-xrdreader --full-run -i "C:\my\pdfs"

# a different material, five papers
diffai-xrdreader --full-run -n 5 --elements "Mo OR Molybdenum"

# cheaper while experimenting
diffai-xrdreader --full-run --model gpt-4.1-mini --single-pass

# a different provider
diffai-xrdreader --full-run --provider claude --model claude-sonnet-4-5

# download papers only, no AI and no key needed
diffai-xrdreader --steps download --sources arxiv -n 10

# redo only the final validation of an earlier run
diffai-xrdreader --steps step3 -o "xrdreader_output\2026-08-07_143022"

# a different model per step
diffai-xrdreader --full-run --set PHASE0_MODEL=gpt-4.1-nano --set PHASE1_MODEL=gpt-4o
```

Steps run in order and each needs the previous one's output, so to continue an earlier run,
point `-o` at its folder.

**Step 1 reads images**, so it needs a vision-capable model. A text-only model such as
Llama-3.3-70B will fail there — use `gpt-4o`, Gemini, or Claude for that step.

---

## Sources

arXiv is fully open. The others need a credential:

| `--sources` | Credential |
|---|---|
| `arxiv` | none |
| `crossref` | `UNPAYWALL_EMAIL` — any real email address |
| `springer` | `SPRINGER_API_KEY` |
| `elsevier` | `ELSEVIER_API_KEY` |

```bash
$env:UNPAYWALL_EMAIL="you@email.com"
diffai-xrdreader --full-run --sources arxiv,crossref
```

Any setting can also be passed inline: `--set UNPAYWALL_EMAIL=you@email.com`.

---

## Web interface

```bash
diffai-xrdreader --ui
```

Opens at http://localhost:8501. Run it from the folder you want results in — the UI writes its
output folder to the current directory and its **Browse files** and **JSON outputs** tabs read
from there. Ctrl+C to stop. CIF export and Materials Project lookups need
`pip install ".[webapp]"`.

---

## Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `conda is not recognized` | conda is not set up for this shell, or not installed | Use the **Anaconda Powershell Prompt**, or run `conda init powershell` once to enable conda everywhere |
| `ModuleNotFoundError: No module named 'diffai.xrdreader'` | `py -3.13` used, or environment not active | `conda activate xrdreader`, then `diffai-xrdreader` |
| `Missing API key(s) for the steps this run would execute` | An enabled step has no key | Set the key it names, or use `--steps download` |
| `ERROR: ... ipykernel ... requires tornado` during install | Unrelated package in your environment | Ignore — check for `Successfully installed` below it |
| `Input validation error` in step 1 | Model cannot read images | `--set PHASE1_MODEL=gpt-4o` |
| `non-serverless model ... dedicated endpoint` | Model not enabled on your Together account | Choose another model |
| Prints "Skipping…" and does nothing | A later step found no input from an earlier one | Point `-o` at the previous run's folder |
| `[WinError 206] ... filename too long` | Path over the Windows 260-character limit | Install from a short path such as `C:\xrd` |

---

*Every flag and combination: [CLI_REFERENCE.md](CLI_REFERENCE.md).*
