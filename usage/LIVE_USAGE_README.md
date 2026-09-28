# Live Usage — Feature Plan

A real-time view inside the desktop app (Windows + macOS) showing CPU, GPU, RAM, disk, network and more, updating every second with a modern, "cool" look.

---

## Goal

Give users a real-time view of how their machine and the app are performing, without slowing the app down. It should feel polished: dark, clean, animated, and readable at a glance.

---

## What It Shows

| Area | Details |
|---|---|
| **CPU** | Total %, per-core %, clock speed, model name |
| **GPU** | Usage %, VRAM and temperature on NVIDIA; usage % on AMD/Intel (Windows) and Mac |
| **RAM** | Used / total / %, plus swap |
| **Disk** | Space used / total, live read and write speed |
| **Network** | Live upload and download speed |
| **System** | OS, uptime, battery % and charging state |
| **App itself** | How much RAM this app is using |

---

## Design ("Cool" Look)

- **Theme:** near-black background with one neon accent colour (cyan or purple).
- **Ring gauges** for CPU, RAM and GPU, with the % in the centre.
- **Colour by load:** green under 60%, amber from 60–85%, red above 85%.
- **Live graphs** of the last 60 seconds for CPU, GPU and network up/down.
- **Per-core grid:** one small tile per core that glows brighter as it gets busier.
- **Info strip** along the bottom: CPU name, GPU name, uptime, battery, app RAM.
- **Smooth motion:** values ease between updates instead of jumping.
- Clear typography, and the % numbers always readable.

---

## How Users Open It

- Menu item: **View → Live Usage**
- Hotkey: **Ctrl+Shift+M** (Windows) / **Cmd+Shift+M** (Mac)
- **Status bar mini readout** (CPU % and RAM %) that opens the full Live Usage panel when clicked
- It can be a **dockable side panel** or a **floating always-on-top mini window**

---

## Performance Rules

- Stats are collected in a **background thread**. The UI is only updated from the main thread.
- Default refresh is **1 second**. GPU on Mac refreshes every **2 seconds**, because it's slower to read.
- Hardware info (CPU/GPU names) is read **once, in the background**, at startup. On Mac this takes 1–2 seconds and must never freeze the UI.
- **Stop collecting when the Live Usage panel is hidden**, so it costs nothing while closed.
- Only redraw what changed. Never rebuild widgets on every update.

---

## Platform Notes

- **Windows:** needs `pywin32` for non-NVIDIA GPU usage. Install it on Windows only, using a platform marker in requirements.
- **macOS:** CPU/GPU temperature needs admin rights, so it isn't shown. Everything else works without sudo.
- **NVIDIA GPUs:** full stats including temperature.
- **No GPU detected:** hide the GPU section cleanly. Don't show errors or zeros.

---

## Optional Extras

- **Alerts:** notify when, for example, RAM stays above 90% for 30 seconds.
- **History export:** save the last N minutes to CSV.
- **Settings:** refresh interval, which sections to show, always-on-top, theme.
- **Top processes:** the top 5 processes by CPU and RAM.

---

## Build Order

1. Add the stats backend (`sysmon.py`) and dependencies, then verify the numbers on Windows and Mac.
2. Build the ring gauges and the status bar mini readout.
3. Add the live 60-second graphs and the per-core grid.
4. Wire up the menu item, hotkey, and dock/floating modes.
5. Add the optional extras.
6. Test the packaged build (PyInstaller) on both platforms.

---

## Release

- Commit as `feat: live usage`, which makes it a minor version bump (e.g. v1.3.0 → v1.4.0).
- The existing CI builds for Windows and macOS pick it up automatically.
- If the Windows build complains about a missing module, add `win32pdh` as a PyInstaller hidden import.

---

## Done When

- [ ] Numbers match Task Manager (Windows) and Activity Monitor (Mac) within a few %
- [ ] UI stays smooth, with no freezes on open or while running
- [ ] App CPU overhead stays under ~1–2% with the Live Usage panel open
- [ ] Nothing runs while the Live Usage panel is closed
- [ ] GPU section handles NVIDIA, AMD/Intel, Apple Silicon and "no GPU"
- [ ] Works in the packaged builds on both Windows and macOS
