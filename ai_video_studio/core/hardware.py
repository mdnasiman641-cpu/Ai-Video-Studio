"""Hardware and FFmpeg capability detection (no Qt).

What the machine has (CPU, memory, graphics adapters) is read from the operating system; what
can actually be used is decided by FFmpeg itself: an encoder counts as usable only when this
FFmpeg build lists it **and** a short test encode with it succeeds. A GPU name or an installed
driver alone is never taken as proof.

:func:`detect` runs everything (a few seconds: one tiny test encode per hardware encoder) and
returns a :class:`HardwareInfo`; :func:`recommend` turns it into suggested settings (encoder,
parallel renders, encoder and transcription threads). Results are cached per FFmpeg path.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from ai_video_studio.core.process import run_process

log = logging.getLogger(__name__)

# hardware encoder family -> (H.264 encoder, H.265 encoder, label)
HW_FAMILIES: dict[str, tuple[str, str, str]] = {
    "nvenc": ("h264_nvenc", "hevc_nvenc", "NVIDIA NVENC"),
    "qsv": ("h264_qsv", "hevc_qsv", "Intel Quick Sync"),
    "amf": ("h264_amf", "hevc_amf", "AMD AMF"),
}
CPU_LABEL = "CPU software (x264 / x265)"
# preference order for "Auto" (a preference, not a promise that a GPU is faster for every job)
AUTO_ORDER = ("nvenc", "qsv", "amf")


@dataclass
class GpuInfo:
    name: str
    vendor: str            # nvidia | amd | intel | other
    memory_mb: int | None = None
    driver: str | None = None


@dataclass
class EncoderCheck:
    name: str              # e.g. h264_nvenc
    listed: bool           # compiled into this FFmpeg build
    usable: bool           # a test encode succeeded
    error: str = ""        # why it is not usable (the real FFmpeg message)


@dataclass
class HardwareInfo:
    os: str = ""
    cpu_name: str = ""
    logical_cores: int = 1
    physical_cores: int = 1
    memory_gb: float | None = None
    gpus: list[GpuInfo] = field(default_factory=list)
    ffmpeg_version: str = ""
    encoders: dict[str, EncoderCheck] = field(default_factory=dict)
    hwaccels: list[str] = field(default_factory=list)  # hardware decoders FFmpeg was built with

    def usable(self, encoder: str) -> bool:
        check = self.encoders.get(encoder)
        return bool(check and check.usable)

    def family_usable(self, family: str, codec: str = "h264") -> bool:
        h264, h265, _label = HW_FAMILIES[family]
        return self.usable(h264 if codec == "h264" else h265)

    def summary_lines(self) -> list[str]:
        lines = [f"CPU: {self.cpu_name or 'unknown'} ({self.physical_cores} cores, {self.logical_cores} threads)",
                 f"Memory: {self.memory_gb:.0f} GB" if self.memory_gb else "Memory: unknown"]
        lines += [f"GPU: {g.name}" + (f" ({g.memory_mb // 1024} GB)" if g.memory_mb and g.memory_mb >= 1024 else "")
                  for g in self.gpus] or ["GPU: none found"]
        lines.append(f"FFmpeg: {self.ffmpeg_version or 'unknown'}")
        for family, (h264, h265, label) in HW_FAMILIES.items():
            states = []
            for name in (h264, h265):
                c = self.encoders.get(name)
                states.append(f"{name} " + ("✔ usable" if c and c.usable else "listed, test failed" if c and c.listed
                                            else "not in FFmpeg"))
            lines.append(f"{label}: " + ", ".join(states))
        lines.append("Hardware decoding (FFmpeg): " + (", ".join(self.hwaccels) or "none"))
        return lines

    def to_dict(self) -> dict:
        return asdict(self)


# -- operating system ---------------------------------------------------------------------

def _powershell_json(command: str) -> list[dict]:
    """Run a read-only PowerShell CIM query and return its rows (Windows only)."""
    try:
        result = run_process(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              command + " | ConvertTo-Json -Compress"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def _vendor(name: str) -> str:
    lower = name.lower()
    if "nvidia" in lower or "geforce" in lower or "quadro" in lower or "rtx" in lower:
        return "nvidia"
    if "amd" in lower or "radeon" in lower or "ati " in lower:
        return "amd"
    if "intel" in lower or "arc" in lower.split():
        return "intel"
    return "other"


@lru_cache(maxsize=1)
def cpu_info() -> tuple[str, int, int]:
    """``(name, logical cores, physical cores)``."""
    logical = os.cpu_count() or 1
    name, physical = "", 0
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            name = platform.processor()
        rows = _powershell_json("Get-CimInstance Win32_Processor | Select-Object NumberOfCores")
        physical = sum(int(r.get("NumberOfCores") or 0) for r in rows)
    elif Path("/proc/cpuinfo").is_file():
        text = Path("/proc/cpuinfo").read_text(errors="replace")
        match = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
        name = match.group(1).strip() if match else platform.processor()
        cores = set(re.findall(r"^physical id\s*:\s*(\d+)\s*$.*?^core id\s*:\s*(\d+)\s*$", text, re.M | re.S))
        physical = len(cores)
    else:
        name = platform.processor()
    return name or "unknown", logical, physical or max(1, logical // 2 if logical > 2 else logical)


def memory_gb() -> float | None:
    if sys.platform == "win32":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong),
                            ("avail_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                            ("avail_virtual", ctypes.c_ulonglong), ("avail_ext", ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total / 1024 ** 3
        except (OSError, AttributeError):
            return None
        return None
    try:
        match = re.search(r"^MemTotal:\s*(\d+)\s*kB", Path("/proc/meminfo").read_text(), re.M)
        return int(match.group(1)) / 1024 ** 2 if match else None
    except OSError:
        return None


def gpu_info() -> list[GpuInfo]:
    gpus: list[GpuInfo] = []
    if sys.platform == "win32":
        for row in _powershell_json("Get-CimInstance Win32_VideoController | "
                                    "Select-Object Name, AdapterRAM, DriverVersion"):
            name = str(row.get("Name") or "").strip()
            if name:
                ram = row.get("AdapterRAM")
                gpus.append(GpuInfo(name, _vendor(name), int(ram) // 1024 ** 2 if isinstance(ram, int) and ram > 0
                                    else None, row.get("DriverVersion")))
    try:  # NVIDIA: exact memory (Windows reports at most 4 GB in AdapterRAM)
        result = run_process(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                              "--format=csv,noheader,nounits"], timeout=10)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 3:
                    known = next((g for g in gpus if g.vendor == "nvidia" and parts[0] in g.name), None)
                    if known:
                        known.memory_mb, known.driver = int(float(parts[1])), parts[2]
                    else:
                        gpus.append(GpuInfo(parts[0], "nvidia", int(float(parts[1])), parts[2]))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    if not gpus and sys.platform.startswith("linux"):
        vendors = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
            try:
                vendor = (card / "device" / "vendor").read_text().strip()
            except OSError:
                continue
            if vendor in vendors:
                gpus.append(GpuInfo(f"{vendors[vendor].upper()} graphics ({card.name})", vendors[vendor]))
    return gpus


# -- FFmpeg ---------------------------------------------------------------------------------

def ffmpeg_version(ffmpeg: Path | str) -> str:
    try:
        result = run_process([str(ffmpeg), "-hide_banner", "-version"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    first = (result.stdout.splitlines() or [""])[0]
    match = re.match(r"ffmpeg version (\S+)", first)
    return match.group(1) if match else first.strip()


def hwaccels(ffmpeg: Path | str) -> list[str]:
    try:
        result = run_process([str(ffmpeg), "-hide_banner", "-hwaccels"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    return [ln for ln in lines if not ln.lower().startswith("hardware acceleration")]


_cache: dict[str, HardwareInfo] = {}
_lock = threading.Lock()


def detect(ffmpeg: Path | str | None, refresh: bool = False) -> HardwareInfo:
    """Detect the machine and what this FFmpeg can use on it (cached per FFmpeg path)."""
    key = str(ffmpeg)
    with _lock:
        if key in _cache and not refresh:
            return _cache[key]
    from ai_video_studio.core.ffmpeg_commands import available_encoders

    name, logical, physical = cpu_info()
    info = HardwareInfo(os=f"{platform.system()} {platform.release()}", cpu_name=name, logical_cores=logical,
                        physical_cores=physical, memory_gb=memory_gb(), gpus=gpu_info())
    if ffmpeg:
        info.ffmpeg_version = ffmpeg_version(ffmpeg)
        info.hwaccels = hwaccels(ffmpeg)
        try:
            listed = available_encoders(ffmpeg)
        except (OSError, subprocess.TimeoutExpired):
            listed = set()
        from ai_video_studio.core.ffmpeg_commands import encoder_error, nvenc_usable

        for h264, h265, _label in HW_FAMILIES.values():
            for encoder in (h264, h265):
                # the same cached test encode the exports use, so this report and the export agree
                usable = nvenc_usable(ffmpeg, encoder)
                info.encoders[encoder] = EncoderCheck(encoder, encoder in listed, usable,
                                                      "" if usable else encoder_error(ffmpeg, encoder))
        for encoder in ("libx264", "libx265"):
            info.encoders[encoder] = EncoderCheck(encoder, encoder in listed, encoder in listed,
                                                  "" if encoder in listed else "not included in this FFmpeg build")
    log.info("Hardware detected", extra={"cpu": info.cpu_name, "threads": info.logical_cores, "gpus":
                                         [g.name for g in info.gpus],
                                         "usable_encoders": [e for e, c in info.encoders.items() if c.usable]})
    with _lock:
        _cache[key] = info
    return info


def cached(ffmpeg: Path | str | None) -> HardwareInfo | None:
    with _lock:
        return _cache.get(str(ffmpeg))


@dataclass
class Recommendation:
    encoder: str                 # auto | nvenc | qsv | amf | cpu
    encoder_label: str
    max_renders: int             # exports at the same time
    encoder_threads: int         # 0 = FFmpeg decides
    transcription_threads: int   # CPU threads for Whisper
    notes: list[str] = field(default_factory=list)


def recommend(info: HardwareInfo) -> Recommendation:
    """Suggested settings for this machine. Analysis (Whisper, face tracking) stays one at a time."""
    family = next((f for f in AUTO_ORDER if info.family_usable(f)), None)
    notes = []
    memory = info.memory_gb or 8
    if family:
        # a hardware encoder leaves the CPU for decoding and filters, so two exports can overlap
        renders = 2 if info.physical_cores >= 6 and memory >= 15 else 1
        label = HW_FAMILIES[family][2]
        notes.append(f"{label} passed a test encode; it is used for H.264/H.265 in Auto mode.")
    else:
        renders = 1  # x264 already uses every core for one export
        label = CPU_LABEL
        notes.append("No hardware encoder passed a test encode; exports use the CPU (x264/x265).")
    threads = max(2, info.logical_cores // renders) if renders > 1 else 0
    whisper = max(1, min(info.physical_cores, 16))
    notes.append(f"Speech recognition on the CPU uses {whisper} threads (one per physical core).")
    return Recommendation("auto", label, renders, threads, whisper, notes)
