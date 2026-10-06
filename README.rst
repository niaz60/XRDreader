|Icon| |title|_
===============

.. |title| replace:: diffai.xrdreader
.. _title: https://diffractionai.github.io/diffai.xrdreader

.. |Icon| image:: https://avatars.githubusercontent.com/diffractionai
        :target: https://diffractionai.github.io/diffai.xrdreader
        :height: 100px

|PyPI| |Forge| |PythonVersion| |PR|

|CI| |Codecov| |Black| |Tracking|

.. |Black| image:: https://img.shields.io/badge/code_style-black-black
        :target: https://github.com/psf/black

.. |CI| image:: https://github.com/diffractionai/diffai.xrdreader/actions/workflows/matrix-and-codecov-on-merge-to-main.yml/badge.svg
        :target: https://github.com/diffractionai/diffai.xrdreader/actions/workflows/matrix-and-codecov-on-merge-to-main.yml

.. |Codecov| image:: https://codecov.io/gh/diffractionai/diffai.xrdreader/branch/main/graph/badge.svg
        :target: https://codecov.io/gh/diffractionai/diffai.xrdreader

.. |Forge| image:: https://img.shields.io/conda/vn/conda-forge/diffai.xrdreader
        :target: https://anaconda.org/conda-forge/diffai.xrdreader

.. |PR| image:: https://img.shields.io/badge/PR-Welcome-29ab47ff
        :target: https://github.com/diffractionai/diffai.xrdreader/pulls

.. |PyPI| image:: https://img.shields.io/pypi/v/diffai.xrdreader
        :target: https://pypi.org/project/diffai.xrdreader/

.. |PythonVersion| image:: https://img.shields.io/pypi/pyversions/diffai.xrdreader
        :target: https://pypi.org/project/diffai.xrdreader/

.. |Tracking| image:: https://img.shields.io/badge/issue_tracking-github-blue
        :target: https://github.com/diffractionai/diffai.xrdreader/issues

.. important::

   **XRDreader was previously named ERAF4XRD.** The framework is described under that
   former name in `arXiv:2609.18583 <https://arxiv.org/abs/2609.18583>`_. The repository,
   the package (``diffai.xrdreader``) and the command (``diffai-xrdreader``) now use the
   name XRDreader; nothing else about the framework changed.

   **XRDreader is a component of DiffAI** and is being integrated under the ``diffai``
   namespace. This repository is the working copy while that integration is in progress,
   which is why the package is named ``diffai.xrdreader`` and why some links below still
   point at the DiffAI project.

   **To install and run XRDreader, do not follow the conda-forge or PyPI commands in the
   Installation section below** — the package is not published to those channels yet,
   so they will fail. Install from source instead, and use the XRDreader guides:

   * `HOW_TO_RUN.md <HOW_TO_RUN.md>`_ — the short reference: install, run, options,
     troubleshooting.
   * `TUTORIAL.md <TUTORIAL.md>`_ — step by step from a bare computer to your first
     results, assuming no command-line experience.
   * `CLI_REFERENCE.md <CLI_REFERENCE.md>`_ — every flag and combination.

   The rest of this page is the standard DiffAI package description.

Multi-step agentic framework for extracting powder X-ray diffraction (XRD) data — figures and metadata — from scientific literature.

XRDreader downloads open-access papers, screens them for XRD relevance, detects and classifies
figures, extracts crystallographic metadata, and cross-verifies the results. Each step can be
powered by a different LLM provider (OpenAI, Anthropic, Google, xAI, or open-source models via
Together AI).

For more information about the diffai.xrdreader library, please consult our `online documentation <https://diffractionai.github.io/diffai.xrdreader>`_.

Citation
--------

If you use diffai.xrdreader in a scientific publication, we would like you to cite this package as

        XRDreader (diffai.xrdreader) Package, https://github.com/niaz60/XRDreader

Installation
------------

