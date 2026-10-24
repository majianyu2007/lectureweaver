"""Cross-platform pieces: these run on macOS, Linux and Windows in CI."""

import os
import sys
import pytest


def test_state_dir_and_private_files(isolated_env):
    from lectureweaver.config import secrets_dir, state_dir

    assert state_dir() == isolated_env / "state"
    d = secrets_dir()
    assert d.is_dir()
    if os.name == "posix":
        assert (d.stat().st_mode & 0o777) == 0o700


def test_default_state_dir_per_os(monkeypatch):
    from lectureweaver import config

    monkeypatch.delenv("LW_STATE_DIR", raising=False)
    p = config._default_state_dir()
    if sys.platform == "darwin":
        assert p.parts[-3:] == ("Library", "Application Support", "LectureWeaver")
    elif os.name == "nt":
        assert p.name == "LectureWeaver"
    else:
        assert p.name == "lectureweaver"


def test_secret_fallback_roundtrip():
    from lectureweaver.secrets import delete_secret, get_secret, set_secret

    set_secret("nwafu:test", "p@ss中文")
    assert get_secret("nwafu:test") == "p@ss中文"
    assert delete_secret("nwafu:test")
    assert get_secret("nwafu:test") is None


def test_process_lock_is_exclusive(tmp_path):
    from lectureweaver.kb import try_lock

    first = try_lock(tmp_path / "x.lock")
    assert first is not None
    assert try_lock(tmp_path / "x.lock") is None
    first.close()
    again = try_lock(tmp_path / "x.lock")
    assert again is not None
    again.close()


def test_kb_paths_are_posix(kb):
    p = kb.root / "lectures" / "a" / "b.json"
    p.parent.mkdir(parents=True)
    p.write_text("{}", encoding="utf-8")
    assert kb.rel(p) == "lectures/a/b.json"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="headless Linux only")
def test_no_gui_gives_clear_message(monkeypatch):
    from lectureweaver import prompt

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(prompt.PromptUnavailable, match="终端"):
        prompt.dialog_prompter()


def test_ocr_none_backend_is_reported(monkeypatch, tmp_path):
    from PIL import Image

    from lectureweaver import ocr

    ocr.backend.cache_clear()
    monkeypatch.setenv("LW_OCR_BACKEND", "none")
    img = tmp_path / "a.png"
    Image.new("RGB", (40, 20), "white").save(img)
    with pytest.raises(ocr.OcrUnavailable):
        ocr.ocr_images([img])
    ocr.backend.cache_clear()


def test_cli_prints_utf8_when_piped():
    import subprocess

    r = subprocess.run([sys.executable, "-m", "lectureweaver.cli", "--help"], capture_output=True)
    assert r.returncode == 0
    assert "课堂" in r.stdout.decode("utf-8")


def _rapid_available():
    from lectureweaver.ocr import _rapid_engine

    return _rapid_engine() is not None


@pytest.mark.skipif(not _rapid_available(), reason='needs pip install "lectureweaver[ocr]"')
def test_rapidocr_reads_text(monkeypatch, tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from lectureweaver import ocr

    ocr.backend.cache_clear()
    monkeypatch.setenv("LW_OCR_BACKEND", "rapidocr")
    img = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(img).text((30, 40), "FileInputStream 2026", fill="black", font=ImageFont.load_default(size=56))
    p = tmp_path / "t.png"
    img.save(p)
    res = ocr.ocr_images([p])[str(p)]
    text = ocr.lines_text(res)
    assert "FileInputStream" in text.replace(" ", ""), text
    assert all(0 <= l["conf"] <= 1 and len(l["box"]) == 4 for l in res["lines"])
    ocr.backend.cache_clear()
