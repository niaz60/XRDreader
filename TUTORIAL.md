# XRDreader tutorial — your first run

This gets you from a brand-new computer to real results. No programming knowledge needed.

**You will:** install two free programs, install XRDreader, and run it on a real scientific
paper. It will find the X-ray diffraction figures in that paper and pull out the data.

**You need:** a Windows computer, and an OpenAI account (your first run costs about 30 cents).

**Time:** about 25 minutes, and most of that is waiting for things to download.

**How to use this page:** work through the parts in order. Each grey box is one command —
click it, copy it, paste it into your window, press Enter. After each one, this page tells you
what you should see. If you see that, carry on.

> To paste into the black window: **right-click**. Ctrl+V does not always work there.

---

## Part 1 — Install two free programs

You only ever do this once.

### 1a. Miniconda

Go to <https://docs.conda.io/en/latest/miniconda.html>, download the Windows installer, open it,
and click Next through every screen. Accept the defaults.

### 1b. Git

Go to <https://git-scm.com/downloads>, download the Windows version, open it, and click Next
through every screen. Accept the defaults.

**When both are finished, close every black or blue command window you have open.** New programs
only appear in windows opened *after* they are installed.

---

## Part 2 — Open the right window

This part matters. There are two similar-looking windows, and only one of them works
straight out of the box.

Click **Start**, type `Anaconda`, and click **Anaconda Powershell Prompt**.

> ⚠️ Do **not** open plain "PowerShell" or "Command Prompt". Those will tell you
> `conda is not recognized` and nothing will work. It must be the **Anaconda** one.

A black window opens. The line at the bottom looks something like:

```
(base) PS C:\Users\yourname>
```

That **`(base)`** at the start is how you know you have the right window.

> Once you are comfortable, you can use any terminal you like, including the one inside VS
> Code. Run `conda init powershell` once in this Anaconda window and every terminal will
> understand `conda` from then on. Until you do that, stay in the Anaconda window.

Now check both programs installed properly. Type this and press Enter:

```
conda --version
```

You should see a version number, like `conda 24.1.2`. Any number is fine.

Then:

```
git --version
```

You should see something like `git version 2.54.0.windows.1`. Any number is fine.

If either one says **"is not recognized"**, that program did not install. Go back to Part 1,
then close the window and open a new Anaconda Powershell Prompt.

---

## Part 3 — Install XRDreader

Also only once. Six commands, one at a time.

**3a.** Go to the top of your C: drive.

```
cd C:\
```

**3b.** Download XRDreader.

```
git clone https://github.com/niaz60/XRDreader.git
```

You should see `Cloning into 'XRDreader'...` and then a few lines about receiving
objects. It takes under a minute.

**3c.** Go into the folder that just appeared.

```
cd C:\XRDreader
```

**3d.** Make a private space for XRDreader to live in, so it cannot disturb anything else on
your computer.

```
conda create -n xrdreader python=3.13 -y
```

Lots of text scrolls past. It ends with the word `done`. This takes a minute or two.

**3e.** Switch into that space.

```
conda activate xrdreader
```

Look at the start of your prompt. It changed from `(base)` to **`(xrdreader)`**. That is how you
know it worked.

**3f.** Install XRDreader itself.

```
pip install .
```

A great deal of text scrolls past for a few minutes. You are looking for a line near the end
that says:

```
Successfully installed ... diffai.xrdreader-0.0.1 ...
```

> You will probably also see a red block saying `ERROR: pip's dependency resolver...` and
> something about `ipykernel` and `tornado`. **Ignore it.** It is complaining about an
> unrelated program, not about XRDreader. As long as you see `Successfully installed`, you are
> fine.

**3g.** Check it worked.

```
diffai-xrdreader --help
```

You should see a list of options starting with `usage: diffai-xrdreader`. If you do, XRDreader is
installed.

---

## Part 4 — Get your AI key

XRDreader uses an AI model to read the papers, so it needs your own key. Once only.

