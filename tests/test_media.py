"""MP4 sample-table access, keyframe selection and audio-only extraction on a
synthetic recording built like the school's (moov at the end, 1 s GOP, AAC)."""

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from lectureweaver.mp4index import parse_movie
from lectureweaver.visual import KeyframeSource, VisualParams, dedupe, select_frames

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")


class LocalReader:
    """Same interface as visual.RangeReader, over a local file; counts bytes."""

    def __init__(self, path: Path):
        self.path, self.url, self.bytes, self.requests = path, str(path), 0, 0

    def size(self):
        return self.path.stat().st_size

    def read(self, off, length):
        with open(self.path, "rb") as f:
            f.seek(off)
            b = f.read(length)
        self.bytes += len(b)
        self.requests += 1
        return b

    def close(self):
        pass


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    d = tmp_path_factory.mktemp("rec")
    out = d / "rec.mp4"
    # four "slides" of 10 s each, a keyframe every second, AAC audio track
    # each "slide" has a block of "text" at a different place
    slides = [("white", 40), ("lightblue", 120), ("lightyellow", 200), ("white", 280)]
    inputs = []
    for c, y in slides:
        inputs += ["-f", "lavfi", "-t", "10", "-i", f"color=c={c}:s=640x360:r=10,drawbox=x=60:y={y}:w=480:h=40:color=black:t=fill"]
    filt = "".join(f"[{i}:v]" for i in range(4)) + "concat=n=4:v=1:a=0,drawbox=x=40:y=40:w=200:h=60:color=red@1:t=fill:enable='gte(t,20)'[v]"
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-f", "lavfi", "-t", "40", "-i", "sine=frequency=440:sample_rate=48000",
           "-filter_complex", filt, "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-g", "10", "-keyint_min", "10", "-sc_threshold", "0",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", str(out)]
    subprocess.run(cmd, check=True)
    return out


def test_moov_at_end_and_tables(recording):
    rd = LocalReader(recording)
    mv = parse_movie(rd.read, rd.size())
    assert mv.moov_offset > 1000  # like the platform files: moov after mdat
    v, a = mv.track("vide"), mv.track("soun")
    assert v.codec == "avc1" and (v.width, v.height) == (640, 360)
    assert len(v.keyframes()) == 40
    assert a.codec == "mp4a" and a.sample_rate == 48000 and a.asc
    # reading the index costs about the size of the index, not the file
    assert rd.bytes < mv.moov_size + 5000


def test_keyframe_selection_fetches_little(recording):
    rd = LocalReader(recording)
    mv = parse_movie(rd.read, rd.size())
    src = KeyframeSource(rd, mv, mv.track("vide"), workers=2)
    frames = select_frames(src, VisualParams(coarse_s=4.0))
    kept = dedupe(frames, 0.003)
    # four distinct screens; the last one gets an extra red box at t=20..,
    # which belongs to the third/fourth screen as an edit, not a new screen
    assert 3 <= len(kept) <= 5, [f.t for f, _ in kept]
    assert rd.bytes < recording.stat().st_size * 0.8


def test_audio_only_extraction(recording, tmp_path):
    from lectureweaver.audio import fetch_audio_wav

    rd = LocalReader(recording)
    mv = parse_movie(rd.read, rd.size())
    before = rd.bytes
    wav = tmp_path / "a.wav"
    fetch_audio_wav(rd, mv.track("soun"), wav, workers=2)
    with wave.open(str(wav)) as w:
        dur = w.getnframes() / w.getframerate()
        assert w.getframerate() == 16000 and w.getnchannels() == 1
    assert abs(dur - 40) < 0.5
    audio_bytes = sum(mv.track("soun").sample_sizes)
    assert rd.bytes - before == audio_bytes  # exactly the audio samples, nothing else


def test_audio_slots_parallel_and_sequential_files():
    from lectureweaver.pipeline import audio_slots

    # two screen-recording files over the same 45 min (two projector inputs): one slot
    det = {"teacherViewFiles": [{"id": "t", "startTime": "2026-10-22 10:09:00"}],
           "vgaViewFiles": [{"id": "v1", "startTime": "2026-10-22 10:09:00"}, {"id": "v2", "startTime": "2026-10-22 10:09:00"}]}
    slots = audio_slots(det)
    assert len(slots) == 1 and [(n, f["id"]) for n, f in slots[0][1]] == [("teacher", "t"), ("vga", "v1")]
    # a recording split in time: both halves are recognised
    det = {"teacherViewFiles": [{"id": "a", "startTime": "2026-10-22 10:09:00"}, {"id": "b", "startTime": "2026-10-22 10:31:00"}]}
    slots = audio_slots(det)
    assert [[f["id"] for _, f in c] for _, c in slots] == [["a"], ["b"]]
    assert (slots[1][0] - slots[0][0]).total_seconds() == 22 * 60


def test_screen_files_prefers_main_device():
    from lectureweaver.pipeline import screen_files

    files = [{"id": "aux", "recordDeviceName": "辅屏"}, {"id": "main", "recordDeviceName": "主屏"}]
    main, aux = screen_files(files)
    assert main["id"] == "main" and [f["id"] for f in aux] == ["aux"]


def test_audio_slots_use_main_screen_when_named():
    from lectureweaver.pipeline import audio_slots

    det = {"vgaViewFiles": [
        {"id": "aux", "recordDeviceName": "辅屏", "startTime": "2026-10-22 10:09:00"},
        {"id": "main", "recordDeviceName": "主屏", "startTime": "2026-10-22 10:09:00"},
    ]}
    assert [[f["id"] for _, f in c] for _, c in audio_slots(det)] == [["main"]]


