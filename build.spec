# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SV Face ID Attendance.

Usage:
    macOS:   pyinstaller build.spec
    Windows: pyinstaller build.spec

Produces a single-folder app bundle.  For a .app on macOS, set bundle=True below.
For a single .exe on Windows, set onefile=True below.
"""
import os
import sys
import platform

# ── Options ──────────────────────────────────────────────────────────
APP_NAME = "SV Face ID"
BUNDLE_ID = "com.svtechnologies.faceid"
VERSION = "1.0.0"
onefile = False          # True → single exe (slower startup); False → folder
bundle = True            # macOS only: True → .app bundle
console = False          # True → show terminal window (useful for debugging)

# ── Paths ────────────────────────────────────────────────────────────
here = os.path.abspath(SPECPATH)
venv = os.path.join(here, "venv", "lib")

# Find site-packages dynamically
site_packages = None
for root, dirs, files in os.walk(venv):
    if root.endswith("site-packages"):
        site_packages = root
        break
if site_packages is None:
    raise FileNotFoundError("Cannot find site-packages under venv/")

face_models_dir = os.path.join(site_packages, "face_recognition_models", "models")

# ── Data files to bundle ─────────────────────────────────────────────
datas = [
    # face_recognition dlib models (≈100 MB total)
    (face_models_dir, os.path.join("face_recognition_models", "models")),
]

# Include the face_recognition_models __init__.py so pkg_resources can find it
fr_models_init = os.path.join(site_packages, "face_recognition_models")
datas.append((fr_models_init, "face_recognition_models"))

# ── Hidden imports ───────────────────────────────────────────────────
hiddenimports = [
    "face_recognition",
    "face_recognition_models",
    "dlib",
    "cv2",
    "numpy",
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.sip",
    "sqlite3",
    "csv",
    "json",
    "hashlib",
    "pickle",
    "fcntl",
    "face_attendance",
    "face_attendance.dashboard",
    "face_attendance.dashboard.app",
    "face_attendance.dashboard.engine",
    "face_attendance.dashboard.screens",
    "face_attendance.dashboard.screens.live",
    "face_attendance.dashboard.screens.realtime",
    "face_attendance.dashboard.screens.report",
    "face_attendance.dashboard.screens.employees",
    "face_attendance.dashboard.screens.settings",
    "face_attendance.dashboard.widgets",
    "face_attendance.dashboard.theme",
    "face_attendance.dashboard.bridge",
    "face_attendance.dashboard.sound",
    "face_attendance.dashboard.telegram",
    "face_attendance.dashboard.lock",
    "face_attendance.recognition",
    "face_attendance.tracking",
    "face_attendance.attendance",
    "face_attendance.camera",
    "face_attendance.models",
    "face_attendance.settings",
    "face_attendance.config",
    "face_attendance.storage",
    "face_attendance.persistence",
    "face_attendance.repository",
    "face_attendance.liveness",
    "face_attendance.geometry",
    "face_attendance.report",
]

# Windows doesn't have fcntl
if platform.system() == "Windows":
    hiddenimports.remove("fcntl")

# ── Excludes (trim size) ─────────────────────────────────────────────
excludes = [
    "tkinter", "_tkinter", "matplotlib", "scipy", "pandas",
    "IPython", "jupyter", "notebook", "pytest",
    "pip", "wheel",
]

# ── Analysis ─────────────────────────────────────────────────────────
a = Analysis(
    [os.path.join(here, "dashboard.py")],
    pathex=[here],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(here, "scripts", "pyi_rthook.py")],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# ── Build ────────────────────────────────────────────────────────────
if onefile:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas,
        name=APP_NAME,
        debug=False,
        strip=False,
        upx=True,
        console=console,
        icon=os.path.join(here, "icon.ico") if os.path.exists(os.path.join(here, "icon.ico")) else None,
    )
else:
    exe = EXE(
        pyz, a.scripts,
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        strip=False,
        upx=True,
        console=console,
        icon=os.path.join(here, "icon.ico") if os.path.exists(os.path.join(here, "icon.ico")) else None,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name=APP_NAME,
    )

# macOS .app bundle
if bundle and platform.system() == "Darwin" and not onefile:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=os.path.join(here, "icon.icns") if os.path.exists(os.path.join(here, "icon.icns")) else None,
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSCameraUsageDescription": "This app uses the camera for face recognition attendance.",
            "LSMinimumSystemVersion": "10.15",
        },
    )
