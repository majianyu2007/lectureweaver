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


def test_blocking_lock_waits_for_release(tmp_path):
    import threading
    import time

    from lectureweaver.kb import try_lock

    held = try_lock(tmp_path / "asr.lock")
    threading.Timer(0.5, held.close).start()
    t0 = time.time()
    got = try_lock(tmp_path / "asr.lock", wait=True)
    assert got is not None and time.time() - t0 >= 0.4
    got.close()


def test_process_skips_a_lecture_another_worker_holds(kb, capsys):
    from conftest import make_course, make_lecture
    from lectureweaver.cli import main
    from lectureweaver.commands import lecture_lock
    from lectureweaver.kb import try_lock

    lid = make_lecture(kb, make_course(kb))
    kb.x("DELETE FROM stages WHERE lecture_id=?", lid)
    kb.db.commit()
    held = try_lock(lecture_lock(kb, lid))
    try:
        main(["process", "-l", lid])
    finally:
        held.close()
    out = capsys.readouterr().out
    assert "正在处理这个课时，跳过" in out and lid in out
    assert kb.stage(lid, "details") is None  # nothing was run


def test_ylb_retries_a_stuck_server_and_fetches_week_by_week(monkeypatch):
    import json

    import httpx

    from lectureweaver.platform import ylb as Y

    monkeypatch.setattr(Y.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        body = json.loads(req.content or b"{}")
        calls.append(body.get("week"))
        if len(calls) == 1:
            raise httpx.ReadTimeout("stuck", request=req)
        if len(calls) == 2:
            return httpx.Response(500, json={"error": "Internal Server Error"})
        week = body["week"]
        items = [{"id": f"w{week}-{i}"} for i in range(2)] + ([{"id": "w1-0"}] if week == 2 else [])
        return httpx.Response(200, json={"videoInfoResourceList": items, "total": len(items)})

    y = Y.YlbClient(httpx.Client(transport=httpx.MockTransport(handler)), Y.YlbToken(user_id="u"))
    recs = y.all_recordings("c1", "2026-2027", 1, weeks=[1, 2])
    assert [r["id"] for r in recs] == ["w1-0", "w1-1", "w2-0", "w2-1"]  # deduplicated across weeks
    assert calls == [1, 1, 1, 2]


def test_platform_error_is_reported_without_traceback(monkeypatch, capsys):
    from lectureweaver import commands
    from lectureweaver.cli import main
    from lectureweaver.platform.ylb import PlatformError

    def boom(args):
        raise PlatformError("云课堂网络错误：ReadTimeout")

    monkeypatch.setattr(commands, "cmd_sync", boom)
    assert main(["sync"]) == 4
    assert "稍后重试" in capsys.readouterr().err