.. note::

   The conda-forge and PyPI commands below apply once the package is published to those channels.
   Until then, install from source with ``pip install .`` (see "install from sources" below).

The preferred method is to use `Miniconda Python
<https://docs.conda.io/projects/miniconda/en/latest/miniconda-install.html>`_
and install from the "conda-forge" channel of Conda packages.

To add "conda-forge" to the conda channels, run the following in a terminal. ::

        conda config --add channels conda-forge

We want to install our packages in a suitable conda environment.
The following creates and activates a new environment named ``diffai.xrdreader_env`` ::

        conda create -n diffai.xrdreader_env diffai.xrdreader
        conda activate diffai.xrdreader_env

The output should print the latest version displayed on the badges above.

If the above does not work, you can use ``pip`` to download and install the latest release from
`Python Package Index <https://pypi.python.org>`_.
To install using ``pip`` into your ``diffai.xrdreader_env`` environment, type ::

        pip install diffai.xrdreader

If you prefer to install from sources, after installing the dependencies, obtain the source archive from
`GitHub <https://github.com/diffractionai/diffai.xrdreader/>`_. Once installed, ``cd`` into your ``diffai.xrdreader`` directory
and run the following ::

        pip install .

This package also provides a command-line tool, ``diffai-xrdreader``. To check it has been installed
correctly, type ::

        diffai-xrdreader --help

You can also verify the installed version. ::

        python -c "import diffai.xrdreader; print(diffai.xrdreader.__version__)"


To view the basic usage and available commands, type ::

        diffai-xrdreader -h

Getting Started
---------------

New to the command line? Start with `TUTORIAL.md <TUTORIAL.md>`_ — a step-by-step
walkthrough from a bare computer to your first results, assuming no programming
experience.

Otherwise see `HOW_TO_RUN.md <HOW_TO_RUN.md>`_ — a short guide covering install,
first run, and examples — and `CLI_REFERENCE.md <CLI_REFERENCE.md>`_ for every flag
and command combination.

You may also consult our `online documentation <https://diffractionai.github.io/diffai.xrdreader>`_ for tutorials and API references.

Support and Contribute
----------------------

If you see a bug or want to request a feature, please `report it as an issue <https://github.com/diffractionai/diffai.xrdreader/issues>`_ and/or `submit a fix as a PR <https://github.com/diffractionai/diffai.xrdreader/pulls>`_.

Feel free to fork the project and contribute. To install diffai.xrdreader
in a development mode, with its sources being directly used by Python
rather than copied to a package directory, use the following in the root
directory ::

        pip install -e .

To ensure code quality and to prevent accidental commits into the default branch, please set up the use of our pre-commit
hooks.

1. Install pre-commit in your working environment by running ``conda install pre-commit``.

2. Initialize pre-commit (one time only) ``pre-commit install``.

Thereafter your code will be linted by black and isort and checked against flake8 before you can commit.
If it fails by black or isort, just rerun and it should pass (black and isort will modify the files so should
pass after they are modified). If the flake8 test fails please see the error messages and fix them manually before
trying to commit again.

Improvements and fixes are always appreciated.

Before contributing, please read our `Code of Conduct <https://github.com/diffractionai/diffai.xrdreader/blob/main/CODE-OF-CONDUCT.rst>`_.

Contact
-------

.. TODO: Confirm the maintainer names, emails, and the GitHub org / documentation URLs (used in the
   badges at the top) before publishing. The list below came from the scikit-package template and
   may not match the final author list.

For more information on diffai.xrdreader please visit the project `web-page <https://diffractionai.github.io/>`_ or email the maintainers ``Afnan Mostafa(amostafa@ur.rochester.edu), Simon J. L. Billinge(sbillinge@ucsb.edu), Niaz Abdolrahim(niaz@rochester.edu), and William Ratcliff(wratclif@umd.edu)``.

Acknowledgements
----------------

``diffai.xrdreader`` is built and maintained with `scikit-package <https://scikit-package.github.io/scikit-package/>`_.
