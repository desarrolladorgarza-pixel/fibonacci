"""
FIBONACCI — Capa de plataforma.

Hermes resuelve la portabilidad con instaladores distintos por SO, un Git Bash
embebido en Windows y una lista curada de extras para Termux. Funciona, pero la
lógica de plataforma queda regada por todo el código.

Fibonacci concentra TODA la variación de sistema operativo en este archivo.
El resto del código nunca pregunta en qué SO corre. Portar a un sistema nuevo
es implementar aquí y nada más.

Soportado: Linux, macOS, Windows (nativo, sin WSL), Android/Termux, BSD,
y cualquier ARM64 (Raspberry Pi, DGX, VPS).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Platform:
    os: str              # linux | macos | windows | android | bsd
    arch: str            # x86_64 | arm64 | ...
    shell: str
    is_termux: bool = False
    is_container: bool = False
    has_gpu: bool = False
    has_systemd: bool = False

    @property
    def unix(self) -> bool:
        return self.os != "windows"


def detect() -> Platform:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)

    termux = "com.termux" in os.environ.get("PREFIX", "")
    if termux or "ANDROID_ROOT" in os.environ:
        os_name = "android"
    elif sysname == "darwin":
        os_name = "macos"
    elif sysname == "windows":
        os_name = "windows"
    elif "bsd" in sysname:
        os_name = "bsd"
    else:
        os_name = "linux"

    container = Path("/.dockerenv").exists() or os.environ.get("container") is not None

    if os_name == "windows":
        shell = "powershell" if shutil.which("powershell") else "cmd"
    else:
        shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"

    gpu = bool(shutil.which("nvidia-smi")) or (os_name == "macos" and arch == "arm64")
    systemd = Path("/run/systemd/system").exists()

    return Platform(os_name, arch, shell, termux, container, gpu, systemd)


PLATFORM = detect()


# ---------------------------------------------------------------------------
# Rutas — cada SO tiene su convención y aquí se respeta
# ---------------------------------------------------------------------------

def home() -> Path:
    if PLATFORM.os == "android":
        return Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
    return Path.home()


def data_dir() -> Path:
    """Datos persistentes: memoria, journal, sesiones."""
    if PLATFORM.os == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", home() / "AppData/Local"))
    elif PLATFORM.os == "macos":
        base = home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", home() / ".local/share"))
    p = base / "fibonacci"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    if PLATFORM.os == "windows":
        base = Path(os.environ.get("APPDATA", home() / "AppData/Roaming"))
    elif PLATFORM.os == "macos":
        base = home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config"))
    p = base / "fibonacci"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace() -> Path:
    p = Path(os.environ.get("FIBONACCI_WORKSPACE", home() / "fibonacci"))
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------

def run(command: str, cwd: str | Path | None = None, timeout: int = 120) -> tuple[int, str]:
    """Ejecuta un comando con la shell correcta del sistema."""
    if PLATFORM.os == "windows":
        args = ["powershell", "-NoProfile", "-Command", command] \
            if PLATFORM.shell == "powershell" else ["cmd", "/c", command]
        shell = False
    else:
        args, shell = command, True

    try:
        r = subprocess.run(
            args, shell=shell, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"Tiempo agotado tras {timeout}s"


def notify(title: str, body: str) -> bool:
    """Notificación nativa. Silenciosa si el sistema no la soporta."""
    try:
        if PLATFORM.os == "macos":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        elif PLATFORM.os == "android" and shutil.which("termux-notification"):
            subprocess.run(["termux-notification", "-t", title, "-c", body],
                           capture_output=True, timeout=5)
        elif PLATFORM.os == "windows":
            ps = ('[Windows.UI.Notifications.ToastNotificationManager]::'
                  'CreateToastNotifier("Fibonacci")')
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f'Write-Host "{title}: {body}"; {ps}'],
                           capture_output=True, timeout=5)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], capture_output=True, timeout=5)
        else:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def clipboard(text: str) -> bool:
    cmds = {
        "macos": ["pbcopy"],
        "windows": ["clip"],
        "android": ["termux-clipboard-set"],
    }
    cmd = cmds.get(PLATFORM.os) or (
        ["wl-copy"] if shutil.which("wl-copy") else
        ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else None
    )
    if not cmd:
        return False
    try:
        subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if PLATFORM.os == "windows":
        return os.environ.get("WT_SESSION") is not None or sys.stdout.isatty()
    return sys.stdout.isatty()


def terminal_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:  # noqa: BLE001
        return default


def describe() -> str:
    p = PLATFORM
    bits = [f"{p.os}/{p.arch}"]
    if p.is_termux:
        bits.append("termux")
    if p.is_container:
        bits.append("contenedor")
    if p.has_gpu:
        bits.append("gpu")
    return " · ".join(bits)
