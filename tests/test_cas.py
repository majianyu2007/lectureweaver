"""The CAS protocol itself is tested in nwafu-cas; here only the adapter."""

import nwafu_cas
import pytest

from lectureweaver.platform.cas import AuthError, LoginRequired, PrompterUi, convert, cookies_from_json, cookies_to_json, has_trusted_device


def test_errors_become_lectureweaver_errors_with_hints():
    e = convert(nwafu_cas.InvalidCredentials("您提供的用户名或者密码有误", "检查密码"))
    assert isinstance(e, AuthError) and e.kind == "password" and "检查密码" in str(e)
    e = convert(nwafu_cas.LoginRequired("统一身份认证未登录或已失效"))
    assert isinstance(e, LoginRequired) and "lw auth login" in str(e)
    e = convert(nwafu_cas.SiteChanged("登录页无法识别"))
    assert "nwafu-cas" in str(e) and e.kind == "schema"


def test_cookie_roundtrip_keeps_trusted_device():
    jar = cookies_from_json([
        {"name": "MULTIFACTOR_BROWSER_FINGERPRINT", "value": "F", "domain": "authserver.nwafu.edu.cn", "path": "/authserver", "expires": None},
        {"name": "CASTGC", "value": "TGT", "domain": "authserver.nwafu.edu.cn", "path": "/authserver", "expires": 1},  # expired
    ])
    assert has_trusted_device(jar)
    assert [c["name"] for c in cookies_to_json(jar)] == ["MULTIFACTOR_BROWSER_FINGERPRINT"]


def test_prompter_ui_saves_and_announces_qr(monkeypatch, isolated_env):
    from lectureweaver.platform import cas

    opened, said = [], []
    monkeypatch.setattr(cas, "_open", opened.append)

    class P:
        def info(self, m):
            said.append(m)

    q = nwafu_cas.QrChallenge("mfa", b"\x89PNG\r\n\x1a\n...", "用钉钉、企业微信或 WeLink 扫码并确认")
    PrompterUi(P()).show_qr(q)
    assert opened and opened[0].read_bytes().startswith(b"\x89PNG") and "钉钉" in said[0]


@pytest.mark.parametrize("secret", [False, True])
def test_prompter_ui_ask_passes_secret(secret):
    class P:
        def ask(self, m, secret=False, default=""):
            return f" {m}:{secret} "

    assert PrompterUi(P()).ask("请输入短信验证码", secret=secret) == f"请输入短信验证码:{secret}"
