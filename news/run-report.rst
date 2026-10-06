**Added:**

* A run report. Every run now writes ``report.html`` into its own output folder,
  a single page showing which documents were kept or rejected and why, the figure
  crops Step I cut out and how it classified each one, the metadata Step II
  extracted, and every tool call each step's agent made with the evidence Step III
  used. Open it by double-clicking; it needs no server and no network. Build one
  for an earlier run with ``diffai-xrdreader --report <run folder>``, add
  ``--embed-report`` for a copy that can be sent on its own, and turn the
  automatic write off with ``--no-report`` or ``WRITE_RUN_REPORT=false``.

**Changed:**

* No news

**Deprecated:**

* No news

**Removed:**

* No news

**Fixed:**

* No news

**Security:**

* No news
