"""Audio for speech recognition, without transferring the whole recording.

The AAC samples of a recording are listed in its sample tables. On the
teacher-camera files they sit in ~1 s chunks (≈2 700 runs, ~21 MB of a
~730 MB file), so we fetch just those byte ranges, wrap each AAC frame in an
ADTS header and let ffmpeg decode the result to 16 kHz mono WAV. If the
container is unusual we fall back to streaming the whole file through ffmpeg
(the transfer is reported honestly either way). Only the small WAV touches
the disk and it is deleted after recognition.
"""

from __future__ import annotations

import concurrent.futures
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .media import MediaError, ffmpeg_bin
from .mp4index import adts_header, coalesce, parse_movie
from .visual import RangeReader


@dataclass
class AudioPlan:
    url: str
    runs: int
    bytes: int
    duration: float


def plan_audio(reader: RangeReader) -> tuple[AudioPlan, object, object]:
    movie = parse_movie(reader.read, reader.size())
    tr = movie.track("soun")
    if tr is None or not tr.asc:
        raise MediaError("录像中没有可用的 AAC 音轨")
    runs = coalesce(list(zip(tr.sample_offsets, tr.sample_sizes)), max_gap=0)
    return AudioPlan(reader.url, len(runs), sum(s for _, s in runs), tr.duration / tr.timescale), movie, tr


def _samples_in(track, t0: float, t1: float) -> list[tuple[int, int]]:
    ts = track.timescale
    out = []
    for off, size, dt in zip(track.sample_offsets, track.sample_sizes, track.sample_times):
        t = dt / ts
        if t0 <= t < t1:
            out.append((off, size))
    return out


def _decode_adts(asc, channels, chunks: list[bytes], out_wav: Path) -> None:
    adts = bytearray()
    for c in chunks:
        adts += adts_header(asc, len(c), channels) + c
    r = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-y", "-f", "aac", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)],
        input=bytes(adts), capture_output=True, timeout=1800,
    )
    if r.returncode != 0 or not out_wav.exists():
        raise MediaError(f"音频解码失败：{r.stderr.decode(errors='ignore')[-200:]}")


def _read_samples(reader: RangeReader, samples: list[tuple[int, int]], max_gap: int, workers: int = 8) -> list[bytes]:
    """Fetch samples (offset, size) in file order, merging close ranges."""
    import bisect

    runs = coalesce(samples, max_gap=max_gap)
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        blobs = list(ex.map(lambda r: reader.read(r[0], r[1]), runs))
    starts = [r[0] for r in runs]
    out = []
    for off, size in sorted(samples):
        i = bisect.bisect_right(starts, off) - 1
        base = starts[i]
        out.append(blobs[i][off - base : off - base + size])
    return out


def speech_seconds(wav: Path) -> float:
    from .asr import vad_segments

    segs = vad_segments(wav)
    if segs:
        return sum(b - a for a, b in segs)
    # no VAD available: fall back to "loud enough" frames
    import array
    import math
    import wave

    with wave.open(str(wav)) as w:
        pcm = array.array("h", w.readframes(w.getnframes()))
        rate = w.getframerate()
    step = rate // 10  # 0.1 s of 16-bit mono samples
    loud = 0
    for i in range(0, len(pcm) - step, step):
        chunk = pcm[i : i + step]
        if math.sqrt(sum(x * x for x in chunk) / step) > 500:
            loud += 1
    return loud * 0.1


@dataclass
class AudioChoice:
    reader: RangeReader
    track: object
    name: str
    speech_s: float
    est_bytes: int
    max_gap: int
    notes: list


def choose_audio_source(candidates: list[tuple[str, RangeReader]], work: Path, windows: int = 3, win_s: float = 15.0) -> AudioChoice | None:
    """Pick the stream that actually carries the lecture audio.

    Which camera records the teacher's microphone differs between rooms and
    days (one room, one day: teacher stream ~11 kbps of silence, screen
    recording 129 kbps of speech), so every stream is sampled: three short
    excerpts are fetched by byte range, decoded and measured with VAD.
    """
    scored = []
    net_errors = []
    for name, rd in candidates:
        try:
            movie = parse_movie(rd.read, rd.size())
        except httpx.HTTPError as e:
            net_errors.append(f"{name}: {e}")
            continue
        except Exception:  # noqa: BLE001
            continue
        tr = movie.track("soun")
        if tr is None or not tr.asc or not tr.sample_sizes:
            continue
        dur = tr.duration / tr.timescale
        runs = len(coalesce(list(zip(tr.sample_offsets, tr.sample_sizes)), max_gap=0))
        interleaved = runs > 12000
        max_gap = 512 * 1024 if interleaved else 0
        excerpts = []
        for i in range(1, windows + 1):
            t0 = dur * i / (windows + 1)
            excerpts += _samples_in(tr, t0, t0 + win_s)
        try:
            wav = work / f"probe_{name}.wav"
            _decode_adts(tr.asc, tr.channels, _read_samples(rd, excerpts, max_gap), wav)
            sp = speech_seconds(wav)
        except httpx.HTTPError as e:
            net_errors.append(f"{name}: {e}")
            continue
        except Exception:  # noqa: BLE001
            continue
        if interleaved:
            est = rd.size()  # audio interleaved with every video frame: ~whole file
        else:
            est = sum(tr.sample_sizes)
        scored.append(AudioChoice(rd, tr, name, sp, est, max_gap, [f"{name}: 抽样 {windows}×{win_s:.0f}s 检出语音 {sp:.0f}s，音频码率 {sum(tr.sample_sizes) * 8 / dur / 1000:.0f}kbps"]))
    if not scored:
        if net_errors:
            # A network problem, not an unusual file: streaming the whole
            # recording instead would only fail more slowly. Re-run later.
            raise MediaError("读取录像时网络中断，稍后重跑 lw process 即可续上（" + "；".join(net_errors)[:200] + "）")
        return None
    best_speech = max(c.speech_s for c in scored)
    # streams within 80% of the best speech amount are equivalent; then cheapest
    good = [c for c in scored if c.speech_s >= best_speech * 0.8]
    pick = min(good, key=lambda c: c.est_bytes)
    pick.notes = [n for c in scored for n in c.notes]
    if best_speech < 3:
        pick.notes.append("所有视频路的抽样中都几乎没有语音")
    return pick


def fetch_audio_wav(reader: RangeReader, track, out_wav: Path, workers: int = 8, max_gap: int = 0) -> None:
    samples = sorted(zip(track.sample_offsets, track.sample_sizes))
    _decode_adts(track.asc, track.channels, _read_samples(reader, samples, max_gap, workers), out_wav)
