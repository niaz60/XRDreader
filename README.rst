XRDreader
=========

|PythonVersion| |License| |Black| |PR|

.. |PythonVersion| image:: https://img.shields.io/badge/python-3.12%2B-blue
        :target: https://www.python.org/downloads/

.. |License| image:: https://img.shields.io/badge/license-BSD--3--Clause-blue
        :target: LICENSE.rst

.. |Black| image:: https://img.shields.io/badge/code_style-black-black
        :target: https://github.com/psf/black

.. |PR| image:: https://img.shields.io/badge/PR-Welcome-29ab47ff
        :target: https://github.com/niaz60/XRDreader/pulls

**Multi-step agentic framework for extracting powder X-ray diffraction (XRD) data — figures and
metadata — from the published literature.** It is distributed as the Python package
``diffai.xrdreader`` and run with the command ``diffai-xrdreader``.

XRDreader was previously named **ERAF4XRD**, and is described under that former name in
`arXiv:2609.18583 <https://arxiv.org/abs/2609.18583>`_. It is part of the
`DiffAI <https://github.com/diffractionai>`_ organisation and will move there once it is
published to PyPI, which is why the package is named ``diffai.xrdreader``. Until then, install
it from source as shown below.

What it does
------------

Given keywords such as ``Cu OR Copper`` and ``XRD``, XRDreader runs four steps and leaves a validated JSON
record for every paper it keeps:

**Step 0 — download and screening** (``download``, ``step0``)
    Collects open-access PDFs from arXiv, Springer, Elsevier and CrossRef/Unpaywall, then a
    model judges whether each paper genuinely contains XRD work and rejects the rest.

**Step I — figure detection** (``step1``)
    A vision model finds the figures and classifies which of them are XRD patterns.

**Step II — metadata extraction** (``step2``, ``clean``)
    Extracts metadata—crystal structure, space group, lattice parameters, phases and wavelength, then
    normalises and deduplicates the records. The cleaning function does not use any model.

**Step III — validation** (``step3``)
    A second model cross-checks every field against the information in the paper.

Run any subset with ``--steps``, for example ``--steps step2,clean,step3``.

Each step can use a different LLM provider: OpenAI, Anthropic, Google, xAI, or open-source
models through Together AI.

Installation
------------

You need `conda <https://docs.conda.io/en/latest/miniconda.html>`_ and
`git <https://git-scm.com/downloads>`_. On Windows the simplest shell is the **Anaconda
Powershell Prompt**, the only one that knows ``conda`` without setup. Any other terminal works
too, including the one in VS Code, once you have run ``conda init powershell`` in it once. ::

        git clone https://github.com/niaz60/XRDreader.git
        cd XRDreader
        conda create -n xrdreader python=3.13 -y
        conda activate xrdreader
        pip install .

Check that it worked ::

        diffai-xrdreader --help

The web interface needs two extra packages, for CIF export and Materials Project lookups ::

        pip install ".[webapp]"

Quick start
-----------

Run XRDreader from any folder you like — results are written into the folder you run from, so use
a folder of your own rather than the source tree. You need an API key for one LLM provider,
OpenAI by default ::

        $env:OPENAI_API_KEY="sk-..."      # Windows PowerShell
        export OPENAI_API_KEY="sk-..."    # macOS and Linux

        diffai-xrdreader --full-run --sources arxiv -n 1

That downloads one open-access paper, screens it, then extracts and validates its XRD metadata.
It takes two to three minutes and costs roughly $0.30 in API usage. Every run writes its own
timestamped folder, so runs never overwrite each other ::

        xrdreader_output/2026-10-06_091740/
            documents/   the PDFs it downloaded
            results/     extracted JSON and cropped figures
            logs/        the run log

The file ending ``__phase3_validated_FINAL.json`` holds the validated result for each paper.

Add ``--dry-run`` to any command to print the configuration it resolved and exit, without
downloading anything or calling a model. Launch the web interface with ``diffai-xrdreader --ui``.

Documentation
-------------

* `HOW_TO_RUN.md <HOW_TO_RUN.md>`_ — the short reference: install, run, options, troubleshooting.
* `TUTORIAL.md <TUTORIAL.md>`_ — step by step from scratch, literally,
  assuming no command-line experience.
* `CLI_REFERENCE.md <CLI_REFERENCE.md>`_ — every flag and combination.

Citation
--------

If you use XRDreader in a scientific publication, please cite the preprint (for now), the benchmark data
and the software:

* Preprint — `arXiv:2609.18583 <https://arxiv.org/abs/2609.18583>`_, where the framework appears
  under its former name, ERAF4XRD.
* Benchmark data — `doi:10.5281/zenodo.22683615 <https://doi.org/10.5281/zenodo.22683615>`_.
* Software — XRDreader (``diffai.xrdreader``), https://github.com/niaz60/XRDreader

Support and contribute
----------------------

Please `report bugs and request features as issues <https://github.com/niaz60/XRDreader/issues>`_,
or `open a pull request <https://github.com/niaz60/XRDreader/pulls>`_. Improvements and fixes are
always appreciated.

For development, install in editable mode so your edits take effect without reinstalling, and
enable the pre-commit hooks ::

        pip install -e .
        conda install pre-commit
        pre-commit install

Commits are then linted with black and isort and checked against flake8. If black or isort fails,
simply run the commit again — they rewrite the files, so the second attempt passes. flake8
failures have to be fixed by hand.

Before contributing, please read the `Code of Conduct
<https://github.com/niaz60/XRDreader/blob/main/CODE-OF-CONDUCT.rst>`_.

Contact
-------

Afnan Mostafa (amostafa@ur.rochester.edu or afnanmostafa102@gmail.com).

License
-------

BSD 3-Clause — see `LICENSE.rst <LICENSE.rst>`_.

Acknowledgements
----------------

``diffai.xrdreader`` is built and maintained with
`scikit-package <https://scikit-package.github.io/scikit-package/>`_.
