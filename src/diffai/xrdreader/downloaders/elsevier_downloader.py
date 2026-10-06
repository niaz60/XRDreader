"""Elsevier (ScienceDirect) open-access PDF downloader.

Searches ScienceDirect for the configured elements and technique, pages
through the results, de-duplicates by DOI/title, and downloads open-access
PDFs into the run's Elsevier/ subfolder via the article-retrieval API.
Requires ELSEVIER_API_KEY, Creative-Commons filtering is optional, and any
stub PDF under 50 KB is discarded. Returns True if at least one PDF was saved.
"""

import os
import time

import requests

from diffai.xrdreader.config import (
    ELEMENTS,
    ELSEVIER_API_KEY,
    OUTPUT_PDF_DIR,
    REQUIRE_CC_LICENSE,
    SLEEP,
    TARGET_DOWNLOADS,
    TECHNIQUE,
)
from diffai.xrdreader.utils import (
    ensure_dir,
    get_source_subdir,
    log,
    log_download,
    make_pdf_safe_title,
)


# main function (returns 'true' if at least 1 pdf is downloaded/counted, 'false' if none/failed)
def download_elsevier_pdfs() -> bool:
    """
    Downloads Open Access PDFs from Elsevier's ScienceDirect API using manual pagination.
    Avoids duplication, skips paywalled content, and respects the TARGET_DOWNLOADS limit.
    """
    ensure_dir(OUTPUT_PDF_DIR)
    elsevier_dir = get_source_subdir(OUTPUT_PDF_DIR, "Elsevier")

    if not ELSEVIER_API_KEY:
        log("ERROR: ELSEVIER_API_KEY not set.")
        return False

    try:
        # Elsevier search query
        query = f"({ELEMENTS}) AND ({TECHNIQUE})"
        log(f"Searching Elsevier (ScienceDirect) with query: {query}")

        # ScienceDirect search API settings
        base_url = "https://api.elsevier.com/content/search/sciencedirect"
        headers = {"X-ELS-APIKey": ELSEVIER_API_KEY}
        all_results = []
        start = 0
        count = 25
        total_reached = False

        # fetches search results page by page
        while not total_reached and start < TARGET_DOWNLOADS * 5:
            params = {"query": query, "start": start, "count": count}

            # sends ScienceDirect search request
            r = requests.get(
                base_url, headers=headers, params=params, timeout=30
            )

            # 200 = success; 401 = invalid or missing API key; 403 = access denied; 429 = too many requests
            if r.status_code != 200:
                log(
                    f"[WARNING] Elsevier API request failed at start={start}: {r.status_code}"
                )
                break

            # extracts search entries
            entries = r.json().get("search-results", {}).get("entry", [])
            if not entries:
                break

            # stores results and moves to next page
            all_results.extend(entries)
            log(
                f"[DEBUG] Page fetched: start={start}, got={len(entries)} results."
            )
            start += count
            time.sleep(2)

            # if Elsevier returns fewer than 25 results, this likely means there are no more pages.
            if len(entries) < count:
                total_reached = True

        # removes duplicate records
        unique = {}
        for r in all_results:
            key = r.get("prism:doi") or r.get("dc:title")
            if key:
                unique[key] = r
        results = list(unique.values())
        log(
            f"Deduplicated to {len(results)} unique results before download phase."
        )

        # loop through unique results
        downloaded = 0
        for i, result in enumerate(results):
            if downloaded >= TARGET_DOWNLOADS:
                break

            # extracts DOI and title
            doi = result.get("prism:doi")
            title = result.get("dc:title", f"Elsevier_paper_{i+1}")
            if not doi:
                continue

            # CC license filtering
            if REQUIRE_CC_LICENSE:
                els_license = (result.get("prism:copyright") or "").lower()
                els_oa_flag = (result.get("openaccess") or "").lower()
                if (
                    "creative commons" not in els_license
                    and "cc" not in els_license
                    and els_oa_flag != "true"
                ):
                    log(f"Skipping Elsevier {doi}: no CC license")
                    continue

            # builds article URL
            pdf_url = f"https://api.elsevier.com/content/article/doi/{doi}"
            pdf_headers = {
                "Accept": "application/pdf",
                "X-ELS-APIKey": ELSEVIER_API_KEY,
            }

            # creates a safe filename
            safe_name = make_pdf_safe_title(
                title=title, fallback=f"Elsevier_paper_{i+1}", unique_id=doi
            )
            out_path = os.path.join(elsevier_dir, f"{safe_name}.pdf")

            if os.path.exists(out_path):
                log(f"Already exists: {safe_name}")
                continue

            try:
                # start downloading the PDF (sends the actual PDF request)
                r = requests.get(pdf_url, headers=pdf_headers, timeout=60)
                if (
                    r.status_code == 200
                    and "pdf" in r.headers.get("Content-Type", "").lower()
                ):
                    with open(out_path, "wb") as f:
                        f.write(r.content)
                    if os.path.getsize(out_path) > 50_000:
                        downloaded += 1
                        log(
                            f"[OK] Downloaded OA PDF [{downloaded}/{TARGET_DOWNLOADS}]: {safe_name}"
                        )
                        log_download(
                            pdf_filename=f"{safe_name}.pdf",
                            source="Elsevier",
                            url=pdf_url,
                            doi=doi,
                            title=title,
                        )
                    else:
                        os.remove(out_path)
                        log(
                            f"[WARNING] Deleted tiny stub PDF (<50 KB): {safe_name}"
                        )

                # skips failed download
                else:
                    log(f"[WARNING] Skipped (status={r.status_code}) {doi}")

            # handles error for one PDF
            except Exception as e:
                log(f"[ERROR] Error downloading {doi}: {e}")

            time.sleep(SLEEP)

        # final log and return value
        log(
            f"Elsevier download complete: {downloaded}/{TARGET_DOWNLOADS} real PDFs saved."
        )
        return downloaded > 0

    # handles major failure
    except Exception as e:
        log(f"Elsevier downloader failed: {e}")
        return False
