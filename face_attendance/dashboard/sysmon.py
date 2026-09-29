"""System resource monitor: background collection, main-thread consumption.

Provides ``static_info()`` (hardware summary, call once) and a ``Monitor``
QThread that emits a ``snapshot`` signal every *interval* seconds with live
CPU / RAM / GPU / disk / network stats.
"""
import os
import platform
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import psutil

from PyQt6.QtCore import QThread, pyqtSignal


# ── one-shot hardware info ─────────────────────────────────────────────────

@dataclass
class StaticInfo:
    cpu_name: str = ""
    cpu_cores_physical: int = 0
    cpu_cores_logical: int = 0
    ram_total_bytes: int = 0
    swap_total_bytes: int = 0
    gpu_name: str = ""
    gpu_vram_total_mb: int = 0
    os_name: str = ""
    os_version: str = ""
    hostname: str = ""


def static_info() -> StaticInfo:
    info = StaticInfo()
    info.cpu_cores_physical = psutil.cpu_count(logical=False) or 1
    info.cpu_cores_logical = psutil.cpu_count(logical=True) or 1
    info.ram_total_bytes = psutil.virtual_memory().total
    info.swap_total_bytes = psutil.swap_memory().total
    info.os_name = platform.system()
    info.os_version = platform.version()
    info.hostname = platform.node()

    # CPU name
    try:
        if sys.platform == "darwin":
            import subprocess
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=2)
            info.cpu_name = result.stdout.strip() or "Unknown CPU"
        elif sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            info.cpu_name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
            winreg.CloseKey(key)
        else:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        info.cpu_name = line.split(":", 1)[1].strip()
                        break
    except Exception:
        info.cpu_name = platform.processor() or "Unknown CPU"

    # GPU name (best effort)
    try:
        import subprocess
        if sys.platform == "darwin":
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-detailLevel", "mini"],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith(("Displays:", "Graphics", "Vendor",
                                                        "Device", "Bus", "VRAM", "Metal",
                                                        "Resolution", "Display Type",
                                                        "Connection", "Mirror", "Online",
                                                        "Automatic", "Rotation")):
                    if ":" in stripped and not stripped.endswith(":"):
                        continue
                    if stripped.endswith(":"):
                        info.gpu_name = stripped.rstrip(":")
                        break
        elif sys.platform == "win32":
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, text=True, timeout=5)
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip() and l.strip() != "Name"]
            if lines:
                info.gpu_name = lines[0]
    except Exception:
        pass

    return info


# ── live snapshot ──────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    timestamp: float = 0.0
    # CPU
    cpu_percent: float = 0.0
    cpu_per_core: List[float] = field(default_factory=list)
    cpu_freq_mhz: float = 0.0
    # RAM
    ram_used_bytes: int = 0
    ram_total_bytes: int = 0
    ram_percent: float = 0.0
    # Swap
    swap_used_bytes: int = 0
    swap_total_bytes: int = 0
    swap_percent: float = 0.0
    # Disk
    disk_used_bytes: int = 0
    disk_total_bytes: int = 0
    disk_percent: float = 0.0
    disk_read_bytes_sec: float = 0.0
    disk_write_bytes_sec: float = 0.0
    # Network
    net_sent_bytes_sec: float = 0.0
    net_recv_bytes_sec: float = 0.0
    # GPU (best effort)
    gpu_percent: float = 0.0
    gpu_vram_used_mb: float = 0.0
    gpu_vram_total_mb: float = 0.0
    gpu_temp_c: Optional[float] = None
    gpu_available: bool = False
    # System
    uptime_sec: float = 0.0
    battery_percent: Optional[float] = None
    battery_charging: bool = False
    # App
    app_ram_bytes: int = 0


def _take_snapshot(prev_disk_io, prev_net_io, prev_time):
    snap = Snapshot()
    now = time.monotonic()
    snap.timestamp = now
    dt = now - prev_time if prev_time else 1.0

    # CPU
    snap.cpu_percent = psutil.cpu_percent(interval=None)
    snap.cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    freq = psutil.cpu_freq()
    snap.cpu_freq_mhz = freq.current if freq else 0.0

    # RAM
    vm = psutil.virtual_memory()
    snap.ram_used_bytes = vm.used
    snap.ram_total_bytes = vm.total
    snap.ram_percent = vm.percent

    # Swap
    sw = psutil.swap_memory()
    snap.swap_used_bytes = sw.used
    snap.swap_total_bytes = sw.total
    snap.swap_percent = sw.percent

    # Disk
    try:
        usage = psutil.disk_usage("/")
        snap.disk_used_bytes = usage.used
        snap.disk_total_bytes = usage.total
        snap.disk_percent = usage.percent
    except Exception:
        pass
    try:
        dio = psutil.disk_io_counters()
        if dio and prev_disk_io and dt > 0:
            snap.disk_read_bytes_sec = max(0, (dio.read_bytes - prev_disk_io.read_bytes) / dt)
            snap.disk_write_bytes_sec = max(0, (dio.write_bytes - prev_disk_io.write_bytes) / dt)
        prev_disk_io = dio
    except Exception:
        pass

    # Network
    try:
        nio = psutil.net_io_counters()
        if nio and prev_net_io and dt > 0:
            snap.net_sent_bytes_sec = max(0, (nio.bytes_sent - prev_net_io.bytes_sent) / dt)
            snap.net_recv_bytes_sec = max(0, (nio.bytes_recv - prev_net_io.bytes_recv) / dt)
        prev_net_io = nio
    except Exception:
        pass

    # Uptime
    snap.uptime_sec = now - psutil.boot_time() if hasattr(psutil, "boot_time") else 0.0
    snap.uptime_sec = time.time() - psutil.boot_time()

    # Battery
    try:
        bat = psutil.sensors_battery()
        if bat:
            snap.battery_percent = bat.percent
            snap.battery_charging = bat.power_plugged or False
    except Exception:
        pass

    # App memory
    try:
        proc = psutil.Process(os.getpid())
        snap.app_ram_bytes = proc.memory_info().rss
    except Exception:
        pass

    return snap, prev_disk_io, prev_net_io


# ── background monitor thread ──────────────────────────────────────────────

class Monitor(QThread):
    """Emits ``snapshotReady(Snapshot)`` on every tick."""

    snapshotReady = pyqtSignal(object)

    def __init__(self, interval=1.0, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._stop = threading.Event()

    def run(self):
        psutil.cpu_percent(interval=None)
        prev_disk = prev_net = None
        prev_time = 0.0
        while not self._stop.is_set():
            snap, prev_disk, prev_net = _take_snapshot(prev_disk, prev_net, prev_time)
            prev_time = snap.timestamp
            self.snapshotReady.emit(snap)
            self._stop.wait(self._interval)

    def stop(self):
        self._stop.set()
        self.wait(3000)