def test_dedupe_keeps_the_first_visit_of_a_returning_page():
    from PIL import Image

    from lectureweaver.visual import Frame, dedupe, signature

    a, b = signature(Image.new("RGB", (32, 18), "black")), signature(Image.new("RGB", (32, 18), "white"))
    first = Frame(t=1.0, sig=a, page=0, first_seen=1.0, visits=[1.0])
    other = Frame(t=5.0, sig=b, page=1, first_seen=5.0, visits=[5.0])
    again = Frame(t=9.0, sig=a, page=2, first_seen=9.0, visits=[9.0])
    out = dedupe([first, other, again], 0.003)
    assert len(out) == 2
    kept, visits = next(x for x in out if x[0].sig is a)
    assert kept.page == 0 and kept.first_seen == 1.0 and visits == [1.0, 9.0]


def test_break_changed_only_keeps_new_screens():
    from PIL import Image

    from lectureweaver.visual import break_changed, signature

    dark = signature(Image.new("RGB", (64, 36), "black"))
    same = signature(Image.new("RGB", (64, 36), "black"))
    other = signature(Image.new("RGB", (64, 36), "white"))
    assert break_changed(dark, None, 0.06)  # first frame after a jump is kept
    assert not break_changed(same, dark, 0.06)
    assert break_changed(other, dark, 0.06)


def test_break_windows_follow_bell_schedule():
    from datetime import datetime

    from lectureweaver.pipeline import break_windows, in_windows

    lec = {"date": "2026-10-20", "start_period": 3, "end_period": 4}
    windows = break_windows(lec)
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in windows] == [("10:55", "11:05")]
    assert in_windows(datetime(2026, 10, 20, 10, 59), windows)
    assert not in_windows(datetime(2026, 10, 20, 11, 5), windows)
    evening = break_windows({"date": "2026-10-20", "start_period": 9, "end_period": 10})
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in evening] == [("19:45", "19:50")]


def test_visual_uses_main_screen_and_drops_repeated_break_frames(kb, tmp_path, monkeypatch):
    """Aux is compared against the main screen; a static break screen is kept once."""
    import json
    import subprocess
    from pathlib import Path

    from lectureweaver.visual import VisualParams

    def make(name, *sections):
        inputs = []
        for i, (color, seconds) in enumerate(sections):
            # a black bar keeps solid colours from counting as blank
            src = f"color=c={color}:s=640x360:r=10,drawbox=x={40 + i * 80}:y=60:w=220:h=50:color=black:t=fill"
            inputs += ["-f", "lavfi", "-t", str(seconds), "-i", src]
        filt = "".join(f"[{i}:v]" for i in range(len(sections))) + f"concat=n={len(sections)}:v=1:a=0[v]"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", filt, "-map", "[v]",
             "-c:v", "libx264", "-g", "10", "-keyint_min", "10", "-pix_fmt", "yuv420p", str(tmp_path / name)], check=True,
        )
        return tmp_path / name

    # class slide, a different screen held through the break, then the class slide returns
    main = make("main.mp4", ("lightblue", 30), ("lightyellow", 10), ("lightblue", 20))
    aux = make("aux.mp4", ("darkgreen", 60))
    black = make("black.mp4", ("black", 60))
    from conftest import make_course, make_lecture

    cid = make_course(kb)
    lid = make_lecture(kb, cid, date="2026-10-20", segs=((3, "10:54:30", "10:55:30"),))
    kb.x("UPDATE lectures SET end_period=4 WHERE id=?", lid)
    rec = kb.q1("SELECT id FROM recordings WHERE lecture_id=?", lid)
    det = {"vgaViewFiles": [
        {"id": "black", "recordDeviceName": "辅屏", "startTime": "2026-10-20 10:54:30", "endTime": "2026-10-20 10:55:30", "videoStorePath": str(black)},
        {"id": "aux", "recordDeviceName": "辅屏", "startTime": "2026-10-20 10:54:30", "endTime": "2026-10-20 10:55:30", "videoStorePath": str(aux)},
        {"id": "main", "recordDeviceName": "主屏", "startTime": "2026-10-20 10:54:30", "endTime": "2026-10-20 10:55:30", "videoStorePath": str(main)},
    ]}
    from lectureweaver.kb import dumps

    kb.x("UPDATE recordings SET detail=? WHERE id=?", dumps(det), rec["id"])
    kb.db.commit()

    from lectureweaver import pipeline

    monkeypatch.setattr("lectureweaver.visual.RangeReader", lambda url: LocalReader(Path(url)))

    lec = kb.q1("SELECT * FROM lectures WHERE id=?", lid)
    p = pipeline.Pipeline(kb, visual_params=VisualParams(coarse_s=4.0))
    res = p.stage_visual(lec, tmp_path / "work")
    frames = json.loads((kb.abs(lec["dir"]) / "frames.json").read_text("utf-8"))["frames"]

    assert res.status == "done"
    assert all(f["stream"] in ("vga", "vga_aux") for f in frames)
    assert sum(1 for f in frames if f["stream"] == "vga_aux") == 1
    assert frames and all("副屏" in f["reason"] for f in frames if f["stream"] == "vga_aux")
    # the returning class slide is one page, cited at its first visit
    blue = next(f for f in frames if f["stream"] == "vga" and f["page"].endswith("000"))
    assert blue["clock"] == "10:54:30" and blue["also_at"]
