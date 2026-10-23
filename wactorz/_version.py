# SPDX-License-Identifier: Apache-2.0
"""The installed release of Wactorz.

The one place the version is written: the build reads it from here
(``[tool.hatch.version]`` in ``pyproject.toml``), and ``scripts/sync_versions.py``
changes it and carries it to the frontend's ``package.json``, the add-on
manifests and the docs.
"""

__version__ = "0.7.1"
