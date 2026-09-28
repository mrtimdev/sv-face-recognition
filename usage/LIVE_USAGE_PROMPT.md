Add a **Live Usage** feature to my existing Python desktop app (Windows + macOS): a real-time view of CPU, GPU, RAM and more that updates every second. Read my project first, and fit into its existing structure, GUI framework and style. Make targeted changes. Don't rewrite existing code.

**Backend:** Use the attached `sysmon.py` as the stats source. It already provides `static_info()` (hardware info, call once), `snapshot()` (live stats) and a `Monitor` background thread. Add its dependencies to my requirements, with `pywin32` for Windows only.

**What to show:**
- CPU: total %, per-core %, clock speed, model name
- GPU: usage %, plus VRAM and temperature when available
- RAM and swap
- Disk: space, plus read/write speed
- Network: upload and download speed
- Uptime, battery, and this app's own RAM use

**Design, modern and cool:**
- Dark theme with one neon accent colour
- Ring gauges for CPU, RAM and GPU, with the % in the centre
- Colours by load: green under 60%, amber 60–85%, red above 85%
- Live 60-second graphs for CPU, GPU and network
- A per-core tile grid that glows with load
- An info strip at the bottom
- Values that ease smoothly between updates instead of jumping
- Use the fastest suitable chart approach for my framework (e.g. pyqtgraph for Qt)

**How users open it:**
- Menu item "View → Live Usage"
- Hotkey Ctrl+Shift+M (Cmd+Shift+M on Mac)
- A small CPU/RAM readout in the status bar that opens the Live Usage panel when clicked
- The Live Usage panel works as a dockable side panel or a floating always-on-top window

**Performance, must follow:**
- Collect stats in a background thread and update the UI only on the main thread
- Refresh every 1s; refresh the GPU on Mac every 2s
- Load `static_info()` in the background so startup never freezes
- Stop collecting when the Live Usage panel is hidden
- Update only the values that changed; never rebuild widgets on each tick

**Edge cases:**
- No GPU: hide that section cleanly
- Missing data (temperature, battery, frequency): hide it rather than showing 0 or an error
- Must work in the packaged PyInstaller build on both OSes

**Deliver:**
- Briefly explain which files you changed and why
- Note anything I need to test on Windows vs macOS
- Keep the optional extras (alerts, CSV export, settings, top processes) out for now, but make them easy to add later
