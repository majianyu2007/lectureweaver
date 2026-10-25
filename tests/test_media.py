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
