"""Point OBS, recso, Audacity and DaVinci Resolve at a project's folders, then open them."""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
from pathlib import Path

import core

HOME = Path.home()
OBS_CONFIG = HOME / ".config" / "obs-studio"
OBS_WEBSOCKET_CONFIG = OBS_CONFIG / "plugin_config" / "obs-websocket" / "config.json"
AUDACITY_CFG = HOME / ".config" / "audacity" / "audacity.cfg"
RESOLVE_BIN = Path("/opt/resolve/bin/resolve")
RESOLVE_DATA = HOME / ".local" / "share" / "DaVinciResolve"
RESOLVE_CONFIG = RESOLVE_DATA / "configs" / "config.dat"
RESOLVE_USER_CONFIG = RESOLVE_DATA / "configs" / "config.user.xml"
RESOLVE_PROJECTS = RESOLVE_DATA / "Resolve Project Library" / "Resolve Projects" / "Users"
# Resolve's first Media Storage location doubles as its scratch disk (CacheClip,
# .gallery, project backups), so it gets its own hidden folder; the projects
# folder is added as a second location, purely for browsing.
RESOLVE_SCRATCH = HOME / "Videos" / ".resolve"
RESOLVE_SCRATCH_DIRS = ("CacheClip", ".gallery", "Resolve Project Backups")
RECSO_APPIMAGES = HOME / "Applications"
# Errors show in a toast, which cuts off long text.
RECSO_API_OFF = "Turn on recso's Automation API (Settings ▸ Advanced), then press Open again."


class AppError(Exception):
    """A problem the user can fix; the message is shown as-is."""


def launch(command: list[str]) -> None:
    prefix = ["uwsm-app", "--"] if shutil.which("uwsm-app") else []
    subprocess.Popen(prefix + command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def open_path(path: Path) -> None:
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def is_running(name: str) -> bool:
    return subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL).returncode == 0


def set_ini(path: Path, values: dict[str, dict[str, str]]) -> None:
    """Set keys in an INI-style file, keeping every other line untouched."""
    lines = path.read_text().splitlines() if path.exists() else []
    pending = {section: dict(keys) for section, keys in values.items()}
    output: list[str] = []
    section = None

    def flush(name: str | None) -> None:
        for key, value in pending.pop(name, {}).items():
            output.append(f"{key}={value}")

    for line in lines:
        header = re.fullmatch(r"\[(.+)\]", line.strip())
        if header:
            flush(section)
            section = header.group(1)
        elif section in pending and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in pending[section]:
                line = f"{key}={pending[section].pop(key)}"
        output.append(line)
    flush(section)
    for name in list(pending):
        output.append(f"[{name}]")
        flush(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(output) + "\n")
    os.replace(temporary, path)


def read_ini(path: Path, section: str, key: str) -> str | None:
    current = None
    for line in path.read_text().splitlines() if path.exists() else []:
        header = re.fullmatch(r"\[(.+)\]", line.strip())
        if header:
            current = header.group(1)
        elif current == section and line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip()
    return None


# --- OBS -------------------------------------------------------------------------

