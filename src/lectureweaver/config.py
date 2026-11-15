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
import re
import shutil
import subprocess
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


def nb_cache_dir() -> Path:
    p = state_dir() / "nb_cache"
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
    soffice_bin: str = ""
    # Outbound proxy for Google / GitHub (campus *.nwafu.edu.cn stays direct).
    proxy: str = ""
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


def find_soffice(cfg: MachineConfig | None = None) -> str | None:
    """Locate LibreOffice ``soffice`` for headless PPTX -> PDF conversion."""
    cfg = cfg or MachineConfig.load()
    candidates: list[str] = []
    if cfg.soffice_bin:
        candidates.append(cfg.soffice_bin)
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if sys.platform == "darwin":
        candidates.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    elif os.name == "nt":
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(str(Path(base) / "LibreOffice" / "program" / "soffice.exe"))
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _system_proxy() -> str | None:
    """Best-effort detection of macOS / Windows system HTTP(S) proxy."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["scutil", "--proxy"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3, stdin=subprocess.DEVNULL,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        vals = dict(re.findall(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.+?)\s*$", out, re.M))
        for prefix in ("HTTPS", "HTTP"):
            if vals.get(f"{prefix}Enable") == "1" and vals.get(f"{prefix}Proxy") and vals.get(f"{prefix}Port"):
                return f"http://{vals[f'{prefix}Proxy']}:{vals[f'{prefix}Port']}"
        if vals.get("SOCKSEnable") == "1" and vals.get("SOCKSProxy") and vals.get("SOCKSPort"):
            return f"socks5://{vals['SOCKSProxy']}:{vals['SOCKSPort']}"
    elif os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
                enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
                server, _ = winreg.QueryValueEx(k, "ProxyServer")
                if enabled and server:
                    server = str(server).strip()
                    if "=" in server:
                        parts = dict(item.split("=", 1) for item in server.split(";") if "=" in item)
                        server = parts.get("https") or parts.get("http") or next(iter(parts.values()), "")
                    if server and "://" not in server:
                        server = f"http://{server}"
                    return server or None
        except OSError:
            return None
    return None


def outbound_proxy(cfg: MachineConfig | None = None) -> str | None:
    """Proxy URL for non-campus international endpoints (Google, GitHub, PyPI)."""
    if os.environ.get("LW_PROXY") is not None:
        v = os.environ["LW_PROXY"].strip()
        return None if v.lower() in ("", "none", "direct", "off") else v
    cfg = cfg or MachineConfig.load()
    if cfg.proxy:
        v = cfg.proxy.strip()
        if v.lower() in ("none", "direct", "off"):
            return None
        return v
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(k):
            return os.environ[k].strip()
    return _system_proxy()
