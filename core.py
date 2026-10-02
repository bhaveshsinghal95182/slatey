"""Video projects on disk: layout, media import and voiceover polishing.

Every video lives in one folder under PROJECTS:

    <Project>/
      Media/              everything to edit with; import this one folder into Resolve
        Footage/            OBS recordings
        Voiceover/          polished WAVs
        Voiceover/Raw/      Audacity exports
        Music/  Images/  Other/
      Audacity/           Audacity project files (.aup3)
      Renders/            final exports from Resolve
      .videoworkflow.json marks the folder as a project
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HOME = Path.home()
VIDEOS = HOME / "Videos"
PROJECTS = VIDEOS / "Projects"
STATE_FILE = HOME / ".config" / "slatey" / "state.json"
OLD_STATE_FILE = HOME / ".config" / "video-workflow" / "state.json"  # before the rename to Slatey
MARKER = ".videoworkflow.json"

MEDIA = "Media"

# Folder key -> path relative to the project, in the order the UI shows them.
FOLDERS = {
    "Footage": "Media/Footage",
    "Voiceover": "Media/Voiceover",
    "Voiceover Raw": "Media/Voiceover/Raw",
    "Music": "Media/Music",
    "Images": "Media/Images",
    "Other": "Media/Other",
    "Audacity": "Audacity",
    "Renders": "Renders",
}
# Where the first layout kept things: old path -> folder key.
OLD_LAYOUT = {
    "Footage": "Footage",
    "Voiceover/Final": "Voiceover",
    "Voiceover/Raw": "Voiceover Raw",
    "Music": "Music",
    "Images": "Images",
    "Other": "Other",
}
OLD_EMPTY_DIRS = ("Voiceover", "Resolve/Cache", "Resolve/Gallery", "Resolve/Media", "Resolve")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".mxf"}
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".opus", ".ogg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".svg"}
DEESS_PRESETS = {"Off": None, "Gentle": "0.22", "Standard": "0.38", "Strong": "0.58"}


@dataclass(frozen=True)
class Project:
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    def folder(self, key: str) -> Path:
        return self.path / FOLDERS[key]

    def files(self, key: str) -> list[Path]:
        folder = self.folder(key)
        if not folder.is_dir():
            return []
        items = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]
        return sorted(items, key=lambda p: p.stat().st_mtime, reverse=True)

    @property
    def created(self) -> datetime:
        try:
            return datetime.fromisoformat(json.loads((self.path / MARKER).read_text())["created"])
        except (OSError, ValueError, KeyError):
            return datetime.fromtimestamp(self.path.stat().st_mtime)

    def ensure_layout(self) -> None:
        self.migrate()
        for relative in FOLDERS.values():
            (self.path / relative).mkdir(parents=True, exist_ok=True)

    def migrate(self) -> None:
        """Move files from the first layout into Media/, then drop its empty folders."""
        for old, key in OLD_LAYOUT.items():
            source = self.path / old
            if not source.is_dir():
                continue
            target = self.folder(key)
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                if item.is_file():
                    shutil.move(item, unique_path(target / item.name))
            remove_if_empty(source)
        for old in OLD_EMPTY_DIRS:
            remove_if_empty(self.path / old)


def remove_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass  # missing or not empty


def clean_name(name: str) -> str:
    value = re.sub(r"[^\w .,&()'-]+", "-", name.strip(), flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" .-")[:80]
    if not value:
        raise ValueError("Enter a project name with letters or numbers.")
    return value


def create_project(name: str, root: Path = PROJECTS) -> Project:
    path = root / clean_name(name)
    if path.exists():
        raise FileExistsError(f"A project named “{path.name}” already exists.")
    path.mkdir(parents=True)
    (path / MARKER).write_text(json.dumps({"created": datetime.now().isoformat(timespec="seconds")}, indent=2))
    project = Project(path)
    project.ensure_layout()
    return project


def list_projects(root: Path = PROJECTS) -> list[Project]:
    if not root.is_dir():
        return []
    projects = [Project(p) for p in root.iterdir() if p.is_dir() and (p / MARKER).is_file()]
    return sorted(projects, key=lambda p: p.created, reverse=True)


# --- active project (remembered between launches) ----------------------------

def set_active_project(project: Project) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = read_state()
    state["active_project"] = str(project.path)
    state["projects_root"] = str(PROJECTS)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def read_state() -> dict:
    for path in (STATE_FILE, OLD_STATE_FILE):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return {}


# --- importing media ----------------------------------------------------------

def folder_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "Footage"
    if ext in IMAGE_EXTS:
        return "Images"
    if ext in AUDIO_EXTS:
        return "Music"
    if ext == ".aup3":
        return "Audacity"
    return "Other"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many copies of {path.name}")


def import_files(project: Project, sources: list[Path], *, move: bool = False) -> list[Path]:
    """Copy (or move) files into the project, sorted into folders by type."""
    placed: list[Path] = []
    for source in sources:
        source = Path(source).expanduser().resolve()
        if source.is_dir():
            children = sorted(p for p in source.rglob("*") if p.is_file() and not p.name.startswith("."))
            placed.extend(import_files(project, children, move=move))
            continue
        if not source.is_file():
            raise FileNotFoundError(f"{source} does not exist.")
        folder = project.folder(folder_for(source))
        folder.mkdir(parents=True, exist_ok=True)
        if source.is_relative_to(project.path.resolve()):
            placed.append(source)
            continue
        destination = unique_path(folder / source.name)
        if move:
            shutil.move(source, destination)
        else:
            shutil.copy2(source, destination)
        placed.append(destination)
    return placed


def loose_recordings(videos: Path = VIDEOS) -> list[Path]:
    """Video files sitting directly in ~/Videos, i.e. recorded outside any project."""
    if not videos.is_dir():
        return []
    return sorted(
        (p for p in videos.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )


# --- voiceover polishing -----------------------------------------------------

def audio_filters(preset: str = "Standard", loudness: int = -16) -> str:
    if preset not in DEESS_PRESETS:
        raise ValueError("Unknown de-essing preset")
    if not -24 <= loudness <= -12:
        raise ValueError("Loudness must be between -24 and -12 LUFS")
    filters = ["highpass=f=80"]
    intensity = DEESS_PRESETS[preset]
    if intensity:
        filters.extend((f"deesser=i={intensity}:m=0.55:f=0.5", "treble=g=-1.2:f=7000"))
    filters.extend((
        "acompressor=threshold=0.035:ratio=2.5:attack=12:release=120:makeup=1.4",
        f"loudnorm=I={loudness}:TP=-1.5:LRA=11",
    ))
    return ",".join(filters)


def polish_voiceover(source: Path, project: Project, preset: str = "Standard", loudness: int = -16) -> Path:
    """Write a de-essed, loudness-levelled mono 48 kHz/24-bit WAV into Voiceover/Final."""
    source = Path(source).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in AUDIO_EXTS:
        raise ValueError("Choose an existing audio file.")
    output_dir = project.folder("Voiceover")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = unique_path(output_dir / f"{source.stem}-polished.wav")
    fd, name = tempfile.mkstemp(suffix=".wav", prefix=".voiceover-", dir=output_dir)
    os.close(fd)
    temporary = Path(name)
    try:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-vn", "-af", audio_filters(preset, loudness), "-ac", "1", "-ar", "48000",
            "-c:a", "pcm_s24le", str(temporary),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Audio processing failed.")
        os.replace(temporary, output)
        return output
    finally:
        temporary.unlink(missing_ok=True)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
