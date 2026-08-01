"""
FIBONACCI — Control de equipo.

Lo que Hermes no hace: ver la pantalla y operar la máquina como lo haría una
persona, local o remota.

## Honestidad sobre reversibilidad

El journal de Fibonacci se construyó sobre una premisa: toda mutación declara
su inverso. **El control de GUI rompe esa premisa y hay que decirlo.** No
existe un "des-clic". Si el agente pulsa "Enviar", se envió.

Por eso el control de pantalla no finge ser reversible. Hace otra cosa:

  - **Registro forense.** Captura antes y después de cada acción. No puedes
    deshacerlo, pero puedes ver exactamente qué pasó y cuándo.
  - **Ámbito por ventana.** El agente opera libre dentro de las aplicaciones
    que declaraste; fuera de ellas se detiene. Una acción en tu editor no es
    lo mismo que una en tu banco.
  - **Detección de destino peligroso.** Antes de escribir o pulsar, se
    inspecciona el título de la ventana activa. Bancos, correo, terminales
    con sudo y gestores de contraseñas exigen confirmación siempre.

## Remoto

SSH y SFTP. Las operaciones de archivo remotas SÍ son reversibles: se
descarga una copia previa antes de escribir, igual que en local. Los comandos
remotos no lo son, y se marcan como tales.

Sin dependencias: usa los binarios `ssh`/`scp` del sistema. Si tienes
`paramiko` instalado se usa, pero no es requisito.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .platform import PLATFORM, data_dir

log = logging.getLogger("fibonacci.control")


# ---------------------------------------------------------------------------
# Visión de pantalla
# ---------------------------------------------------------------------------

@dataclass
class Screenshot:
    path: Path
    width: int = 0
    height: int = 0
    window_title: str = ""
    ts: float = field(default_factory=time.time)

    def as_base64(self) -> str:
        return base64.b64encode(self.path.read_bytes()).decode()


class ScreenError(RuntimeError):
    pass


def _shot_dir() -> Path:
    d = data_dir() / "capturas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture(region: tuple[int, int, int, int] | None = None) -> Screenshot:
    """
    Captura de pantalla con las herramientas nativas de cada sistema. Se
    prefieren binarios del SO sobre librerías Python: menos dependencias y
    mejor comportamiento con Wayland, permisos de macOS y multi-monitor.
    """
    dest = _shot_dir() / f"{int(time.time()*1000)}.png"

    if PLATFORM.os == "macos":
        cmd = ["screencapture", "-x"]
        if region:
            x, y, w, h = region
            cmd += ["-R", f"{x},{y},{w},{h}"]
        cmd.append(str(dest))
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        if r.returncode != 0:
            raise ScreenError(
                "screencapture falló. macOS exige permiso de Grabación de "
                "Pantalla: Ajustes > Privacidad y seguridad > Grabación de pantalla.")

    elif PLATFORM.os == "windows":
        ps = f'''
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$bmp.Save("{dest}", [System.Drawing.Imaging.ImageFormat]::Png)
'''
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            raise ScreenError(f"captura falló: {r.stderr.decode(errors='replace')[:200]}")

    elif PLATFORM.os == "android":
        if not shutil.which("termux-screenshot"):
            raise ScreenError("instala termux-api: pkg install termux-api")
        subprocess.run(["termux-screenshot", str(dest)], capture_output=True, timeout=20)

    else:  # Linux / BSD
        for tool, cmd in (
            ("grim", ["grim", str(dest)]),                       # Wayland
            ("spectacle", ["spectacle", "-b", "-n", "-o", str(dest)]),
            ("gnome-screenshot", ["gnome-screenshot", "-f", str(dest)]),
            ("scrot", ["scrot", "-o", str(dest)]),
            ("import", ["import", "-window", "root", str(dest)]),
        ):
            if shutil.which(tool):
                subprocess.run(cmd, capture_output=True, timeout=20)
                break
        else:
            raise ScreenError(
                "sin herramienta de captura. Instala una: grim (Wayland), "
                "scrot o gnome-screenshot (X11).")

    if not dest.exists() or dest.stat().st_size == 0:
        raise ScreenError("la captura salió vacía (¿permisos del sistema?)")

    return Screenshot(path=dest, window_title=active_window())


def active_window() -> str:
    """Título de la ventana enfocada. Es la base del control de ámbito."""
    try:
        if PLATFORM.os == "macos":
            script = ('tell application "System Events" to get name of first '
                      'application process whose frontmost is true')
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip()
        if PLATFORM.os == "windows":
            ps = ('Add-Type -AssemblyName Microsoft.VisualBasic; '
                  '(Get-Process | Where-Object {$_.MainWindowTitle} | '
                  'Sort-Object -Property StartTime -Descending | '
                  'Select-Object -First 1).MainWindowTitle')
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=8)
            return r.stdout.strip()
        for cmd in (["xdotool", "getactivewindow", "getwindowname"],
                    ["xprop", "-root", "_NET_ACTIVE_WINDOW"]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                return r.stdout.strip()[:200]
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------------------------------------------------------------------------
# Ámbito por ventana: dónde el agente puede actuar sin preguntar
# ---------------------------------------------------------------------------

# Contextos donde SIEMPRE se confirma, sin importar la configuración. No es
# paranoia: es que un clic equivocado aquí no tiene arreglo.
SENSITIVE_WINDOWS = [
    r"banc|bank|bbva|santander|banorte|hsbc|citi|paypal|stripe",
    r"1password|bitwarden|lastpass|keepass|llavero|keychain",
    r"gmail|outlook|correo|mail\b",
    r"sudo|root@|administrador|administrator",
    r"aws console|azure portal|gcp|cloud console",
    r"github.*settings|configuraci.n.*cuenta|account settings",
    r"transferencia|payment|checkout|pagar",
]
_SENSITIVE = [re.compile(p, re.I) for p in SENSITIVE_WINDOWS]


def window_is_sensitive(title: str) -> str | None:
    for pat in _SENSITIVE:
        if pat.search(title or ""):
            return pat.pattern
    return None


class InputError(RuntimeError):
    pass


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    if PLATFORM.os == "macos":
        clicks = 2 if double else 1
        script = (f'tell application "System Events" to click at {{{x}, {y}}}')
        for _ in range(clicks):
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    elif PLATFORM.os == "windows":
        ps = f'''
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({x},{y})
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern void mouse_event(uint f,uint x,uint y,uint d,int e);' -Name U -Namespace W
[W.U]::mouse_event(0x02,0,0,0,0); [W.U]::mouse_event(0x04,0,0,0,0)
'''
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=10)
    elif _has("xdotool"):
        btn = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        subprocess.run(["xdotool", "mousemove", str(x), str(y), "click",
                        "--repeat", "2" if double else "1", btn],
                       capture_output=True, timeout=10)
    elif _has("ydotool"):
        subprocess.run(["ydotool", "mousemove", "-a", "-x", str(x), "-y", str(y)],
                       capture_output=True, timeout=10)
        subprocess.run(["ydotool", "click", "0xC0"], capture_output=True, timeout=10)
    else:
        raise InputError("sin backend de entrada. Instala xdotool (X11) o "
                         "ydotool (Wayland).")
    return f"clic {button}{' doble' if double else ''} en ({x}, {y})"


def type_text(text: str) -> str:
    if PLATFORM.os == "macos":
        safe = text.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to keystroke "{safe}"'],
                       capture_output=True, timeout=30)
    elif PLATFORM.os == "windows":
        safe = text.replace("'", "''")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Add-Type -AssemblyName System.Windows.Forms; "
                        f"[System.Windows.Forms.SendKeys]::SendWait('{safe}')"],
                       capture_output=True, timeout=30)
    elif _has("xdotool"):
        subprocess.run(["xdotool", "type", "--delay", "12", "--", text],
                       capture_output=True, timeout=60)
    elif _has("ydotool"):
        subprocess.run(["ydotool", "type", text], capture_output=True, timeout=60)
    else:
        raise InputError("sin backend de entrada")
    return f"escrito ({len(text)} caracteres)"


def press_key(key: str) -> str:
    """Teclas y combinaciones: 'Return', 'ctrl+s', 'cmd+shift+4'."""
    if PLATFORM.os == "macos":
        mods = {"cmd": "command down", "ctrl": "control down",
                "alt": "option down", "shift": "shift down"}
        parts = key.lower().split("+")
        base, using = parts[-1], [mods[p] for p in parts[:-1] if p in mods]
        codes = {"return": "return", "enter": "return", "tab": "tab",
                 "escape": "escape", "esc": "escape", "space": "space",
                 "delete": "delete", "backspace": "delete"}
        if base in codes:
            script = 'tell application "System Events" to key code 36' \
                if codes[base] == "return" else \
                f'tell application "System Events" to keystroke {codes[base]}'
            if using:
                script = (f'tell application "System Events" to keystroke "{base}" '
                          f'using {{{", ".join(using)}}}')
        else:
            script = f'tell application "System Events" to keystroke "{base}"'
            if using:
                script += f' using {{{", ".join(using)}}}'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    elif PLATFORM.os == "windows":
        m = {"ctrl": "^", "alt": "%", "shift": "+"}
        parts = key.lower().split("+")
        seq = "".join(m.get(p, "") for p in parts[:-1])
        named = {"return": "{ENTER}", "enter": "{ENTER}", "tab": "{TAB}",
                 "escape": "{ESC}", "esc": "{ESC}", "backspace": "{BACKSPACE}"}
        seq += named.get(parts[-1], parts[-1])
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Add-Type -AssemblyName System.Windows.Forms; "
                        f"[System.Windows.Forms.SendKeys]::SendWait('{seq}')"],
                       capture_output=True, timeout=10)
    elif _has("xdotool"):
        subprocess.run(["xdotool", "key", key.replace("cmd", "super")],
                       capture_output=True, timeout=10)
    else:
        raise InputError("sin backend de entrada")
    return f"tecla {key}"


def scroll(amount: int) -> str:
    if _has("xdotool"):
        btn = "4" if amount > 0 else "5"
        subprocess.run(["xdotool", "click", "--repeat", str(abs(amount)), btn],
                       capture_output=True, timeout=15)
        return f"scroll {amount}"
    if PLATFORM.os == "macos":
        subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to scroll {abs(amount)} '
                        f'{"up" if amount > 0 else "down"}'],
                       capture_output=True, timeout=10)
        return f"scroll {amount}"
    raise InputError("scroll no soportado en esta plataforma")


def input_backend() -> str:
    """Qué backend hay disponible. `fib doctor` lo reporta."""
    if PLATFORM.os == "macos":
        return "osascript (requiere permiso de Accesibilidad)"
    if PLATFORM.os == "windows":
        return "SendKeys"
    if _has("xdotool"):
        return "xdotool (X11)"
    if _has("ydotool"):
        return "ydotool (Wayland)"
    return "ninguno — instala xdotool o ydotool"


# ---------------------------------------------------------------------------
# Máquinas remotas
# ---------------------------------------------------------------------------

@dataclass
class RemoteHost:
    """
    Un servidor bajo control. `scope` decide su autonomía: un staging puede
    ser `free` y producción `confirm`. Esa distinción es la que permite darle
    root real a algo sin perder el sueño.
    """

    alias: str
    host: str
    user: str = ""
    port: int = 22
    key_file: str = ""
    scope: str = "confirm"          # free | confirm | readonly
    note: str = ""

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _args_comunes(self) -> list[str]:
        args = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10"]
        if self.key_file:
            args += ["-i", str(Path(self.key_file).expanduser())]
        return args

    def ssh_args(self) -> list[str]:
        return [*self._args_comunes(), "-p", str(self.port)]

    def scp_args(self) -> list[str]:
        """
        `scp` quiere el puerto en `-P` mayúscula; `ssh` en `-p` minúscula.

        Antes esto se resolvía filtrando `"-p"` de `ssh_args()`, que quitaba el
        flag **pero dejaba el número suelto**: scp lo interpretaba como un
        archivo de origen y abortaba con `stat local "22"`. Como `ssh_args()`
        siempre incluye el puerto, `write()` y `fetch()` fallaban en TODOS los
        hosts, no solo en los de puerto no estándar — y `fetch()` es la copia
        previa de la que depende el undo remoto.
        """
        return [*self._args_comunes(), "-P", str(self.port)]


class RemoteError(RuntimeError):
    pass


class Remote:
    """
    Control remoto sobre `ssh`/`scp` del sistema. Sin dependencias.

    Las operaciones de archivo remotas SÍ son reversibles: se descarga una
    copia previa antes de escribir, exactamente como en local. Los comandos
    remotos no lo son y se declaran así.
    """

    def __init__(self, hosts: dict[str, RemoteHost] | None = None):
        self.hosts = hosts or {}
        if not shutil.which("ssh"):
            log.warning("`ssh` no está en PATH: el control remoto no funcionará")

    def add(self, host: RemoteHost) -> None:
        self.hosts[host.alias] = host

    def get(self, alias: str) -> RemoteHost:
        h = self.hosts.get(alias)
        if h is None:
            raise RemoteError(
                f"host '{alias}' no registrado. Añádelo con `fib host add`. "
                f"Conocidos: {', '.join(self.hosts) or 'ninguno'}")
        return h

    # -- comandos --------------------------------------------------------

    def run(self, alias: str, command: str, timeout: int = 120) -> tuple[int, str]:
        h = self.get(alias)
        if h.scope == "readonly" and _mutates_shell(command):
            raise RemoteError(
                f"'{alias}' está declarado readonly y este comando modifica el "
                "sistema. Cambia su ámbito con `fib host scope` si es intencional.")
        r = subprocess.run(["ssh", *h.ssh_args(), h.target, command],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def probe(self, alias: str) -> dict:
        """Reconocimiento inicial: qué es esta máquina y qué corre."""
        h = self.get(alias)
        code, out = self.run(alias, "uname -a; echo ---; uptime; echo ---; "
                                    "df -h / | tail -1; echo ---; "
                                    "(systemctl --no-pager --type=service --state=running "
                                    "2>/dev/null | head -20 || ps aux --sort=-%mem | head -10)")
        return {"alias": alias, "host": h.host, "scope": h.scope,
                "ok": code == 0, "info": out[:6000]}

    # -- archivos (reversibles) ------------------------------------------

    def read(self, alias: str, path: str, max_bytes: int = 200_000) -> str:
        code, out = self.run(alias, f"head -c {max_bytes} {_q(path)}")
        if code != 0:
            raise RemoteError(f"no se pudo leer {path}: {out[:200]}")
        return out

    def _scp(self, h: RemoteHost, origen: str, destino: str):
        """
        `scp` con respaldo al protocolo antiguo.

        OpenSSH 9 copia por el subsistema SFTP, que no todos los servidores
        exponen: dropbear, busybox y cualquier host endurecido que lo haya
        quitado del `sshd_config` fallan con "subsystem request failed". En
        esos casos `-O` usa el protocolo SCP clásico, que sí funciona. Sin el
        respaldo, la copia remota no falla a medias: falla del todo.
        """
        cmd = ["scp", *h.scp_args(), origen, destino]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 and "subsystem" in (r.stderr or "").lower():
            log.info("El servidor no expone SFTP; reintentando con -O")
            cmd = ["scp", "-O", *h.scp_args(), origen, destino]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                               encoding="utf-8", errors="replace")
        return r

    def fetch(self, alias: str, remote_path: str, local_path: Path) -> bool:
        """Copia previa: la base del undo remoto."""
        h = self.get(alias)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        r = self._scp(h, f"{h.target}:{remote_path}", str(local_path))
        return r.returncode == 0 and local_path.exists()

    def write(self, alias: str, path: str, content: str) -> str:
        h = self.get(alias)
        if h.scope == "readonly":
            raise RemoteError(f"'{alias}' es readonly")
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8",
                                         suffix=".fib") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            r = self._scp(h, tmp_path, f"{h.target}:{path}")
            if r.returncode != 0:
                raise RemoteError(f"escritura falló: {r.stderr[:200]}")
            return f"escrito {alias}:{path} ({len(content)} bytes)"
        finally:
            os.unlink(tmp_path)

    def push(self, alias: str, local_path: Path | str, remote_path: str) -> str:
        """
        Sube un archivo local que ya existe. **Lanza si falla.**

        Es lo que necesita el undo remoto: restaurar la copia previa. Que falle
        en silencio es justo lo que no puede pasar ahí.
        """
        h = self.get(alias)
        r = self._scp(h, str(local_path), f"{h.target}:{remote_path}")
        if r.returncode != 0:
            raise RemoteError(
                f"no se pudo subir a {alias}:{remote_path}: {(r.stderr or '')[:200]}")
        return f"subido a {alias}:{remote_path}"

    def exists(self, alias: str, path: str) -> bool:
        code, _ = self.run(alias, f"test -e {_q(path)}")
        return code == 0

    def health(self, alias: str) -> bool:
        try:
            code, _ = self.run(alias, "echo ok", timeout=15)
            return code == 0
        except Exception:  # noqa: BLE001
            return False


_MUTATING_SHELL = re.compile(
    r"\b(rm|mv|cp|dd|mkfs|chown|chmod|chgrp|ln|touch|mkdir|rmdir|"
    r"systemctl\s+(start|stop|restart|reload|enable|disable)|service|"
    r"apt|apt-get|yum|dnf|pacman|apk|snap|brew|"
    r"pip\s+install|pip3\s+install|npm\s+(i|install)|yarn\s+add|cargo\s+install|"
    r"docker\s+(run|rm|stop|start|exec|build)|kubectl\s+(apply|delete|patch)|"
    r"kill|pkill|killall|truncate|tee|sed\s+-i|crontab|useradd|userdel|"
    r"iptables|ufw|mount|umount|reboot|shutdown|halt)\b")

# Las redirecciones se detectan aparte: `>` no es caracter de palabra, asi que
# `\b>` nunca coincide. Es el tipo de bug que un regex "obvio" esconde bien.
_REDIRECT = re.compile(r"(?<![0-9<>])>{1,2}(?!&)")


def _mutates_shell(cmd: str) -> bool:
    """¿Este comando modifica el sistema remoto?"""
    return bool(_MUTATING_SHELL.search(cmd) or _REDIRECT.search(cmd))


def _q(path: str) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"