class OBSWebSocket:
    """Minimal obs-websocket v5 client (stdlib only)."""

    def __init__(self, port: int, password: str) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("websocket handshake refused")
        hello = self._recv()["d"]
        identify: dict = {"rpcVersion": 1, "eventSubscriptions": 0}
        if auth := hello.get("authentication"):
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest())
            identify["authentication"] = base64.b64encode(
                hashlib.sha256(secret + auth["challenge"].encode()).digest()).decode()
        self._send({"op": 1, "d": identify})
        if self._recv().get("op") != 2:
            raise ConnectionError("obs-websocket identification failed")
        self.counter = 0

    def _send(self, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("obs-websocket closed the connection")
            buf += chunk
        return buf

    def _recv(self) -> dict:
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise ConnectionError("obs-websocket closed the connection")
            if opcode == 0x1:
                return json.loads(data)

    def call(self, request: str, data: dict | None = None) -> dict:
        self.counter += 1
        rid = str(self.counter)
        message: dict = {"requestType": request, "requestId": rid}
        if data is not None:
            message["requestData"] = data
        self._send({"op": 6, "d": message})
        while True:
            response = self._recv()
            if response.get("op") == 7 and response["d"]["requestId"] == rid:
                status = response["d"]["requestStatus"]
                if not status["result"]:
                    raise RuntimeError(f"{request} failed: {status.get('comment')}")
                return response["d"].get("responseData", {})

    def close(self) -> None:
        self.sock.close()


def obs_connect() -> OBSWebSocket | None:
    try:
        config = json.loads(OBS_WEBSOCKET_CONFIG.read_text())
    except (OSError, ValueError):
        return None
    if not config.get("server_enabled"):
        return None
    try:
        return OBSWebSocket(config.get("server_port", 4455), config.get("server_password", ""))
    except (OSError, ConnectionError, ValueError):
        return None


def obs_profile_ini() -> Path | None:
    for name in ("user.ini", "global.ini"):
        profile = read_ini(OBS_CONFIG / name, "Basic", "ProfileDir")
        if profile:
            return OBS_CONFIG / "basic" / "profiles" / profile / "basic.ini"
    profiles = sorted((OBS_CONFIG / "basic" / "profiles").glob("*/basic.ini"))
    return profiles[0] if profiles else None


def hypr_focus(window_class: str) -> bool:
    try:
        clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout)
    except (OSError, ValueError):
        return False
    if not any(c.get("class") == window_class for c in clients):
        return False
    subprocess.run(["hyprctl", "dispatch", "focuswindow", f"class:^({re.escape(window_class)})$"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def open_obs(project: core.Project) -> str:
    footage = project.folder("Footage")
    footage.mkdir(parents=True, exist_ok=True)
    obs = obs_connect()
    if obs:
        try:
            if obs.call("GetRecordStatus").get("outputActive"):
                raise AppError("OBS is recording right now. Stop that recording first, then try again.")
            obs.call("SetRecordDirectory", {"recordDirectory": str(footage)})
        finally:
            obs.close()
        if hypr_focus("com.obsproject.Studio"):
            return "OBS was already open. New recordings now save to Media/Footage."
        return "OBS is running in the tray. New recordings now save to Media/Footage. Click its tray icon to show it."
    if is_running("obs"):
        raise AppError("OBS is open but its websocket server is off, so its save folder can't be changed. Close OBS and try again.")
    profile = obs_profile_ini()
    if profile:
        set_ini(profile, {"SimpleOutput": {"FilePath": str(footage)}, "AdvOut": {"RecFilePath": str(footage)}})
    launch(["obs"])
    return "OBS is starting. Recordings save to this project's Media/Footage folder."


# --- Audacity ----------------------------------------------------------------------

def open_audacity(project: core.Project) -> str:
    if is_running("audacity"):
        raise AppError("Audacity is already open. Save your work and quit Audacity, then open it from here so it saves into this project.")
    sessions = project.folder("Audacity")
    exports = project.folder("Voiceover Raw")
    sessions.mkdir(parents=True, exist_ok=True)
    exports.mkdir(parents=True, exist_ok=True)
    set_ini(AUDACITY_CFG, {
        "Directories/Save": {"Default": str(sessions)},
        "Directories/Open": {"Default": str(sessions)},
        "Directories/Import": {"Default": str(project.path)},
        "Directories/Export": {"Default": str(exports)},
        # The export dialog remembers its last folder here and ignores
        # Directories/Export while it is set.
        "ExportAudioDialog": {"DefaultPath": str(exports)},
    })
    latest = next((p for p in project.files("Audacity") if p.suffix == ".aup3"), None)
    launch(["env", "GDK_BACKEND=x11", "UBUNTU_MENUPROXY=0", "audacity", *([str(latest)] if latest else [])])
    if latest:
        return f"Opening {latest.name}. Exports save to Media/Voiceover/Raw."
    return "Audacity is starting. Save the project to Audacity/ and export to Media/Voiceover/Raw (both are preselected)."


# --- DaVinci Resolve ---------------------------------------------------------------
#
# The free Resolve can't be scripted from outside and sandboxes its own scripts,
# so the app only prepares things: the projects folder shows up in the Media
# page's Media Storage panel, and a Resolve project named like the video opens
# directly. Importing is one right-click on the project's Media folder.

def resolve_running() -> bool:
    # Anchored: an unanchored match would also hit any shell whose command line mentions the path.
    return subprocess.run(["pgrep", "-f", f"^{RESOLVE_BIN}"], stdout=subprocess.DEVNULL).returncode == 0


def resolve_project_exists(name: str) -> bool:
    """Whether the local disk database has a top-level project with this name."""
    return any(RESOLVE_PROJECTS.glob(f"*/Projects/{glob_escape(name)}/Project.db"))


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def reopen_on_launch(name: str) -> None:
    """Resolve's "Load last working project" preference, pointed at this project."""
    text = RESOLVE_USER_CONFIG.read_text()
    text = re.sub(r"<AutoReloadPrevProj>\w*</AutoReloadPrevProj>", "<AutoReloadPrevProj>true</AutoReloadPrevProj>", text)
    text = re.sub(r"<LastWorkingProject>[^<]*</LastWorkingProject>|<LastWorkingProject/>",
                  lambda _m: f"<LastWorkingProject>{xml_escape(name)}</LastWorkingProject>", text)
    text = re.sub(r"<LastWorkingProjectFolder>[^<]*</LastWorkingProjectFolder>", "<LastWorkingProjectFolder/>", text)
    temporary = RESOLVE_USER_CONFIG.with_name(".config.user.xml.tmp")
    temporary.write_text(text)
    os.replace(temporary, RESOLVE_USER_CONFIG)


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def storage_block(number: int, root: Path) -> str:
    return "\n".join((f"Site.1.FS.{number}.Type = IOFileSys", f"Site.1.FS.{number}.Root = {root}",
                      f"Site.1.FS.{number}.DIO = 0", f"Site.1.FS.{number}.BrightClip = 0"))


def resolve_storage_configured() -> bool:
    try:
        text = RESOLVE_CONFIG.read_text()
    except OSError:
        return False
    return f"Site.1.FS.1.Root = {RESOLVE_SCRATCH}\n" in text and f"Site.1.FS.2.Root = {core.PROJECTS}\n" in text


def configure_resolve_storage() -> bool:
    """Media Storage: 1 = hidden scratch folder, 2 = the projects folder.

    Resolve rewrites config.dat when it quits, so this only runs while it is
    closed. Scratch folders Resolve already made in the projects folder are
    moved along. Returns whether the setting is in place.
    """
    if resolve_storage_configured():
        return True
    if resolve_running() or not RESOLVE_CONFIG.exists():
        return False
    text = RESOLVE_CONFIG.read_text()
    blocks = f"Site.1.FS.Count = 2\n\n{storage_block(1, RESOLVE_SCRATCH)}\n{storage_block(2, core.PROJECTS)}\n"
    updated, count = re.subn(r"(?ms)^Site\.1\.FS\.Count = \d+\n.*?(?=^(?!Site\.1\.FS\.)\S)", blocks + "\n", text)
    if not count:
        return False
    backup = RESOLVE_CONFIG.with_name("config.dat.before-video-workflow")
    if not backup.exists():
        shutil.copy2(RESOLVE_CONFIG, backup)
    RESOLVE_SCRATCH.mkdir(parents=True, exist_ok=True)
    core.PROJECTS.mkdir(parents=True, exist_ok=True)
    for name in RESOLVE_SCRATCH_DIRS:
        old, new = core.PROJECTS / name, RESOLVE_SCRATCH / name
        if old.is_dir() and not new.exists():
            shutil.move(old, new)
    RESOLVE_CONFIG.write_text(updated)
    return True


def remove_resolve_sync_leftovers() -> None:
    """Earlier versions installed a sync script and a project template."""
    utility = RESOLVE_DATA / "Fusion" / "Scripts" / "Utility"
    for path in (utility / "Video Workflow Sync.py", utility / "Video Workflow Sync.lua",
                 HOME / ".local" / "share" / "video-workflow" / "resolve-template.drp"):
        path.unlink(missing_ok=True)
    shutil.rmtree(HOME / ".cache" / "video-workflow", ignore_errors=True)


def prepare_resolve() -> None:
    remove_resolve_sync_leftovers()
    configure_resolve_storage()


def open_resolve(project: core.Project) -> str:
    prepare_resolve()
    name = project.name
    if resolve_running():
        hypr_focus("resolve")
        return "Resolve is already open. Import this project's Media folder from the Media page."
    if not RESOLVE_BIN.exists():
        raise AppError(f"DaVinci Resolve isn't installed at {RESOLVE_BIN}.")
    if resolve_project_exists(name):
        reopen_on_launch(name)
        launch([str(RESOLVE_BIN)])
        return f"Resolve is opening your “{name}” project."
    launch([str(RESOLVE_BIN)])
    subprocess.run(["wl-copy", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return (f"Resolve is starting. In the Project Manager, create a new project named “{name}”. The name is on your "
            "clipboard, so just paste it. With that name, Open goes straight into the project from now on.")


# --- recso -------------------------------------------------------------------------
#
# recso's Automation API is off until it's turned on in recso (Settings ▸ Advanced ▸
# Automation API). It's JSON-RPC 2.0 over a Unix socket, one JSON object per line.
# Settings set over it last until recso quits, so Open sets the folders every time.

def recso_cli() -> list[str] | None:
    """The command that runs recso-cli: the deb's, or an AppImage's --cli."""
    if shutil.which("recso-cli"):
        return ["recso-cli"]
    app = os.environ.get("RECSO_APP")
    if app and Path(app).is_file():
        return [app, "--cli"]
    images = [p for p in RECSO_APPIMAGES.glob("*.AppImage") if p.name.lower().startswith("recso")]
    if images:
        return [str(max(images, key=lambda p: p.stat().st_mtime)), "--cli"]
    return None


def recso_socket() -> Path:
    if override := os.environ.get("RECSO_AUTOMATION_SOCKET"):
        return Path(override)
    if runtime := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(runtime) / "recso" / "api.sock"
    return Path(f"/tmp/recso-{getpass.getuser()}") / "api.sock"


def recso_call(method: str, params: dict | None = None) -> dict:
    """One request; raises OSError when recso isn't running or its API is off."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(recso_socket()))
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        sock.sendall(json.dumps(request).encode() + b"\n")
        buffer = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("recso closed the connection")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") != 1:
                    continue
                if "error" in message:
                    raise AppError(f"recso: {message['error']['message']}")
                return message["result"]


def recso_running() -> bool:
    return is_running("recso")


def open_recso(project: core.Project) -> str:
    cli = recso_cli()
    if cli is None:
        raise AppError("recso isn't installed: install its .deb, or put its AppImage in ~/Applications.")
    recordings = project.path / "Recso"
    footage = project.folder("Footage")
    recordings.mkdir(parents=True, exist_ok=True)
    footage.mkdir(parents=True, exist_ok=True)
    try:
        recso_call("api.version")
    except OSError:
        if recso_running():
            raise AppError(RECSO_API_OFF) from None
        # Starts recso with its window hidden and waits for the API. With the API
        # off, recso opens its window as usual and this gives up after the timeout.
        started = subprocess.run([*cli, "--launch", "--timeout", "20000", "status"],
                                 capture_output=True, text=True, check=False)
        if started.returncode == 3 and recso_running():
            raise AppError(RECSO_API_OFF) from None
        if started.returncode:
            raise AppError(started.stderr.strip() or "recso didn't start.") from None
    recso_call("settings.set", {"values": {"recording.outputDir": str(recordings),
                                           "export.outputDir": str(footage)}})
    recso_call("app.show")
    return "recso is open. Recordings save to Recso, and exports to Media/Footage."


# --- microphone ----------------------------------------------------------------------

def set_mic_gain(value: float) -> None:
    if not 1.0 <= value <= 1.8:
        raise AppError("Choose a gain between 1.0 and 1.8.")
    info = subprocess.run(["pactl", "info"], capture_output=True, text=True, check=True)
    if "Default Source: usb_voiceover_denoised" not in info.stdout:
        raise AppError("The noise-reduced USB mic isn't the default input. Reconnect it first.")
    subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{value:.1f}"], check=True)


def mic_gain() -> float | None:
    result = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], capture_output=True, text=True)
    match = re.search(r"Volume: ([\d.]+)", result.stdout)
    return float(match.group(1)) if match else None
