"""Springer Nature open-access PDF downloader.

Searches the Springer Open Access API for the configured elements and
technique, pages the results into one table, and optionally restricts to a
list of journals. For each candidate it tries the API's direct PDF link; when
there isn't one it scrapes the article page and falls back to several known
Springer/Nature PDF URL patterns. Saved PDFs go into the run's Springer/
subfolder. Requires SPRINGER_API_KEY, Creative-Commons filtering is optional.
Returns True if at least one PDF was saved.
"""

import contextlib
import io
import logging
import os
import re
import time

import pandas as pd
import requests
import springernature_api_client.openaccess as openaccess
from bs4 import BeautifulSoup
from springernature_api_client.utils import results_to_dataframe

from diffai.xrdreader.config import (
    ANY_JOURNAL,
    BATCH,
    ELEMENTS,
    OUTPUT_PDF_DIR,
    REQUIRE_CC_LICENSE,
    SLEEP,
    SPRINGER_API_KEY,
    SPRINGER_JOURNALS,
    TARGET_DOWNLOADS,
    TECHNIQUE,
)
from diffai.xrdreader.utils import (
    ensure_dir,
    get_source_subdir,
    log,
    log_download,
)

# springernature_api_client leaks the API key through its logging in two
# ways: it logs every request at INFO with the params dict (which contains
# api_key), and it logs failures at ERROR with the full request URL (which
# carries api_key in the query string). Its logging_config also calls
# logging.basicConfig at import, attaching a console handler and an
# api_client.log file handler to the ROOT logger.
#
# Raising the level alone is not enough -- it would either leave the ERROR
# path leaking or hide genuine errors -- so every record is passed through a
# filter that redacts the key instead. Our own log() does not use the logging
# module, so nothing of ours is affected.


