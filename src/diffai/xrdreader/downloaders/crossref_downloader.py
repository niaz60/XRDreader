"""CrossRef + Unpaywall open-access PDF downloader.

Queries CrossRef for journal articles matching the configured elements and
technique, then asks Unpaywall whether each DOI has a free full-text PDF. It
checks every OA location, not just Unpaywall's "best" one, so a paper isn't
missed when the preferred link has no PDF. Saved PDFs go into the run's
CrossRef/ subfolder. Requires UNPAYWALL_EMAIL, Creative-Commons filtering is
optional, and requests use a browser-like User-Agent to reduce publisher
blocking. Returns True if at least one PDF was saved.
"""

import os
import time

import requests

from diffai.xrdreader.config import (
    ELEMENTS,
    OUTPUT_PDF_DIR,
    REQUIRE_CC_LICENSE,
    SLEEP,
    TARGET_DOWNLOADS,
    TECHNIQUE,
    UNPAYWALL_EMAIL,
)
from diffai.xrdreader.utils import (
    ensure_dir,
    get_source_subdir,
    log,
    log_download,
    make_pdf_safe_title,
)


# main function (returns 'true' if at least 1 pdf is downloaded/counted, 'false' if none/failed)
def download_crossref_oa_pdfs():
    """
    CrossRef + Unpaywall fallback for open-access PDFs.
    Fixed:
      - Enforces valid Unpaywall email
      - Falls back to oa_locations if best_oa_location has no PDF
      - Uses headers to avoid publisher blocking
    """
    # must-have for unpaywall
    if not UNPAYWALL_EMAIL:
        log("ERROR: UNPAYWALL_EMAIL is not set.")
        return False

    ensure_dir(OUTPUT_PDF_DIR)
    crossref_dir = get_source_subdir(OUTPUT_PDF_DIR, "CrossRef")

    query = f"({ELEMENTS}) AND ({TECHNIQUE})"
    log(f"Searching CrossRef OA papers with query: {query}")

    try:
        # sends a request to the CrossRef API
        r = requests.get(
            "https://api.crossref.org/works",
            params={
                "query": query,
                "rows": TARGET_DOWNLOADS
                * 3,  # if target is '20 PDFs', it searches up to 60 CrossRef records (increase as needed)
                "filter": "type:journal-article",
            },
            timeout=40,
        )

        # checks CrossRef response
        if r.status_code != 200:
            log(f"CrossRef query failed: HTTP {r.status_code}")
            return False

        # extracts article records
        items = r.json().get("message", {}).get("items", [])
        if not items:
            log("No results found in CrossRef response.")
            return False

        # extracts DOIs
        dois = [it.get("DOI") for it in items if it.get("DOI")]
        log(f"Found {len(dois)} candidate DOIs from CrossRef.")

        downloaded = 0
        headers = {
            "User-Agent": "Mozilla/5.0 (XRDReader; OA PDF Downloader)"  # pretends to be a browser-like request to reduce blocking by publishers.
        }

        # loop over DOIs (one DOI at a time)
        for doi in dois:
            if downloaded >= TARGET_DOWNLOADS:
                break

            try:
                # asks Unpaywall “is there an open-access PDF for this DOI?”
                up = requests.get(
                    f"https://api.unpaywall.org/v2/{doi}",
                    params={"email": UNPAYWALL_EMAIL},
                    timeout=25,
                )

                if up.status_code != 200:
                    log(f"Unpaywall failed ({up.status_code}) for {doi}")
                    continue

                info = up.json()

                # CC license filtering
                if REQUIRE_CC_LICENSE:
                    best_loc = info.get("best_oa_location") or {}
                    license_str = (best_loc.get("license") or "").lower()
                    if not license_str.startswith("cc"):
                        for loc in info.get("oa_locations", []):
                            loc_lic = (loc.get("license") or "").lower()
                            if loc_lic.startswith("cc"):
                                license_str = loc_lic
                                break
                    if not license_str.startswith("cc"):
                        log(
                            f"Skipping {doi}: no CC license (license={license_str or 'none'})"
                        )
                        continue

                # extracts OA PDF URL
                pdf_url = None

                # first attempt: best OA location
                best = info.get("best_oa_location") or {}
                pdf_url = best.get("url_for_pdf")

                # second attempt: fallback OA locations
                if not pdf_url:
                    for loc in info.get("oa_locations", []):
                        if loc.get("url_for_pdf"):
                            pdf_url = loc["url_for_pdf"]
                            break

                if not pdf_url:
                    log(f"No OA PDF for {doi}")
                    continue

                # creates a safe filename
                title = info.get("title") or doi
                safe_title = make_pdf_safe_title(
                    title=title,
                    fallback=f"crossref_paper_{downloaded+1}",
                    unique_id=doi,
                )
                file_path = os.path.join(crossref_dir, f"{safe_title}.pdf")

                if os.path.exists(file_path):
                    log(f"Already exists: {safe_title}")
                    downloaded += 1
                    continue

                # downloads the PDF
                r2 = requests.get(
                    pdf_url,
                    headers=headers,
                    timeout=60,
                )

                # verifies and saves PDF
                if (
                    r2.status_code == 200
                    and "pdf" in r2.headers.get("Content-Type", "").lower()
                ):
                    with open(file_path, "wb") as f:
                        f.write(r2.content)

                    downloaded += 1
                    log(
                        f"[OK] Downloaded OA PDF [{downloaded}/{TARGET_DOWNLOADS}]: {safe_title}"
                    )
                    log_download(
                        pdf_filename=f"{safe_title}.pdf",
                        source="CrossRef/Unpaywall",
                        url=pdf_url,
                        doi=doi,
                        title=title,
                    )
                else:
                    log(
                        f"[WARNING] PDF fetch failed ({r2.status_code}) for {doi}"
                    )

                # waits between DOI attempts
                time.sleep(SLEEP)

            # handles error for one DOI
            except Exception as e:
                log(f"[ERROR] Error processing {doi}: {e}")
                time.sleep(SLEEP)

        # final log and return value
        log(
            f"CrossRef+Unpaywall complete: {downloaded}/{TARGET_DOWNLOADS} PDFs."
        )
        return downloaded > 0

    # handles major failure
    except Exception as e:
        log(f"CrossRef+Unpaywall failed: {e}")
        return False
