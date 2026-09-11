|Icon| |title|_
===============

.. |title| replace:: diffai.eraf4xrd
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

.. |PyPI| image:: https://img.shields.io/pypi/v/diffai.eraf4xrd
        :target: https://pypi.org/project/diffai.eraf4xrd/

.. |PythonVersion| image:: https://img.shields.io/pypi/pyversions/diffai.eraf4xrd
        :target: https://pypi.org/project/diffai.eraf4xrd/

.. |Tracking| image:: https://img.shields.io/badge/issue_tracking-github-blue
        :target: https://github.com/diffractionai/diffai.xrdreader/issues

Multi-step agentic framework for extracting powder X-ray diffraction (XRD) data — figures and metadata — from scientific literature.

ERAF4XRD downloads open-access papers, screens them for XRD relevance, detects and classifies
figures, extracts crystallographic metadata, and cross-verifies the results. Each step can be
powered by a different LLM provider (OpenAI, Anthropic, Google, xAI, or open-source models via
Together AI).

For more information about the diffai.eraf4xrd library, please consult our `online documentation <https://diffractionai.github.io/diffai.xrdreader>`_.

Citation
--------

If you use diffai.eraf4xrd in a scientific publication, we would like you to cite this package as

        diffai.eraf4xrd Package, https://github.com/diffractionai/diffai.xrdreader

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
The following creates and activates a new environment named ``diffai.eraf4xrd_env`` ::

        conda create -n diffai.eraf4xrd_env diffai.eraf4xrd
        conda activate diffai.eraf4xrd_env

The output should print the latest version displayed on the badges above.

If the above does not work, you can use ``pip`` to download and install the latest release from
`Python Package Index <https://pypi.python.org>`_.
To install using ``pip`` into your ``diffai.eraf4xrd_env`` environment, type ::

        pip install diffai.eraf4xrd

If you prefer to install from sources, after installing the dependencies, obtain the source archive from
`GitHub <https://github.com/diffractionai/diffai.xrdreader/>`_. Once installed, ``cd`` into your ``diffai.eraf4xrd`` directory
and run the following ::

        pip install .

This package also provides a command-line tool, ``diffai-eraf4xrd``. To check it has been installed
correctly, type ::

        diffai-eraf4xrd --help

You can also verify the installed version. ::

        python -c "import diffai.eraf4xrd; print(diffai.eraf4xrd.__version__)"


To view the basic usage and available commands, type ::

        diffai-eraf4xrd -h

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

Feel free to fork the project and contribute. To install diffai.eraf4xrd
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

For more information on diffai.eraf4xrd please visit the project `web-page <https://diffractionai.github.io/>`_ or email the maintainers ``Afnan Mostafa(amostafa@ur.rochester.edu), Simon J. L. Billinge(sbillinge@ucsb.edu), Niaz Abdolrahim(niaz@rochester.edu), and William Ratcliff(wratclif@umd.edu)``.

Acknowledgements
----------------

``diffai.eraf4xrd`` is built and maintained with `scikit-package <https://scikit-package.github.io/scikit-package/>`_.