class _RedactAPIKey(logging.Filter):
    """Replace the Springer key with a placeholder in any log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        key = SPRINGER_API_KEY
        if not key:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if key in message:
            # Collapse args into the message so the redaction cannot be
            # undone by later %-formatting.
            record.msg = message.replace(key, "<SPRINGER_API_KEY redacted>")
            record.args = ()
        return True


_redactor = _RedactAPIKey()
logging.getLogger("api_client").addFilter(_redactor)
for _handler in list(logging.getLogger().handlers):
    _handler.addFilter(_redactor)
    _target = getattr(_handler, "baseFilename", "")
    if isinstance(_handler, logging.FileHandler) and _target.endswith(
        "api_client.log"
    ):
        # Detach the stray log file the client drops in the working
        # directory, and remove it if nothing was written to it.
        logging.getLogger().removeHandler(_handler)
        try:
            _handler.close()
            if os.path.exists(_target) and os.path.getsize(_target) == 0:
                os.remove(_target)
        except Exception:
            pass


# Springer search query (converts the config keywords into Springer API query format)
def build_springer_query(elements: str, technique: str) -> str:
    element_terms = [
        f'keyword:"{e.strip()}"' for e in re.split(r"OR", elements)
    ]  # splits the material keywords wherever OR appears
    tech_terms = [
        f'keyword:"{t.strip().strip(chr(39)).strip(chr(34))}"'  # strips extra spaces, removes single quotes and double quotes from technique terms.
        for t in re.split(r"OR", technique)
    ]  # splits the technique keywords wherever OR appears
    return f"({' OR '.join(element_terms)}) AND ({' OR '.join(tech_terms)})"


# finds PDF link from article page (# opens an article webpage and tries to find a PDF link)
def find_pdf_link(url: str) -> str | None:
    try:
        # downloads the article webpage
        r = requests.get(url, timeout=20, headers=HEADERS)
        if r.status_code != 200:
            return None
        # parses the webpage HTML
        soup = BeautifulSoup(r.text, "html.parser")
        # finds the first link ending in .pdf or containing .pdf?
        link = soup.find("a", href=re.compile(r"\.pdf($|\?)"))
        if not link:
            return None
        # if the PDF link is relative, the code adds the Nature domain
        href = link["href"]
        return (
            href
            if href.startswith("http")
            else "https://www.nature.com" + href
        )
    except Exception:
        return None


# makes requests look like they come from a browser-like client
HEADERS = {"User-Agent": "Mozilla/5.0 (XRDreader; Springer OA Downloader)"}


# silent Springer API search
def silent_search(client, q, p, s):
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        resp = client.search(q=q, p=p, s=s, fetch_all=False)
        df_page = results_to_dataframe(resp)
    return df_page


# main function
def download_springer_pdfs() -> bool:
    ensure_dir(OUTPUT_PDF_DIR)
    springer_dir = get_source_subdir(OUTPUT_PDF_DIR, "Springer")

    # checks API key
    if not SPRINGER_API_KEY:
        log("ERROR: SPRINGER_API_KEY not set.")
        return False

    try:
        # builds query and creates API client
        q = build_springer_query(ELEMENTS, TECHNIQUE)
        client = openaccess.OpenAccessAPI(api_key=SPRINGER_API_KEY)
        log(f"Searching Springer with: {q}")

        # prepares pagination
        all_pages = []
        start = 1
        max_pages = 15

        # fetches pages one by one
        for page in range(max_pages):
            log(f"Fetching page {page+1}/{max_pages} (start={start})")
            df_page = silent_search(client, q=q, p=BATCH, s=start)
            if df_page.empty:
                break
            all_pages.append(df_page)
            start += BATCH
            time.sleep(1.5)

        # stop if none found
        if not all_pages:
            log("No Springer results found.")
            return False

        # combines pages into one table
        df = pd.concat(all_pages, ignore_index=True)

        # optional journal filtering
        if not ANY_JOURNAL and SPRINGER_JOURNALS:
            df = df[df["publicationName"].isin(SPRINGER_JOURNALS)]

        # logs number of candidates
        log(f"Found {len(df)} candidate papers. Target: {TARGET_DOWNLOADS}")

        downloaded = 0

        # loops through candidate papers
        for i, rec in enumerate(df.to_dict("records"), start=1):
            if downloaded >= TARGET_DOWNLOADS:
                break

            # extracts title
            title = rec.get("title", f"paper_{i}")

            # CC license filtering
            if REQUIRE_CC_LICENSE:
                rec_copyright = (rec.get("copyright") or "").lower()
                rec_license = (rec.get("license") or "").lower()
                combined = rec_copyright + " " + rec_license
                if (
                    "creative commons" not in combined
                    and "cc by" not in combined
                    and "cc-by" not in combined
                ):
                    log(
                        f"Skipping Springer paper: no CC license ({title[:60]})"
                    )
                    continue

            # extracts URLs
            urls = [
                u.get("value")
                for u in rec.get("url", [])
                if isinstance(u, dict)
            ]
            pdf_url = next(
                (
                    u
                    for u in urls
                    if isinstance(u, str) and u.lower().endswith(".pdf")
                ),
                None,
            )

            # fallback: finds PDF from webpage
            if not pdf_url and urls:
                pdf_url = find_pdf_link(urls[0])

            # creates a safe filename
            safe_title = "".join(
                c for c in title if c.isalnum() or c in " _-"
            )[:80].strip()
            if not safe_title:
                safe_title = f"paper_{i}"

            out = os.path.join(springer_dir, f"{safe_title}.pdf")

            # skips if exists
            if os.path.exists(out):
                downloaded += 1
                continue

            # extracts DOI from URLs
            doi_match = re.search(
                r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", " ".join(urls), re.I
            )
            doi = doi_match.group(0) if doi_match else None

            # builds candidate PDF URLs (P.S. these are URL candidates, not the Phase I figure candidates :p)
            candidates = []
            if pdf_url:
                candidates.append(pdf_url)
            if doi:
                candidates += [
                    f"https://link.springer.com/article/{doi}/pdf",
                    f"https://link.springer.com/article/{doi}/pdf?pdf=button",
                    f"https://link.springer.com/content/pdf/{doi}.pdf",
                    f"https://rd.springer.com/content/pdf/{doi}.pdf",
                ]

            # Tries each candidate PDF URL
            found = False
            for url in candidates:
                try:
                    r = requests.get(url, headers=HEADERS, timeout=30)
                    if (
                        r.status_code == 200
                        and "pdf" in r.headers.get("Content-Type", "").lower()
                    ):
                        with open(out, "wb") as f:
                            f.write(r.content)
                        downloaded += 1
                        log(
                            f"[OK] Downloaded [{downloaded}/{TARGET_DOWNLOADS}]: {safe_title}"
                        )
                        log_download(
                            pdf_filename=f"{safe_title}.pdf",
                            source="Springer",
                            url=url,
                            doi=doi,
                            title=title,
                        )
                        found = True
                        break
                except Exception:
                    pass

            # logs missing PDF
            if not found:
                log(f"No accessible PDF for {title}")

            time.sleep(SLEEP)

        # final log and return value
        log(f"Springer download complete: {downloaded}/{TARGET_DOWNLOADS}")
        return downloaded > 0

    # handles major failure
    except Exception as e:
        log(f"Springer downloader failed: {e}")
        return False
