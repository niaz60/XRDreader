# How to run ERAF4XRD

ERAF4XRD reads scientific papers and pulls out their X-ray diffraction data. It finds the
papers, picks out the XRD figures, extracts the crystallographic metadata, and checks its
own answers.

- [Install](#install)
- [Your first run](#your-first-run)
- [Common things to do](#common-things-to-do)
- [Where the results go](#where-the-results-go)
- [Download sources](#download-sources)
- [Web interface](#web-interface)
- [All options](#all-options)
- [Troubleshooting](#troubleshooting)

---

## Install

You need **conda** (also called Anaconda or Miniconda). If you do not have it, install
Miniconda first: <https://docs.conda.io/en/latest/miniconda.html>.
It is free, and it is what makes Step 2 work.

Then do these five steps once, in order.

**Step 1 — open a terminal in the ERAF4XRD folder.**

That is the folder you downloaded — the one this file is sitting in. In Windows Explorer, go
into that folder, then hold **Shift** and right-click on empty space and choose
*Open PowerShell window here*.

**Step 2 — create an environment and switch to it.**

```bash
conda create -n eraf4xrd python=3.13 -y
conda activate eraf4xrd
```

An environment is a private space for ERAF4XRD and the other software it needs, so it cannot
clash with anything else on your computer. You create it once; you switch it on every time.

**Step 3 — install ERAF4XRD.**

```bash
pip install .
```

The `.` means *the folder I am in right now*, which is why Step 1 matters.

**Step 4 — check it worked.**

```bash
diffai-eraf4xrd --help
```

If a list of options prints, you are ready.

**Step 5 — make a folder for your results and go there.**

```bash
mkdir C:\my_xrd_work
cd C:\my_xrd_work
```

From here on you can be in any folder you like — you never need to go back to the ERAF4XRD
folder. Results are saved wherever you happen to be, so it is worth having a folder for them.

> **Every new terminal needs `conda activate eraf4xrd` first.** If you close the terminal and
> open a new one, run that one line again before anything else. Everything below assumes it.
>
> Use `diffai-eraf4xrd ...` (or `python -m diffai.eraf4xrd.app ...`). Do **not** use the Windows
> launcher `py -3.13 ...` — it ignores the environment you just switched on and reports
> `ModuleNotFoundError: No module named 'diffai.eraf4xrd'` even after a successful install.

---

## Your first run

**1. Set your AI key.** In PowerShell:

```bash
$env:OPENAI_API_KEY="sk-..."
```

One AI key is all you need to start. OpenAI, Gemini, Anthropic, xAI or Together all work —
see [Download sources](#download-sources) if you want papers from somewhere other than arXiv.

**2. Preview it first — this is free.**

```bash
diffai-eraf4xrd --full-run --sources arxiv -n 1 --dry-run
```

`--dry-run` prints what *would* happen — which steps run, which model, where results go — then
stops. No downloads, no API calls, no cost. You can add it to any command on this page.

**3. Run it for real.**

```bash
diffai-eraf4xrd --full-run --sources arxiv -n 1
```

This downloads one arXiv paper about copper and XRD, screens it, finds the XRD figures,
extracts the metadata and validates it. Expect about **2 minutes** and roughly **$0.30**. Add
`--model gpt-4.1-mini` to bring that under $0.10 while you are experimenting.

---

## Common things to do

| I want to… | Command |
|---|---|
| Use PDFs I already have | `diffai-eraf4xrd --full-run -i "C:\my\pdfs"` |
| Search for a different material | `diffai-eraf4xrd --full-run --elements "Mo OR Molybdenum"` |
| Get more papers | `diffai-eraf4xrd --full-run -n 5` |
| Spend less | `diffai-eraf4xrd --full-run --model gpt-4.1-mini` |
| Go faster and cheaper still | `diffai-eraf4xrd --full-run --single-pass` |
| Use Claude instead of GPT | `diffai-eraf4xrd --full-run --provider claude --model claude-sonnet-4-5` |
| Name my own output folder | `diffai-eraf4xrd --full-run -o my_run` |
| Only download papers, no AI | `diffai-eraf4xrd --steps download` |
| Keep only Creative-Commons papers | `diffai-eraf4xrd --full-run --cc-only` |

Two things worth knowing:

- `-n` is **per source**, so `-n 5` with two sources downloads 10 papers.
- `-i` points at a folder of your own PDFs and turns downloading off. To process only some of
  the PDFs in a folder, put those in their own folder and point `-i` at that.

---

## Where the results go

Every run creates its own dated folder in whatever directory you ran the command from, so runs
never overwrite each other:

```
eraf4xrd_output/2026-08-07_143022/
    documents/   the PDFs it downloaded
    results/     all the JSON output
    logs/        the run log
```

Use `-o my_run` to name the folder yourself.

**To continue an earlier run**, point `-o` at that run's folder. For example, to redo only the
final validation step:

```bash
diffai-eraf4xrd --steps step3 -o "eraf4xrd_output\2026-08-07_143022"
```

The steps run in this order, and each one needs the previous one's results:

**download → step0** (screen) **→ step1** (figures) **→ step2** (metadata) **→ clean → step3** (validate)

Running a later step on its own in a fresh folder will find nothing to work on — that is what
`-o` is for.

---

## Download sources

arXiv is fully open and needs no credential, which makes it the best place to start. The others
each need their own:

| `--sources` value | What you need |
|---|---|
| `arxiv` | nothing |
| `crossref` | `UNPAYWALL_EMAIL` — any real email address |
| `springer` | `SPRINGER_API_KEY` |
| `elsevier` | `ELSEVIER_API_KEY` |

```bash
$env:UNPAYWALL_EMAIL="you@email.com"
$env:SPRINGER_API_KEY="..."
```

You can combine them: `--sources arxiv,crossref`.

> Instead of setting it with `$env:...` you can pass any setting on the command line, for example
> `--set UNPAYWALL_EMAIL=you@email.com`.

---

## Web interface

Everything above is also available in a browser:

```bash
diffai-eraf4xrd --ui
```

It opens at http://localhost:8501. Run it from the folder you want results written to — the UI
writes its output folder in the current directory just as a command-line run does, and its
**Browse files** and **JSON outputs** tabs read from there. Press Ctrl+C in the terminal to stop it.

Streamlit is installed with the package. CIF export and Materials Project lookups additionally
need `pip install ".[webapp]"`.

---

## All options

| Option | What it does |
|---|---|
| `--full-run` | Run everything: download → screen → figures → metadata → validate |
| `--steps step0,step1` | Run only certain steps (`download,step0,step1,step2,clean,step3`) |
| `--skip-download` | Use PDFs already in the output folder |
| `--skip-screening` | Skip screening — run later steps on **every** downloaded PDF |
| `-i "C:\my\pdfs"` | Use PDFs from a folder of your own (turns downloading off) |
| `-n 5` | How many papers to download per source |
| `--elements "Cu OR Copper"` | The material to search for |
| `--technique "XRD OR PXRD"` | The technique to search for |
| `--sources arxiv` | Where to download from |
| `--cc-only` | Keep only Creative-Commons-licensed papers |
| `--provider gpt` | Which AI provider (`gpt`, `gemini`, `claude`, `grok`, `together`) |
| `--model gpt-4o` | Which model |
| `--single-pass` | Ask the AI once per step instead of letting it check its own work (faster and cheaper, usually finds a little less) |
| `--set KEY=VALUE` | Set any advanced setting directly (repeatable) |
| `-o my_run` | Name the output folder |
| `--dry-run` | Preview without running — free |
| `--ui` | Open the web interface |
| `--help` | List every option |

### Using a different model for each step

You can give each step its own model — a cheap one to screen papers, a stronger one to read
figures:

```bash
diffai-eraf4xrd --full-run --set PHASE0_MODEL=gpt-4.1-nano --set PHASE1_MODEL=gpt-4o
```

**Step 1 reads pictures, so it needs a model that can see images.** A text-only model such as
Llama-3.3-70B will fail there. Use `gpt-4o`, a Gemini or Claude model, or another vision model
for step 1.

---

## Troubleshooting

| You see… | What it means | What to do |
|---|---|---|
| `ModuleNotFoundError: No module named 'diffai.eraf4xrd'` | You used `py -3.13`, or the environment is not active | Run `conda activate eraf4xrd`, then use `diffai-eraf4xrd ...` |
| `Missing API key(s) for the steps this run would execute` | A step that is switched on has no key | Set the key it names, or use `--steps download` to run without AI |
| `ERROR: ... ipykernel ... requires tornado` during install | Something unrelated in your environment is incomplete — not ERAF4XRD | Ignore it. The line after it says `Successfully installed`. `pip install tornado` silences it |
| `Input validation error` during step 1 | Your model cannot see images | `--set PHASE1_MODEL=gpt-4o` |
| `non-serverless model ... dedicated endpoint` | That open-source model is not free to call on your account | Pick a different model, or enable it on together.ai |
| It prints "Skipping…" and nothing runs | A later step could not find the earlier step's results | Point `-o` at the previous run's folder |
| It downloads papers when you did not want it to | No `-i` was given, so downloading is on | Use `-i "C:\my\pdfs"` |
| `[WinError 206] ... filename ... too long` while installing | Your install path exceeds the Windows 260-character limit | Install from a short path such as `C:\xrd`, or enable Windows long paths |
| Not sure what a command will do | — | Add `--dry-run` to preview it for free |

---

*Want every flag and combination? See [CLI_REFERENCE.md](CLI_REFERENCE.md).*