1. Go to <https://platform.openai.com/api-keys> and sign in (or create an account).
2. You will need to add a payment method and put a few dollars of credit on the account.
3. Click **Create new secret key**, give it any name, and click Create.
4. Copy the key. It is a long string starting with `sk-`.

> ⚠️ **Copy it now.** OpenAI shows the key once and never again. If you lose it, just make
> another one.

Keep it private — anyone who has it can spend your credit.

---

## Part 5 — Run it

**5a.** Make a folder to keep your results in, and go there.

```
mkdir C:\my_xrd_work
```

```
cd C:\my_xrd_work
```

**5b.** Give XRDreader your key. Replace `sk-paste-your-key-here` with the key you copied,
keeping the quotes.

```
$env:OPENAI_API_KEY="sk-paste-your-key-here"
```

Nothing appears to happen. That is correct.

**5c.** Run it.

```
diffai-xrdreader --full-run --sources arxiv -n 1
```

Now it works for about two to three minutes. You will see lines scroll past like:

```
Starting ArXiv download phase...
[OK] Downloaded arXiv PDF [1/1]: ...
Starting Phase 0...
Starting Phase 1...
```

At the end you get a table of what it cost, and the last line says:

```
Pipeline completed.
```

**That is it. You have run XRDreader.**

---

## Part 6 — Look at what you got

Open your results folder in Windows:

```
explorer C:\my_xrd_work
```

Inside `xrdreader_output` there is a folder named with today's date and time. Open it, and you
will find three folders:

| Folder | What is in it |
|---|---|
| `documents` | the scientific paper it downloaded, as a PDF |
| `results` | everything it extracted |
| `logs` | a record of what it did, useful if something looked wrong |

Open `results`. The file you want is the one ending in:

```
__phase3_validated_FINAL.json
```

That is your answer — the XRD data XRDreader found in the paper, after it checked its own work.
You can open it in Notepad, or in your browser, or in Excel.

You will also see `.png` image files in the folders ending `_xrd` — those are the actual
figures it identified as X-ray diffraction plots, cut out of the paper.

---

## Coming back tomorrow

You never repeat Parts 1, 3 or 4. But two things are forgotten when you close the window, so
each time you start a new Anaconda Powershell Prompt, run these two first:

```
conda activate xrdreader
```

```
$env:OPENAI_API_KEY="sk-paste-your-key-here"
```

Then go to your folder and run as before:

```
cd C:\my_xrd_work
```

```
diffai-xrdreader --full-run --sources arxiv -n 1
```

---

## Changing what it searches for

The run above looked for papers about copper. To look for something else, add `--elements`:

```
diffai-xrdreader --full-run --sources arxiv -n 1 --elements "Mo OR Molybdenum"
```

To get more papers, change the number after `-n`:

```
diffai-xrdreader --full-run --sources arxiv -n 5
```

Each paper costs roughly 30 cents, so `-n 5` costs around $1.50.

To see what a command would do **without spending anything**, add `--dry-run` to the end:

```
diffai-xrdreader --full-run --sources arxiv -n 5 --dry-run
```

---

## If something goes wrong

| You see | What to do |
|---|---|
| `conda is not recognized` | You are in the wrong window. Close it and open **Anaconda Powershell Prompt** (Part 2). To use other terminals, see the note at the end of Part 2 |
| `git is not recognized` | Git did not install. Redo Part 1b, then open a new window |
| `ModuleNotFoundError: No module named 'diffai.xrdreader'` | You forgot `conda activate xrdreader`. Run it and try again |
| `Missing API key(s) for the steps this run would execute` | You forgot step 5b, or you opened a new window since. Redo 5b |
| A red `ERROR` about `ipykernel` / `tornado` during Part 3f | Ignore it. Check for `Successfully installed` just below |
| `You exceeded your current quota` | Your OpenAI account is out of credit. Add some at platform.openai.com |
| Your prompt says `(base)` not `(xrdreader)` | Run `conda activate xrdreader` |

---

*Want the shorter reference version, or every available option? See
[HOW_TO_RUN.md](HOW_TO_RUN.md) and [CLI_REFERENCE.md](CLI_REFERENCE.md).*
