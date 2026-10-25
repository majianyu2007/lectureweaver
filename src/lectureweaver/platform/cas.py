"""NWAFU unified identity (authserver.nwafu.edu.cn) for LectureWeaver.

All protocol work is done by the ``nwafu-cas`` library: password and 扫码
login, every second-factor method the school offers (SMS, 企业微信, 今日校园,
e-mail, 钉钉, WeLink, 公众号 codes, 安全令牌 OTP, 认证密码, 微信扫码,
钉钉/企微/WeLink 扫码, 实人认证), 信任此设备 and site-change detection.

This module only adapts it to LectureWeaver: Chinese errors with a hint for
the ``lw`` commands, and the prompter used for dialogs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Sequence

import nwafu_cas
from nwafu_cas import cookies as _cookies
from nwafu_cas.constants import TRUSTED_DEVICE_COOKIE, UA  # noqa: F401  (re-exported)

from ..prompt import Prompter

TRUSTED_DEVICE_COOKIE = TRUSTED_DEVICE_COOKIE


class AuthError(Exception):
    """A login problem with a user-facing Chinese message and a hint."""

    def __init__(self, message: str, hint: str = "", kind: str = "auth"):
        super().__init__(message)
        self.hint = hint
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.args[0]}{'（' + self.hint + '）' if self.hint else ''}"


class LoginRequired(AuthError):
    pass


def convert(e: nwafu_cas.CasError) -> AuthError:
    cls = LoginRequired if isinstance(e, nwafu_cas.LoginRequired) else AuthError
    hint = e.hint
    if e.kind == "schema":
        hint = "学校的统一身份认证页面可能改版了；升级 nwafu-cas（运行 nwafu-cas doctor 查看）"
    elif isinstance(e, nwafu_cas.LoginRequired) and not hint:
        hint = "运行 lw auth login 重新登录"
    return cls(e.message, hint, e.kind)


class CasClient(nwafu_cas.CasClient):
    """nwafu-cas client whose errors are LectureWeaver's :class:`AuthError`."""

    def open_service(self, service: str = nwafu_cas.DEFAULT_SERVICE):
        try:
            return super().open_service(service)
        except nwafu_cas.CasError as e:
            raise convert(e) from e


cookies_to_json = _cookies.to_json
cookies_from_json = _cookies.from_json


def has_trusted_device(jar) -> bool:
    return _cookies.has(jar, TRUSTED_DEVICE_COOKIE)


class PrompterUi:
    """Second-factor dialogue through a LectureWeaver prompter (terminal or system dialogs)."""

    def __init__(self, prompter: Prompter):
        self.p = prompter

    def choose(self, message: str, options: Sequence[str]) -> int:
        return self.p.choose(message, list(options))

    def ask(self, message: str, secret: bool = False) -> str:
        return self.p.ask(message, secret=secret).strip()

    def info(self, message: str) -> None:
        self.p.info(message)

    def show_qr(self, qr: nwafu_cas.QrChallenge) -> None:
        from ..config import tmp_dir

        path = tmp_dir() / f"login-qr.{qr.image_type}"
        path.write_bytes(qr.image)
        _open(path)
        self.p.info(f"请{qr.hint}（二维码已在图片查看器中打开：{path}）")


def _open(path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
