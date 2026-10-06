"""arXiv open-access PDF downloader.

Searches arXiv for the configured keywords and saves the newest matching
papers into the run's ArXiv/ subfolder. The official `arxiv` client handles
the metadata search; each PDF is then fetched over plain HTTP. Already-
downloaded papers are skipped (both the current and an older filename style),
Creative-Commons filtering is optional, and any file under 50 KB is treated as
a broken download and removed. Returns True if at least one PDF was saved.
"""

import os
import re
import time

import requests

from diffai.xrdreader.config import (
    OUTPUT_PDF_DIR,
    REQUIRE_CC_LICENSE,
    SEARCH_KEYWORDS,
    SLEEP,
    TARGET_DOWNLOADS,
)
from diffai.xrdreader.utils import (
    ensure_dir,
    get_source_subdir,
    log,
    log_download,
    make_pdf_safe_title,
)

# main function (returns 'true' if at least 1 pdf is downloaded/counted, 'false' if none/failed)

# arXiv's Atom API -- which the `arxiv` package wraps -- carries no licence
# field at all (its entries expose only title, authors, summary, categories,
# links and friends). Reading result.license therefore always produced an
# empty string, so REQUIRE_CC_LICENSE silently skipped every arXiv paper.
# OAI-PMH is the interface that does publish it.
_OAI = "https://export.arxiv.org/oai2"
_LICENSE_RE = re.compile(r"<license>(.*?)</license>", re.S)


