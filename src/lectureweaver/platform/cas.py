"""NWAFU unified identity (Wisedu/金智 AuthServer, CAS).

A synchronous Python port of the flow verified in the user's own projects:
``wisedu_cas`` (AES password cipher, failure classification, TOTP) and
``nwafu-bksxk/lib/data/cas_client.dart`` (plain ``/authserver/login?service=``
entry, SMS / 企业微信 / TOTP second factor, the year-long trusted-device
cookie ``MULTIFACTOR_BROWSER_FINGERPRINT``).

1. GET ``/authserver/login?service=<S>``. A 302 means CASTGC is still valid.
2. POST the form with the AES-encrypted password.
3. Follow redirects. Without the trusted-device cookie the AuthServer stops at
   ``reAuthCheck/reAuthLoginView.do`` and wants a second factor; submitting it
   with ``skipTmpReAuth=true`` (信任此设备) sets that cookie.
4. The service URL receives ``?ticket=ST-…`` and the target sets its session.

The cookie jar is persisted (0600) so later runs reuse CASTGC and the
trusted-device mark instead of logging in again.
"""

from __future__ import annotations

import base64
import json
import re
import secrets as _rnd
import time
from dataclasses import dataclass
from http.cookiejar import Cookie
from typing import Callable
from urllib.parse import quote, urljoin, urlparse

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from ..prompt import Prompter

AUTH_SERVER = "https://authserver.nwafu.edu.cn"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)
TRUSTED_DEVICE_COOKIE = "MULTIFACTOR_BROWSER_FINGERPRINT"
_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


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


def _rand(n: int) -> str:
    return "".join(_rnd.choice(_AES_CHARS) for _ in range(n))


def encrypt_password(password: str, salt: str) -> str:
    """``encrypt.js``: base64(AES-CBC(key=salt, iv=rand16, rand64 + password))."""
    key = salt.strip()
    if not key:
        return password
    iv = _rand(16)
    cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
    return base64.b64encode(cipher.encrypt(pad((_rand(64) + password).encode(), 16))).decode()


def input_value(html: str, ident: str) -> str | None:
    m = re.search(rf"<input\b[^>]*\bid=[\"']{re.escape(ident)}[\"'][^>]*>", html, re.I)
    if not m:
        m = re.search(rf"<input\b[^>]*\bname=[\"']{re.escape(ident)}[\"'][^>]*>", html, re.I)
    if not m:
        return None
    v = re.search(r"\bvalue=[\"']([^\"']*)[\"']", m.group(0))
    return v.group(1) if v else ""


def classify_login_failure(html: str) -> AuthError:
    tip = ""
    for pat in (
        r'id="showErrorTip"[^>]*>([^<]+)<',
        r"id=[\"']formErrorTip[\"'][^>]*>.*?<span[^>]*>([^<]+)</span>",
    ):
        m = re.search(pat, html, re.S)
        if m:
            tip = m.group(1).strip()
            break
    if "锁定" in tip or "LOCK" in html:
        return AuthError("账号已被锁定", "到统一身份认证页面解锁后再试", "locked")
    if "密码" in tip or "用户名" in tip:
        return AuthError(tip or "用户名或密码错误", "检查学号和统一身份认证密码", "password")
    if "验证码" in tip or "频繁" in tip:
        return AuthError(tip or "需要安全验证", "失败次数过多时学校会要求滑块验证，请先在浏览器登录一次或稍后再试", "captcha")
    if "维护" in tip:
        return AuthError(tip, "", "maintenance")
    return AuthError(tip or "统一身份认证登录失败", "", "auth")


MFA_METHODS = {
    # code: (label, needs_send, send_type_name)
    "3": ("短信验证码", True, "reAuthDynamicCodeType"),
    "4": ("企业微信验证码", True, "reAuthWChatDynamicCodeType"),
    "10": ("安全令牌（TOTP）", False, None),
}


@dataclass
class MfaChallenge:
    service: str
    methods: list[str]
    user_id: str


def cookies_to_json(jar: httpx.Cookies) -> list[dict]:
    out = []
    for c in jar.jar:
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires,
                "secure": c.secure,
            }
        )
    return out


