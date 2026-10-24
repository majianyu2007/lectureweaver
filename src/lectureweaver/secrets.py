"""Credential storage, kept out of the knowledge base, notes, logs and backups.

Passwords and API keys go to the operating system's credential store through
``keyring``: macOS Keychain, Windows Credential Manager, or the Linux Secret
Service (GNOME Keyring / KWallet). On macOS the entry is the same generic
password item (service ``LectureWeaver``) that earlier versions wrote with
the ``security`` tool, so nothing has to be re-entered.

Where no credential store is usable (e.g. a headless Linux box) a JSON file
in the machine state dir is used, readable only by the current user.

Session cookies and platform tokens are short-lived and large, so they always
live in that private state dir (``secrets/session.json``), never in the
knowledge base.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import secrets_dir

SERVICE = "LectureWeaver"


def _keyring():
    """The usable keyring module, or None (disabled, missing or no backend)."""
    if os.environ.get("LW_NO_KEYCHAIN"):
        return None
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        kr = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - broken backend configuration
        return None
    if isinstance(kr, fail.Keyring) or type(kr).__name__ in ("ChainerBackend",) and not getattr(kr, "backends", None):
        return None
    return keyring


def backend_name() -> str:
    kr = _keyring()
    if kr is None:
        return f"本机私有文件（{_fallback_file()}）"
    name = type(kr.get_keyring()).__module__
    if "macOS" in name or sys.platform == "darwin":
        return "macOS 钥匙串"
    if "Windows" in name:
        return "Windows 凭据管理器"
    return "系统密钥环（Secret Service）"


def _fallback_file() -> Path:
    return secrets_dir() / "keystore.json"


def _read_fallback() -> dict[str, str]:
    p = _fallback_file()
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read (POSIX 0600; on Windows
    the per-user profile directory already restricts access)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name == "posix":
        os.chmod(path, 0o600)


def set_secret(account: str, value: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, account, value)
            return
        except Exception:  # noqa: BLE001 - locked/unavailable store: fall back
            pass
    data = _read_fallback()
    data[account] = value
    write_private(_fallback_file(), json.dumps(data))


def get_secret(account: str) -> str | None:
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(SERVICE, account)
            if v is not None:
                return v
        except Exception:  # noqa: BLE001
            pass
    return _read_fallback().get(account)


def delete_secret(account: str) -> bool:
    found = False
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, account)
            found = True
        except Exception:  # noqa: BLE001 - not present
            pass
    data = _read_fallback()
    if data.pop(account, None) is not None:
        found = True
        write_private(_fallback_file(), json.dumps(data))
    return found


class SessionStore:
    """The cookie jar + platform tokens, one private JSON file."""

    def __init__(self, name: str = "session.json"):
        self.path = secrets_dir() / name

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except json.JSONDecodeError:
            return {}

    def save(self, data: dict[str, Any]) -> None:
        write_private(self.path, json.dumps(data, ensure_ascii=False))

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def redact(text: str, *secrets: str | None) -> str:
    """Remove known secret values from a message before it is shown/logged."""
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text
