from lectureweaver.platform.cas import encrypt_password, input_value, parse_mfa_methods, classify_login_failure

# Structure of reAuthLoginView.do as served on 2026-10-22 (personal data removed).
MFA_TABS_2026 = """
<div class="reauth-tab-item reauth-tab-active" id="tab_8" onclick="handlePcTabClick(this)" data-type="8" data-name="微信扫码">
<div class="reauth-tab-item" id="tab_3" onclick="handlePcTabClick(this)" data-type="3" data-name="短信验证码">
<div class="reauth-tab-more-dropdown" id="moreDropdown" style="display: none;">
<a class="reauth-tab-more-item" href="javascript:;" id="10" title="安全令牌OTP" onclick="handleMoreItemClick(this)" data-name="安全令牌OTP">安全令牌OTP</a>
<a class="reauth-tab-more-item" href="javascript:;" id="4" title="企业微信验证码" onclick="handleMoreItemClick(this)" data-name="企业微信验证码">企业微信验证码</a>
"""

MFA_LEGACY = '<li class="changeReAuthTypes x" id="3">短信</li><li class="changeReAuthTypes" id="10">OTP</li>'


def test_parse_mfa_tabs_2026():
    # 8 (WeChat scan) cannot be done headless and is skipped.
    assert parse_mfa_methods(MFA_TABS_2026) == ["3", "10", "4"]


def test_parse_mfa_legacy():
    assert parse_mfa_methods(MFA_LEGACY) == ["3", "10"]


def test_input_value_by_id_or_name():
    html = '<input type="hidden" id="pwdEncryptSalt" value="Qa7WVrgbwxN0haVr" /><input type="hidden" name="execution" value="e1s1"/>'
    assert input_value(html, "pwdEncryptSalt") == "Qa7WVrgbwxN0haVr"
    assert input_value(html, "execution") == "e1s1"


def test_encrypt_password_roundtrip():
    import base64
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    salt = "Qa7WVrgbwxN0haVr"
    enc = encrypt_password("p@ss中文", salt)
    raw = base64.b64decode(enc)
    # The IV is random and not transmitted; the server ignores the first block
    # because the plaintext starts with 64 random chars. Decrypt with any IV
    # and check the tail.
    plain = AES.new(salt.encode(), AES.MODE_CBC, b"0" * 16).decrypt(raw)
    assert unpad(plain, 16)[64:].decode() == "p@ss中文"


def test_classify_failure():
    e = classify_login_failure('<span id="showErrorTip">您提供的用户名或者密码有误</span>')
    assert e.kind == "password"
