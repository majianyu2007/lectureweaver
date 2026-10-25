"""Pick screenshots that carry teaching content, fetching as little as possible.

Strategy per recording (all numbers configurable, defaults tuned on the
2026-10 recordings: VGA has an IDR keyframe every 1 s, ~50 KB each):

1. Read the MP4 sample tables (moov, ~2 MB) - see :mod:`mp4index`.
2. Coarse pass: decode one keyframe every ``coarse_s`` seconds (default 10).
   Only those keyframes' bytes are downloaded (byte ranges).
3. Refine: where two neighbouring samples differ, bisect between them down
   to one keyframe, so each change is located to ~1 s.
4. Keep the *last* frame before every change (the most complete state of a
   slide / code listing), plus checkpoints during long gradual edits, and
   drop blank frames and near-duplicates (a slide shown twice is kept once,
   with every time it appeared).

The screen recording (VGA) is the main source. The teacher camera is only
sampled where the screen stays unchanged for a long time while the
transcript shows teaching continues (blackboard work); the student camera
is never used.
"""

from __future__ import annotations

import concurrent.futures
import io
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageStat

from .media import REFERER, UA, MediaError, ffmpeg_bin
from .mp4index import Movie, Track, avcc_to_annexb_headers, parse_movie, sample_to_annexb

SIG_W, SIG_H = 192, 108


class RangeReader:
    """Byte-range access to one remote file, metering what is transferred."""

    def __init__(self, url: str, client: httpx.Client | None = None):
        self.url = url
        self._own = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"Referer": REFERER, "User-Agent": UA},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        )
        self.bytes = 0
        self.requests = 0
        self._lock = threading.Lock()
        self._size: int | None = None

    def size(self) -> int:
        if self._size is None:
            for attempt in range(4):
                try:
                    r = self.client.head(self.url)
                    break
                except httpx.HTTPError:
                    if attempt == 3:
                        raise
                    time.sleep(2 ** attempt)
            if r.status_code >= 400:
                raise MediaError(f"无法访问录像文件（HTTP {r.status_code}）")
            self._size = int(r.headers["content-length"])
        return self._size

    def read(self, off: int, length: int, attempts: int = 5) -> bytes:
        # The campus link drops long transfers now and then (e.g. a stream
        # ending 2 MB short of 730 MB); retry with backoff.
        for attempt in range(attempts):
            try:
                r = self.client.get(self.url, headers={"Range": f"bytes={off}-{off + length - 1}"})
                if r.status_code != 206:
                    raise MediaError(f"服务器不支持分段读取（HTTP {r.status_code}）")
                with self._lock:
                    self.bytes += len(r.content)
                    self.requests += 1
                if len(r.content) < length and off + len(r.content) < self.size():
                    raise httpx.ReadError(f"short read {len(r.content)}/{length}")
                return r.content
            except httpx.HTTPError:
                if attempt == attempts - 1:
                    raise
                time.sleep(min(2 ** attempt, 15))
        raise AssertionError

    def close(self) -> None:
        if self._own:
            self.client.close()


@dataclass
class Frame:
    t: float  # seconds into the recording
    img: Image.Image | None = None
    sig: Image.Image | None = None
    key_index: int = 0

    def release(self) -> None:
        self.img = None


@dataclass
class KeptFrame:
    t: float
    path: Path
    stream: str
    reason: str
    also_at: list[float] = field(default_factory=list)
    width: int = 0
    height: int = 0


def signature(img: Image.Image) -> Image.Image:
    return img.convert("L").resize((SIG_W, SIG_H), Image.BILINEAR)


def diff_fraction(a: Image.Image, b: Image.Image, thr: int = 28) -> float:
    h = ImageChops.difference(a, b).histogram()
    return sum(h[thr:]) / (SIG_W * SIG_H)


def is_blank(sig: Image.Image) -> bool:
    st = ImageStat.Stat(sig)
    return st.stddev[0] < 4.0


