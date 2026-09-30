"""Application version: the single source for the dashboard, the installers and updates.

Release builds overwrite this file from the Git tag (``scripts/set_version.py``,
run by ``.github/workflows/build-release.yml``), so tag ``vX.Y.Z`` always
produces an app, a DMG and a Windows installer that all report ``X.Y.Z``.
"""
__version__ = "1.0.5"