def fetch_arxiv_license(arxiv_id: str, timeout: int = 20):
    """Return the licence URL arXiv records for a paper, or None.

    None means 'could not determine', which is deliberately distinct from
    'not Creative Commons' -- the caller must not treat a lookup failure as a
    licensing decision.
    """
    bare = (arxiv_id or "").split("/")[-1].strip()
    bare = re.sub(r"v\d+$", "", bare)  # OAI wants the version-less id
    if not bare:
        return None
    try:
        r = requests.get(
            _OAI,
            params={
                "verb": "GetRecord",
                "identifier": f"oai:arXiv.org:{bare}",
                "metadataPrefix": "arXiv",
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        m = _LICENSE_RE.search(r.text)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def download_arxiv_pdfs() -> bool:
    """
    Downloads PDFs directly from arXiv using the official `arxiv` library.
    Includes detailed debugging to verify PDF URLs and server responses.
    """
    import arxiv

    ensure_dir(OUTPUT_PDF_DIR)
    arxiv_dir = get_source_subdir(OUTPUT_PDF_DIR, "ArXiv")
    log(f"Searching arXiv for: {SEARCH_KEYWORDS}")

    try:
        search = arxiv.Search(
            query=SEARCH_KEYWORDS,
            max_results=TARGET_DOWNLOADS * 10,  # gets 10x more results
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,  # newest papers first
        )

        client_arxiv = arxiv.Client(
            page_size=25, delay_seconds=5
        )  # creates arXiv api client; gets 25 results/page; wait 5s
        downloaded = 0

        # loop through arXiv results (papers) one by one
        # one by one to avoid sending too many request to arXiv API servers
        for i, result in enumerate(client_arxiv.results(search), start=1):
            if downloaded >= TARGET_DOWNLOADS:
                break

            # creates a safe filename
            title = (
                result.title or f"arxiv_paper_{downloaded+1}"
            )  # uses paper title; if missing, uses fallback name
            arxiv_id = getattr(result, "entry_id", "").split("/")[
                -1
            ]  # extracts the arXiv ID from paper URL (tries to)

            # CC licence filtering. Note that arXiv's default licence is
            # nonexclusive-distrib, not Creative Commons, so most papers are
            # expected to fail this filter -- authors have to opt in to CC.
            if REQUIRE_CC_LICENSE:
                arxiv_license = fetch_arxiv_license(arxiv_id)
                if arxiv_license is None:
                    log(
                        f"Skipping arXiv {arxiv_id}: licence could not be "
                        "determined (OAI-PMH lookup failed)"
                    )
                    continue
                low = arxiv_license.lower()
                if "creativecommons" not in low and "/cc/" not in low:
                    log(
                        f"Skipping arXiv {arxiv_id}: not CC "
                        f"(licence={arxiv_license})"
                    )
                    continue

            safe_title = make_pdf_safe_title(
                title=title,
                fallback=f"arxiv_paper_{downloaded+1}",
                unique_id=arxiv_id,
            )
            file_path = os.path.join(arxiv_dir, f"{safe_title}.pdf")

            # checks for files saved using an older naming format to avoid downloading the same paper again
            # if it already exists under the old filename.
            old_style_title = make_pdf_safe_title(
                title=title,
                fallback=f"arxiv_paper_{downloaded+1}",
                unique_id="",
            )
            old_file_path = os.path.join(arxiv_dir, f"{old_style_title}.pdf")

            if os.path.exists(file_path):
                log(f"Already exists: {safe_title}")
                downloaded += 1
                continue

            if os.path.exists(old_file_path):
                log(f"Already exists under old naming: {old_style_title}")
                downloaded += 1
                continue

            # finds PDF link
            # First it tries "result.pdf_url"; if that is missing, it creates the PDF URL from the abstract URL.
            # Ex: https://arxiv.org/abs/2401.12345 --> https://arxiv.org/pdf/2401.12345.pdf
            pdf_url = getattr(result, "pdf_url", None)
            if (
                not pdf_url
                and hasattr(result, "entry_id")
                and "/abs/" in result.entry_id
            ):
                pdf_url = result.entry_id.replace("/abs/", "/pdf/") + ".pdf"

            if not pdf_url:
                log(f"No PDF URL for: {safe_title}")
                continue

            try:
                # This actually fetches the PDF file from arXiv
                r = requests.get(pdf_url, timeout=30, allow_redirects=True)
                log(f"[DEBUG] {i}. Title: {safe_title}")
                log(f"[DEBUG] URL: {pdf_url}")
                log(
                    f"[DEBUG] Status: {r.status_code}, "
                    f"Type: {r.headers.get('Content-Type')}, "
                    f"Size: {len(r.content)} bytes"
                )

                content_type = r.headers.get("Content-Type", "").lower()

                # checks whether the response is valid: r.status_code == 200 (GOOD)
                if r.status_code == 200 and "pdf" in content_type:
                    with open(file_path, "wb") as f:
                        f.write(r.content)

                    # sanity check: checks that the file is larger than 50 KB (avoids saving tiny broken files or error pages)
                    if os.path.getsize(file_path) > 50_000:
                        downloaded += 1
                        log(
                            f"[OK] Downloaded arXiv PDF [{downloaded}/{TARGET_DOWNLOADS}]: {safe_title}"
                        )
                        log_download(
                            pdf_filename=f"{safe_title}.pdf",
                            source="arXiv",
                            url=pdf_url,
                            doi=arxiv_id,
                            title=title,
                        )
                    # remove incomplete files or warn invalid response
                    else:
                        try:
                            os.remove(
                                file_path
                            )  # if the file is too small, it removes it.
                        except Exception:
                            pass  # if the HTTP response was not successful or was not a PDF, it logs a warning.
                        log(
                            f"[WARNING] File too small, likely incomplete: {safe_title}"
                        )
                else:
                    log(
                        f"[WARNING] Invalid response ({r.status_code}) or not a PDF for: {safe_title}"
                    )

            # handles download errors and waits
            except Exception as e:
                log(f"[ERROR] Error fetching {pdf_url}: {e}")

            # timer between fetch
            time.sleep(SLEEP)

        # final log and return value
        log(
            f"arXiv download complete: {downloaded}/{TARGET_DOWNLOADS} PDF(s)."
        )
        return downloaded > 0

    # handles major failure
    except Exception as e:
        log(f"arXiv downloader failed: {e}")
        return False