class KeyframeSource:
    """Decodes chosen keyframes of one video track from byte ranges."""

    def __init__(self, reader: RangeReader, movie: Movie, track: Track, workers: int = 6):
        self.reader = reader
        self.movie = movie
        self.track = track
        self.keys = track.keyframes()
        self.key_times = [track.time_of(i) for i in self.keys]
        self.headers, self.nal_len = avcc_to_annexb_headers(track.avcc)
        self.workers = workers
        self.cache: dict[int, Frame] = {}

    def nearest(self, t: float) -> int:
        """Index into self.keys of the keyframe at or just before t."""
        import bisect

        i = bisect.bisect_right(self.key_times, t) - 1
        return max(0, min(i, len(self.keys) - 1))

    def get(self, key_idxs: list[int]) -> list[Frame]:
        need = sorted({k for k in key_idxs if k not in self.cache})
        if need:
            def fetch(k):
                si = self.keys[k]
                return k, self.reader.read(self.track.sample_offsets[si], self.track.sample_sizes[si])

            with concurrent.futures.ThreadPoolExecutor(self.workers) as ex:
                samples = dict(ex.map(fetch, need))
            imgs = self._decode([samples[k] for k in need])
            if len(imgs) != len(need):
                # Decoder disagreed about frame boundaries: fall back to one by one.
                imgs = []
                for k in need:
                    one = self._decode([samples[k]])
                    imgs.append(one[0] if one else None)
            for k, img in zip(need, imgs):
                f = Frame(t=self.key_times[k], key_index=k)
                if img is not None:
                    f.img = img
                    f.sig = signature(img)
                self.cache[k] = f
        return [self.cache[k] for k in key_idxs]

    def _decode(self, samples: list[bytes]) -> list[Image.Image]:
        stream = b"".join(self.headers + sample_to_annexb(s, self.nal_len) for s in samples)
        w, h = self.track.width, self.track.height
        r = subprocess.run(
            [ffmpeg_bin(), "-v", "error", "-f", "h264", "-i", "pipe:0", "-fps_mode", "passthrough",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            input=stream, capture_output=True, timeout=600,
        )
        fs = w * h * 3
        out = []
        data = r.stdout
        for i in range(len(data) // fs):
            out.append(Image.frombytes("RGB", (w, h), data[i * fs : (i + 1) * fs]))
        return out


@dataclass
class VisualParams:
    coarse_s: float = 8.0
    change_thr: float = 0.004  # fraction of signature pixels that changed: "something changed"
    major_thr: float = 0.06  # a different screen (slide flip, window switch, scroll), not an edit
    checkpoint_s: float = 120.0  # keep intermediate states of long edits on one screen
    stable_probe_s: float = 2.0  # one-sample screens must still look the same this much later
    dedupe_thr: float = 0.003
    max_frames: int = 60


@dataclass
class Screen:
    frames: list  # sampled Frames belonging to this screen (time order)
    keep: list  # Frames chosen to represent it

    @property
    def first_t(self) -> float:
        return self.frames[0].t

    @property
    def last_t(self) -> float:
        return self.frames[-1].t


def select_frames(src: KeyframeSource, p: VisualParams, extra_times: list[float] | None = None, log=None) -> list[Frame]:
    """Representative frames (decoded), time order. See module docstring."""
    if not src.keys:
        return []
    duration = src.key_times[-1]
    times = [i * p.coarse_s for i in range(int(duration // p.coarse_s) + 1)] + [duration]
    times += [t for t in (extra_times or []) if 0 <= t <= duration]
    idxs = sorted({src.nearest(t) for t in times})
    samples = [f for f in src.get(idxs) if f.sig is not None]
    if log:
        log(f"  粗采样 {len(samples)} 帧")

    # 1. split into screens at major changes
    screens: list[list[Frame]] = []
    for f in samples:
        if screens and diff_fraction(screens[-1][-1].sig, f.sig) <= p.major_thr:
            screens[-1].append(f)
        else:
            screens.append([f])

    # 2. one-sample screens: keep only if stable (not a zoom/scroll animation)
    probes = []
    for sc in screens:
        if len(sc) == 1:
            probes.append(src.nearest(sc[0].t + p.stable_probe_s))
    probe_frames = {f.key_index: f for f in src.get(sorted(set(probes)))} if probes else {}

    kept: list[tuple[Frame, float, list[float]]] = []  # frame, dwell seconds, times shown
    for sc in screens:
        if all(is_blank(f.sig) for f in sc):
            continue
        if len(sc) == 1:
            f = sc[0]
            pf = probe_frames.get(src.nearest(f.t + p.stable_probe_s))
            if pf is None or pf.key_index == f.key_index or pf.sig is None or diff_fraction(f.sig, pf.sig) > p.change_thr:
                continue
            kept.append((pf, p.stable_probe_s, [f.t]))
            continue
        # same screen, possibly edited: last state + checkpoints
        anchor = sc[0]
        for i, f in enumerate(sc):
            last = i == len(sc) - 1
            if last:
                if not is_blank(f.sig):
                    kept.append((f, f.t - sc[0].t + p.coarse_s, [sc[0].t]))
                break
            nxt = sc[i + 1]
            edited_since_anchor = diff_fraction(anchor.sig, nxt.sig) > p.change_thr
            if edited_since_anchor and nxt.t - anchor.t >= p.checkpoint_s and not is_blank(f.sig):
                kept.append((f, f.t - anchor.t, [anchor.t]))
                anchor = nxt
    if log:
        log(f"  画面 {len(screens)} 个，保留候选 {len(kept)}")

    # 3. cap: keep the longest-shown screens, then restore time order
    if len(kept) > p.max_frames:
        kept = sorted(kept, key=lambda x: -x[1])[: p.max_frames]
    kept.sort(key=lambda x: x[0].t)
    out = []
    for f, dwell, shown in kept:
        f.dwell = dwell
        f.first_seen = min(shown)
        out.append(f)
    return out


def dedupe(frames: list[Frame], thr: float) -> list[tuple[Frame, list[float]]]:
    out: list[tuple[Frame, list[float]]] = []
    for f in frames:
        hit = None
        for j, (g, also) in enumerate(out):
            if diff_fraction(f.sig, g.sig) <= thr:
                hit = j
                break
        if hit is None:
            out.append((f, []))
        else:
            g, also = out[hit]
            # keep the later (more complete) image, remember when it was shown
            out[hit] = (f, also + [g.t])
    out.sort(key=lambda x: x[0].t)
    return out


def save_webp(img: Image.Image, path: Path, quality: int = 82) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "WEBP", quality=quality, method=6)


def open_track(reader: RangeReader, handler: str = "vide") -> tuple[Movie, Track]:
    movie = parse_movie(reader.read, reader.size())
    tr = movie.track(handler)
    if tr is None:
        raise MediaError(f"录像中没有 {handler} 轨道")
    return movie, tr


def image_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    b = io.BytesIO()
    img.save(b, fmt)
    return b.getvalue()
