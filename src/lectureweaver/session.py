"""One place that owns the school session: cookies, CAS login, ylb token.

Session reuse order, cheapest first:
1. saved ylb token (signed requests work until it expires),
2. ylb refresh-token endpoint,
3. CAS with the saved CASTGC cookie (no password),
4. CAS with the Keychain password (MFA skipped while the trusted-device
   cookie is valid),
5. otherwise: ask the user to run ``lw auth login``.
"""

from __future__ import annotations

import time
import uuid


from .config import MachineConfig
from .platform import cas as cas_mod
from .platform.cas import AuthError, CasClient, LoginRequired
from .platform.ehall import EhallTimetable
from .platform.ylb import SSO_SERVICE, YlbClient, YlbToken, token_from_json, token_to_json
from .prompt import Prompter
from .secrets import SessionStore, get_secret, set_secret, delete_secret


def password_account(username: str) -> str:
    return f"nwafu:{username}"


class Session:
    def __init__(self, cfg: MachineConfig | None = None):
        self.cfg = cfg or MachineConfig.load()
        self.store = SessionStore()
        data = self.store.load()
        self.http = cas_mod.new_client(cas_mod.cookies_from_json(data.get("cookies", [])))
        self.cas = CasClient(self.http)
        self.ylb_token: YlbToken | None = token_from_json(data.get("ylb"))
        # Stable per-install browser fingerprint; see CasClient.ensure_fingerprint.
        self.fingerprint: str = data.get("fingerprint") or uuid.uuid4().hex.upper()
        self._ylb: YlbClient | None = None
        self._ehall: EhallTimetable | None = None

    # ---------------------------------------------------------------- persistence
    def save(self) -> None:
        self.store.save(
            {
                "cookies": cas_mod.cookies_to_json(self.http.cookies),
                "ylb": token_to_json(self.ylb_token) if self.ylb_token else None,
                "saved_at": time.time(),
                "fingerprint": self.fingerprint,
                "username": self.cfg.username,
            }
        )

    def close(self) -> None:
        self.save()
        self.http.close()

    # ---------------------------------------------------------------- login
    def interactive_login(
        self,
        username: str,
        password: str | None,
        prompter: Prompter,
        save_password: bool = True,
        totp_secret: str | None = None,
    ) -> YlbToken:
        if password is None:
            password = get_secret(password_account(username)) or prompter.ask(f"统一身份认证密码（{username}）", secret=True)
        self.cas.ensure_fingerprint(self.fingerprint)
        if save_password:
            self.cas.on_password_ok = lambda: set_secret(password_account(username), password)
        self.cfg.username = username
        self.cfg.save()
        try:
            last = self.cas.login(SSO_SERVICE, username, password, prompter=prompter, totp_secret=totp_secret)
        finally:
            self.cas.on_password_ok = None
            self.save()
        token = YlbClient.token_from_service_response(self.http, last)
        self.ylb_token = token
        self.cfg.username = username
        self.cfg.save()
        if save_password:
            set_secret(password_account(username), password)
        self._ylb = None
        self.save()
        return token

    def _silent_ylb_login(self) -> YlbToken:
        try:
            last = self.cas.open_service(SSO_SERVICE)
        except LoginRequired:
            pw = get_secret(password_account(self.cfg.username)) if self.cfg.username else None
            if not pw:
                raise
            self.cas.ensure_fingerprint(self.fingerprint)
            # No prompter: if MFA is demanded this raises LoginRequired("mfa").
            last = self.cas.login(SSO_SERVICE, self.cfg.username, pw, prompter=None)
        token = YlbClient.token_from_service_response(self.http, last)
        self.ylb_token = token
        self.save()
        return token

    def ylb(self) -> YlbClient:
        if self._ylb is None:
            token = self.ylb_token or self._silent_ylb_login()
            self._ylb = YlbClient(self.http, token, relogin=self._relogin_and_store)
        return self._ylb

    def _relogin_and_store(self) -> YlbToken:
        t = self._silent_ylb_login()
        return t

    def ehall(self) -> EhallTimetable:
        if self._ehall is None:
            self._ehall = EhallTimetable(self.cas)
        return self._ehall

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        jar = self.http.cookies
        names = {c.name for c in jar.jar}
        st = {
            "username": self.cfg.username or None,
            "password_in_keychain": bool(self.cfg.username and get_secret(password_account(self.cfg.username))),
            "castgc_cookie": "CASTGC" in names,
            "trusted_device_cookie": cas_mod.has_trusted_device(jar),
            "ylb_token_age_min": round((time.time() - self.ylb_token.obtained_at) / 60, 1) if self.ylb_token else None,
        }
        return st

    def logout(self, forget_password: bool = False) -> None:
        self.cas.logout()
        if forget_password and self.cfg.username:
            delete_secret(password_account(self.cfg.username))
        self.ylb_token = None
        # Keep only the trusted-device marker so the next login skips MFA.
        keep = [c for c in cas_mod.cookies_to_json(self.http.cookies) if c["name"] == cas_mod.TRUSTED_DEVICE_COOKIE]
        self.http.cookies = cas_mod.cookies_from_json(keep)
        self.save()


__all__ = ["Session", "AuthError", "LoginRequired"]
