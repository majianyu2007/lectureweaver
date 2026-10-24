"""Locations and machine-local settings.

Two roots are kept apart on purpose:

* the knowledge base (``kb_dir``, default ``~/LectureWeaver`` on every OS): courses,
  transcripts, screenshots, note history, learning records. It is what gets
  backed up and migrated, so it never holds credentials.
* the machine state dir (macOS ``~/Library/Application Support/LectureWeaver``,
  Windows ``%LOCALAPPDATA%\\LectureWeaver``, Linux ``~/.local/state/lectureweaver``):
  this machine's paths (notes dir, model files), the session cookie jar and
  scratch space for media. Nothing here is needed to continue on another computer.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _default_state_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LectureWeaver"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "LectureWeaver"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "lectureweaver"


def state_dir() -> Path:
    p = Path(os.environ.get("LW_STATE_DIR") or _default_state_dir()).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def secrets_dir() -> Path:
    p = state_dir() / "secrets"
    p.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(p, 0o700)
    return p


def tmp_dir() -> Path:
    p = state_dir() / "tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tools_dir() -> Path:
    p = state_dir() / "tools"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class MachineConfig:
    """Settings that describe this computer, not the user's knowledge."""

    kb_dir: str = str(Path.home() / "LectureWeaver")
    # Where finished notes are written (an Obsidian vault folder or any dir).
    notes_dir: str = ""
    username: str = ""  # 学号; the password lives in the Keychain
    whisper_bin: str = "whisper-cli"
    whisper_model: str = ""
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    # Upper bound for scratch media on disk (MB); larger jobs stream instead.
    tmp_limit_mb: int = 2048
    extra: dict = field(default_factory=dict)

    @classmethod
    def path(cls) -> Path:
        return state_dir() / "config.json"

    @classmethod
    def load(cls) -> "MachineConfig":
        p = cls.path()
        data = {}
        if p.exists():
            data = json.loads(p.read_text("utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        if os.environ.get("LW_HOME"):
            cfg.kb_dir = os.environ["LW_HOME"]
        if os.environ.get("LW_NOTES_DIR"):
            cfg.notes_dir = os.environ["LW_NOTES_DIR"]
        return cfg

    def save(self) -> None:
        data = asdict(self)
        # Environment overrides are per-process; do not persist them.
        if os.environ.get("LW_HOME"):
            data.pop("kb_dir")
        if os.environ.get("LW_NOTES_DIR"):
            data.pop("notes_dir")
        old = {}
        if self.path().exists():
            old = json.loads(self.path().read_text("utf-8"))
        old.update(data)
        self.path().write_text(json.dumps(old, ensure_ascii=False, indent=2), "utf-8")

    @property
    def kb_path(self) -> Path:
        return Path(self.kb_dir).expanduser()

    @property
    def notes_path(self) -> Path | None:
        return Path(self.notes_dir).expanduser() if self.notes_dir else None