def cookies_from_json(items: list[dict]) -> httpx.Cookies:
    jar = httpx.Cookies()
    now = time.time()
    for it in items or []:
        if it.get("expires") and it["expires"] < now:
            continue
        domain = it.get("domain") or ""
        jar.jar.set_cookie(
            Cookie(
                version=0,
                name=it["name"],
                value=it["value"],
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=it.get("path") or "/",
                path_specified=True,
                secure=bool(it.get("secure")),
                expires=it.get("expires"),
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


def new_client(cookies: httpx.Cookies | None = None, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        cookies=cookies,
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(timeout, connect=12.0),
        follow_redirects=False,
    )


def _is_redirect(r: httpx.Response) -> bool:
    return r.status_code in (301, 302, 303, 307, 308) and bool(r.headers.get("location"))


_ALLOWED_HOST_SUFFIXES = (".nwafu.edu.cn", ".nwsuaf.edu.cn")


def _check_host(url: str) -> None:
    u = urlparse(url)
    host = u.hostname or ""
    if u.scheme not in ("https", "http") or not host.endswith(_ALLOWED_HOST_SUFFIXES):
        raise AuthError("统一身份认证返回了校外的跳转地址，已停止", url[:80], "redirect")


class CasClient:
    def __init__(self, client: httpx.Client):
        self.http = client

    @staticmethod
    def login_url(service: str) -> str:
        return f"{AUTH_SERVER}/authserver/login?service={quote(service, safe='')}"

    def follow(self, url: str, max_hops: int = 15) -> httpx.Response:
        """GET with manual redirects (cookies kept at every hop)."""
        current = url
        for _ in range(max_hops):
            _check_host(current)
            r = self.http.get(current)
            if not _is_redirect(r):
                return r
            current = urljoin(str(r.url), r.headers["location"])
        raise AuthError("统一身份认证重定向次数过多")

    def has_login(self, service: str) -> bool:
        r = self.http.get(self.login_url(service))
        return _is_redirect(r) and "/authserver/login" not in r.headers.get("location", "")

    def open_service(self, service: str) -> httpx.Response:
        """Reach ``service`` with the current CASTGC; raises LoginRequired."""
        r = self.http.get(self.login_url(service))
        if not _is_redirect(r):
            raise LoginRequired("统一身份认证登录已失效", "运行 lw auth login 重新登录", "expired")
        last = self.follow(urljoin(str(r.url), r.headers["location"]))
        url = str(last.url)
        if "/reAuthCheck/" in url:
            raise LoginRequired("学校要求二次验证", "运行 lw auth login 完成验证", "mfa")
        if "/authserver/login" in url:
            raise LoginRequired("统一身份认证登录已失效", "运行 lw auth login 重新登录", "expired")
        return last

    def ensure_fingerprint(self, fingerprint: str) -> None:
        """Register this install's browser fingerprint (``common-header.js``).

        The page computes md5(browser traits) and GETs ``/authserver/bfp/info``,
        which sets ``MULTIFACTOR_BROWSER_FINGERPRINT`` (30 days). 信任此设备 is
        recorded against that value, so it must exist before MFA is submitted.
        """
        if any(c.name == TRUSTED_DEVICE_COOKIE for c in self.http.cookies.jar):
            return
        try:
            self.http.get(f"{AUTH_SERVER}/authserver/bfp/info", params={"bfp": fingerprint, "_": int(time.time() * 1000)})
        except httpx.HTTPError:
            pass

    def login(
        self,
        service: str,
        username: str,
        password: str,
        prompter: Prompter | None = None,
        totp_secret: str | None = None,
        trust_device: bool = True,
    ) -> httpx.Response:
        page = self.http.get(self.login_url(service))
        if _is_redirect(page):
            return self._after_login(page, service, prompter, totp_secret, trust_device)
        html = page.text
        execution = input_value(html, "execution")
        salt = input_value(html, "pwdEncryptSalt")
        if not execution:
            raise AuthError("统一身份认证页面结构已变化", "无法读取登录表单", "schema")
        if not salt or len(salt) != 16:
            raise AuthError("统一身份认证加密参数缺失", "未发送密码", "schema")
        need_captcha = self.http.get(
            f"{AUTH_SERVER}/authserver/checkNeedCaptcha.htl",
            params={"username": username, "_": int(time.time() * 1000)},
        )
        try:
            if need_captcha.json().get("isNeed"):
                raise AuthError(
                    "学校要求图形/滑块验证码",
                    "请先在浏览器中成功登录一次统一身份认证，过一会儿再运行 lw auth login",
                    "captcha",
                )
        except (ValueError, AttributeError):
            pass
        r = self.http.post(
            self.login_url(service),
            data={
                "username": username,
                "password": encrypt_password(password, salt),
                "captcha": "",
                "rememberMe": "true",
                "_eventId": "submit",
                "cllt": "userNameLogin",
                "dllt": "generalLogin",
                "lt": "",
                "execution": execution,
            },
        )
        if not _is_redirect(r):
            raise classify_login_failure(r.text)
        return self._after_login(r, service, prompter, totp_secret, trust_device)

    # Called once the AuthServer has accepted the password (before any MFA).
    on_password_ok: "Callable[[], None] | None" = None

    def _after_login(self, r, service, prompter, totp_secret, trust_device) -> httpx.Response:
        last = self.follow(urljoin(str(r.url), r.headers["location"]))
        url = str(last.url)
        if "/authserver/login" not in url and self.on_password_ok:
            self.on_password_ok()
        if "/reAuthCheck/reAuthLoginView.do" in url:
            challenge = self._parse_mfa(last.text, service)
            return self._complete_mfa(challenge, prompter, totp_secret, trust_device)
        if "/authserver/login" in url:
            raise classify_login_failure(last.text)
        return last

    def _parse_mfa(self, html: str, service: str) -> MfaChallenge:
        methods = parse_mfa_methods(html)
        if not methods:
            from ..config import tmp_dir

            dump = tmp_dir() / "mfa_page.html"
            dump.write_text(html, "utf-8")
            raise AuthError("无法读取学校提供的二次验证方式", f"页面已保存到 {dump}", "mfa")
        uid = re.search(r'"reAuthUserId"\s*:\s*"([^"]*)"', html)
        return MfaChallenge(service=service, methods=methods, user_id=uid.group(1) if uid else "")

    def _select_method(self, c: MfaChallenge, code: str) -> None:
        r = self.http.post(
            f"{AUTH_SERVER}/authserver/reAuthCheck/changeReAuthType.do",
            data={"isMultifactor": "true", "reAuthType": code, "service": c.service},
        )
        j = _json(r)
        if str(j.get("code")) != "1":
            raise AuthError(j.get("message") or "无法切换二次验证方式", "", "mfa")

    def _complete_mfa(self, c: MfaChallenge, prompter: Prompter | None, totp_secret, trust_device) -> httpx.Response:
        if "10" in c.methods and totp_secret:
            import pyotp

            method, code = "10", pyotp.TOTP(totp_secret).now()
            self._select_method(c, method)
        else:
            if prompter is None:
                raise LoginRequired("学校要求二次验证", "运行 lw auth login 交互完成", "mfa")
            labels = [MFA_METHODS[m][0] for m in c.methods]
            method = c.methods[prompter.choose("学校要求二次验证（完成后本机将被信任，一段时间内不再询问）", labels)]
            label, needs_send, send_type = MFA_METHODS[method]
            self._select_method(c, method)
            if needs_send:
                r = self.http.post(
                    f"{AUTH_SERVER}/authserver/dynamicCode/getDynamicCodeByReauth.do",
                    data={"userName": c.user_id, "authCodeTypeName": send_type},
                )
                j = _json(r)
                msg = f"{j.get('returnMessage', '')}{j.get('mobile', '')}"
                if str(j.get("res")) not in ("success", "wechat_success", "cpdaily_success"):
                    raise AuthError(msg or "验证码发送失败", "稍后重试", "mfa")
                prompter.info(msg or "验证码已发送")
            code = prompter.ask(f"请输入{label}").strip()
        is_totp = method == "10"
        r = self.http.post(
            f"{AUTH_SERVER}/authserver/reAuthCheck/reAuthSubmit.do",
            data={
                "service": c.service,
                "reAuthType": method,
                "isMultifactor": "true",
                "password": "",
                "dynamicCode": "" if is_totp else code,
                "uuid": "",
                "answer1": "",
                "answer2": "",
                "otpCode": code if is_totp else "",
                "skipTmpReAuth": "true" if trust_device else "false",
            },
        )
        j = _json(r)
        if j.get("code") != "reAuth_success":
            raise AuthError(j.get("msg") or "二次验证码错误", "检查验证码后重试", "mfa")
        last = self.follow(self.login_url(c.service))
        if "/authserver/" in str(last.url):
            raise AuthError("二次验证后未能完成登录", "请重新运行 lw auth login", "mfa")
        return last

    def logout(self) -> None:
        try:
            self.http.get(f"{AUTH_SERVER}/authserver/logout")
        except httpx.HTTPError:
            pass


def parse_mfa_methods(html: str) -> list[str]:
    """Second-factor codes offered on ``reAuthLoginView.do``, in page order."""
    # Two layouts seen on authserver.nwafu.edu.cn:
    #  * 2026-10 tabs: <div class="reauth-tab-item" data-type="3"> plus a
    #    "more" dropdown <a class="reauth-tab-more-item" id="10">;
    #  * older: <… class="changeReAuthTypes" id="10">.
    methods: list[str] = []
    for tag in re.finditer(r"<[a-z]+\b[^>]*(?:reauth-tab-item|reauth-tab-more-item|changeReAuthTypes)[^>]*>", html, re.I):
        t = tag.group(0)
        m = re.search(r'\bdata-type=["\']([^"\']+)["\']', t) or re.search(r'\bid=["\']([^"\']+)["\']', t)
        if m and m.group(1) in MFA_METHODS and m.group(1) not in methods:
            methods.append(m.group(1))
    return methods


def _json(r: httpx.Response) -> dict:
    try:
        v = r.json()
        return v if isinstance(v, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


def has_trusted_device(jar: httpx.Cookies) -> bool:
    return any(c.name == TRUSTED_DEVICE_COOKIE for c in jar.jar)


RetryHook = Callable[[], bool]
