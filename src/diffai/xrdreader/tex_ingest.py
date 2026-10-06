"""LaTeX-source ingestion for arXiv papers (optional alternative to PDF text).
Dependency-free. fetch_tex_source() downloads + inlines \\input/\\include;
clean_latex() flattens to readable text (keeps table values)."""

import gzip
import io
import os
import re
import tarfile
import urllib.request


def _members(data):
    """Unpack an arXiv e-print tarball into {filename: tex source}."""
    files = {}
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data))
        for m in tf.getmembers():
            if m.isfile() and m.name.lower().endswith(".tex"):
                files[m.name] = (
                    tf.extractfile(m).read().decode("utf-8", "replace")
                )
    except Exception:
        try:
            return {
                "main.tex": gzip.decompress(data).decode("utf-8", "replace")
            }
        except Exception:
            return {"main.tex": data.decode("utf-8", "replace")}
    return files


def _resolve(name, files, seen):
    """Inline input/include references into one .tex string."""
    text = files.get(name, "")

    def sub(m):
        ref = m.group(1).strip()
        cand = ref if ref.lower().endswith(".tex") else ref + ".tex"
        for k in files:
            if os.path.basename(k) == os.path.basename(cand) and k not in seen:
                seen.add(k)
                return _resolve(k, files, seen)
        return ""

    return re.sub(r"\\(?:input|include)\{([^}]*)\}", sub, text)


def fetch_tex_source(arxiv_id, timeout=90):
    """Download an arXiv paper's LaTeX source and inline its includes."""
    req = urllib.request.Request(
        f"https://arxiv.org/e-print/{arxiv_id}",
        headers={"User-Agent": "Mozilla/5.0 (XRDreader research)"},
    )
    data = urllib.request.urlopen(req, timeout=timeout).read()
    files = _members(data)
    main = next(
        (k for k in files if "\\documentclass" in files[k]),
        next(
            (k for k in files if "\\begin{document}" in files[k]),
            next(iter(files), None),
        ),
    )
    return _resolve(main, files, {main}) if main else ""


def clean_latex(raw):
    """Flatten LaTeX to readable text, keeping table values."""
    s = re.sub(r"(?<!\\)%.*", "", raw)
    m = re.search(r"\\begin\{document\}(.*)\\end\{document\}", s, re.DOTALL)
    if m:
        s = m.group(1)
    s = re.sub(
        r"\\begin\{(tabular|tabularx|array|longtable)\}(\[[^\]]*\])?(\{[^{}]*\})?",
        "\n",
        s,
    )
    s = re.sub(r"\\end\{(tabular|tabularx|array|longtable)\}", "\n", s)
    s = re.sub(
        r"\\(begin|end)\{(table|figure|center|threeparttable|adjustbox|table\*|figure\*)\}(\[[^\]]*\])?",
        "",
        s,
    )
    s = s.replace("\\\\", "\n")
    s = re.sub(r"\\(hline|toprule|midrule|bottomrule)|\\cline\{[^}]*\}", "", s)
    s = re.sub(r"\\multicolumn\{[^}]*\}\{[^}]*\}\{([^}]*)\}", r"\1", s)
    s = re.sub(r"\\multirow\{[^}]*\}\{[^}]*\}\{([^}]*)\}", r"\1", s)
    s = s.replace("&", " | ")
    s = re.sub(r"\\SI\{([^}]*)\}\{([^}]*)\}", r"\1 \2", s)
    s = re.sub(r"\\(num|ang|si)\{([^}]*)\}", r"\2", s)
    for c in [
        "textbf",
        "textit",
        "emph",
        "mathrm",
        "mathbf",
        "text",
        "mathit",
        "textrm",
        "textsc",
        "ce",
        "chem",
        "overline",
        "bar",
        "hat",
        "vec",
        "tilde",
        "underline",
        "mathbb",
        "mathcal",
    ]:
        s = re.sub(r"\\" + c + r"\*?\{([^{}]*)\}", r"\1", s)
    for k, v in {
        r"\AA": "Å",
        r"\times": "x",
        r"\pm": "±",
        r"\alpha": "α",
        r"\beta": "β",
        r"\gamma": "γ",
        r"\degree": "°",
        r"\circ": "°",
        r"\lambda": "λ",
        r"\theta": "θ",
    }.items():
        s = s.replace(k, v)
    s = re.sub(
        r"\\(cite|citep|citet|ref|eqref|label|footnote|url|href)\*?(\[[^\]]*\])?\{[^}]*\}",
        "",
        s,
    )
    s = s.replace("$", " ")
    s = re.sub(r"\\[a-zA-Z@]+\*?(\[[^\]]*\])?\{([^{}]*)\}", r"\2", s)
    s = re.sub(r"\\[a-zA-Z@]+\*?", " ", s)
    s = s.replace("{", " ").replace("}", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", s).strip()


if __name__ == "__main__":
    import sys

    aid = sys.argv[1]
    txt = clean_latex(fetch_tex_source(aid))
    print(f"{aid}: cleaned LaTeX = {len(txt)} chars")
    print(txt[:1500])
