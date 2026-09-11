# How to run ERAF4XRD

ERAF4XRD reads scientific papers and pulls out their X-ray diffraction data. It finds the
papers, picks out the XRD figures, extracts the crystallographic metadata, and checks its
own answers.

This page starts from nothing and assumes you have not installed anything yet.

> **Never used a command line before?** Start with **[TUTORIAL.md](TUTORIAL.md)** instead.
> It walks through the same thing more slowly, shows what you should see after every
> command, and assumes no programming experience at all. Come back here once it works.

- [Before you start](#before-you-start)
- [Install](#install)
- [Preview anything for free](#preview-anything-for-free)
- [Common things to do](#common-things-to-do)
- [Where the results go](#where-the-results-go)
- [Download sources](#download-sources)
- [Web interface](#web-interface)
- [All options](#all-options)
- [Troubleshooting](#troubleshooting)

---

## Before you start

Three things. Each is free to install. Skip any you already have.

### 1. conda

Creates a private space for ERAF4XRD and the software it needs, so nothing clashes with the
rest of your computer.

Get it: <https://docs.conda.io/en/latest/miniconda.html> (Miniconda is the small version and is
all you need.)

To check it is there, open PowerShell and type:

```bash
conda --version
```

You should see something like `conda 25.1.1`.

### 2. git

Downloads the ERAF4XRD code. **Optional** — Step 2 below has a way to do it without git.

Get it: <https://git-scm.com/downloads>

```bash
git --version
```

### 3. An AI key

ERAF4XRD uses an AI model to read the papers, so you need an account with one provider. You
only need **one** of these:

| Provider | Where to get a key |
|---|---|
| OpenAI — the default | <https://platform.openai.com/api-keys> |
| Google Gemini | <https://aistudio.google.com/app/apikey> |
| Anthropic Claude | <https://console.anthropic.com/settings/keys> |

These are paid services, but a first test run costs well under a dollar. Your key is a long
string that starts with something like `sk-`. Keep it private.

---

## Install

Ten steps, done once. Type or paste one line at a time.

**Step 1 — open PowerShell.**

Press the Windows key, type `powershell`, press Enter. A blue window opens. Every command below
goes in this window.

**Step 2 — download the code.**

```bash
cd C:\
git clone https://github.com/diffractionai/diffai.xrdreader.git
```

*No git?* Open <https://github.com/diffractionai/diffai.xrdreader> in your browser, click the
green **Code** button, choose **Download ZIP**, and unzip it into `C:\`. Then carry on.

**Step 3 — go into the folder you just downloaded.**

```bash
cd C:\diffai.xrdreader
```

If you used the ZIP, the folder is probably called `diffai.xrdreader-main`, so use that name
instead.

**Step 4 — create the environment.**

```bash
conda create -n eraf4xrd python=3.13 -y
```

This takes a minute or two. You only ever do it once.

**Step 5 — switch on the environment.**

```bash
conda activate eraf4xrd
```

Your prompt now starts with `(eraf4xrd)`. That is how you know it worked.

**Step 6 — install ERAF4XRD.**

```bash
pip install .
```

The `.` means *the folder I am in right now* — which is why Steps 2 and 3 mattered. This prints
a lot of text and takes a few minutes.

**Step 7 — check it worked.**

```bash
diffai-eraf4xrd --help
```

If a list of options prints, the install succeeded.

**Step 8 — give it your AI key.**

Paste your own key in place of the `sk-...`:

```bash
$env:OPENAI_API_KEY="sk-..."
```

**Step 9 — make a folder to keep your results in, and go there.**

```bash
mkdir C:\my_xrd_work
cd C:\my_xrd_work
```

Results are saved wherever you are standing, so it is worth having a folder for them. From here
on you never need to go back to the ERAF4XRD folder.

**Step 10 — run it.**

```bash
diffai-eraf4xrd --full-run --sources arxiv -n 1
```

This downloads one arXiv paper about copper and XRD, screens it, finds the XRD figures,
extracts the metadata and validates it. Expect **2 to 3 minutes** and **$0.25 to $0.35**,
depending on the paper.

When it finishes, look in `C:\my_xrd_work\eraf4xrd_output\` for a folder named with today's
date and time.

> **Opening a new PowerShell window later?** You must redo two of these steps, because they do
> not carry over: `conda activate eraf4xrd` (Step 5) and `$env:OPENAI_API_KEY="..."` (Step 8).
> Steps 1-4, 6 and 7 are done for good.
>
> Use `diffai-eraf4xrd ...` (or `python -m diffai.eraf4xrd.app ...`). Do **not** use the Windows
> launcher `py -3.13 ...` — it ignores the environment you switched on and reports
> `ModuleNotFoundError: No module named 'diffai.eraf4xrd'` even after a successful install.

---

## Preview anything for free

Add `--dry-run` to any command on this page. It prints what *would* happen — which steps run,
which model, where results go — then stops. No downloads, no AI calls, no cost.

```bash
diffai-eraf4xrd --full-run --sources arxiv -n 1 --dry-run
```

Use it whenever you are unsure what a command will do.

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

Every run creates its own dated folder inside whatever directory you were standing in, so runs
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

arXiv is fully open and needs nothing extra, which makes it the best place to start. The other
sources each need their own credential:

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

You can combine sources: `--sources arxiv,crossref`.

> Instead of setting it with `$env:...` you can pass any setting on the command line, for
> example `--set UNPAYWALL_EMAIL=you@email.com`.

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
| `conda : The term 'conda' is not recognized` | conda is not installed, or PowerShell cannot see it | Install Miniconda (see [Before you start](#before-you-start)), then close and reopen PowerShell |
| `git : The term 'git' is not recognized` | git is not installed | Install it, or use the Download ZIP route in Step 2 |
| `ModuleNotFoundError: No module named 'diffai.eraf4xrd'` | You used `py -3.13`, or you forgot `conda activate eraf4xrd` | Run `conda activate eraf4xrd`, then use `diffai-eraf4xrd ...` |
| `Missing API key(s) for the steps this run would execute` | A step that is switched on has no key | Redo Step 8, or use `--steps download` to run with no AI at all |
| `ERROR: ... ipykernel ... requires tornado` during install | Something unrelated in your setup is incomplete — not ERAF4XRD | Ignore it. The line after it says `Successfully installed`. `pip install tornado` silences it |
| `Input validation error` during step 1 | Your model cannot see images | `--set PHASE1_MODEL=gpt-4o` |
| `non-serverless model ... dedicated endpoint` | That open-source model is not free to call on your account | Pick a different model, or enable it on together.ai |
| It prints "Skipping…" and nothing runs | A later step could not find the earlier step's results | Point `-o` at the previous run's folder |
| It downloads papers when you did not want it to | No `-i` was given, so downloading is on | Use `-i "C:\my\pdfs"` |
| `[WinError 206] ... filename ... too long` while installing | Your folder path is over the Windows 260-character limit | Install from a short path such as `C:\xrd`, or turn on Windows long paths |
| Not sure what a command will do | — | Add `--dry-run` to preview it for free |

---

*Want every flag and combination? See [CLI_REFERENCE.md](CLI_REFERENCE.md).*
