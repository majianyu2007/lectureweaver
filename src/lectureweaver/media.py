"""Remote media access without downloading whole recordings.

The school media servers (``lzmNN.nwafu.edu.cn``) serve plain MP4 with HTTP
Range support and only require a ``Referer`` from the platform (verified
2026-10-23: 403 without it, 206 with it). Everything here works on byte
ranges:

* :class:`RangeProxy` - a loopback HTTP proxy that ffmpeg/ffprobe talk to. It
  adds the Referer, forwards Range requests and counts the bytes really
  fetched from the school, so each job can report its true network cost.
  (It also sidesteps name resolution problems in some ffmpeg builds.)
* :func:`probe` / :func:`grab_frame` - metadata and single frames via seeks.
* :func:`fetch_audio_track` - pulls only the audio samples out of the MP4
  (using its sample tables), typically ~10x less traffic than the whole file.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

REFERER = "https://ylb.nwafu.edu.cn/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Safari/537.36"


class MediaError(Exception):
    pass


class TransferMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.bytes = 0
        self.requests = 0

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes += n

    def request(self) -> None:
        with self._lock:
            self.requests += 1


class _Handler(http.server.BaseHTTPRequestHandler):
    server: "_Server"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence
        pass

    def _upstream(self) -> str | None:
        key = self.path.split("/", 2)[-1].split("?")[0].split(".")[0]
        return self.server.routes.get(key)

    def do_HEAD(self):
        self._forward(head=True)

    def do_GET(self):
        self._forward(head=False)

    def _forward(self, head: bool) -> None:
        url = self._upstream()
        if not url:
            self.send_error(404)
            return
        headers = {"Referer": REFERER, "User-Agent": UA}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        meter = self.server.meter
        meter.request()
        try:
            with self.server.client.stream("HEAD" if head else "GET", url, headers=headers) as r:
                self.send_response(r.status_code)
                for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                    if h in r.headers:
                        self.send_header(h, r.headers[h])
                self.end_headers()
                if head:
                    return
                for chunk in r.iter_raw(64 * 1024):
                    meter.add(len(chunk))
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        # ffmpeg seeks by dropping the connection; stop pulling.
                        return
        except httpx.HTTPError:
            try:
                self.send_error(502)
            except Exception:  # noqa: BLE001
                pass


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RangeProxy:
    """``with RangeProxy() as p: url = p.local(remote)`` then hand url to ffmpeg."""

    def __init__(self) -> None:
        self.meter = TransferMeter()
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.routes = {}
        self._srv.meter = self.meter
        self._srv.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> "RangeProxy":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._srv.client.close()

    def local(self, remote_url: str) -> str:
        key = uuid.uuid4().hex
        self._srv.routes[key] = remote_url
        host, port = self._srv.server_address
        ext = Path(remote_url.split("?")[0]).suffix or ".mp4"
        return f"http://{host}:{port}/m/{key}{ext}"


# ---------------------------------------------------------------- ffmpeg helpers
def ffmpeg_install_hint() -> str:
    import sys

    if sys.platform == "darwin":
        return "brew install ffmpeg"
    if sys.platform == "win32":
        return "winget install Gyan.FFmpeg（或用 lw init --ffmpeg 指定路径）"
    return "sudo apt install ffmpeg（或发行版对应的包；也可用 lw init --ffmpeg 指定路径）"


def ffmpeg_bin() -> str:
    from .config import MachineConfig

    b = MachineConfig.load().ffmpeg_bin
    if not shutil.which(b):
        raise MediaError(f"未找到 ffmpeg：{ffmpeg_install_hint()}")
    return b


def ffprobe_bin() -> str:
    from .config import MachineConfig

    b = MachineConfig.load().ffprobe_bin
    if not shutil.which(b):
        raise MediaError(f"未找到 ffprobe：{ffmpeg_install_hint()}")
    return b


@dataclass
class ProbeInfo:
    duration: float
    has_audio: bool
    video: dict
    audio: dict | None


def probe(local_url: str) -> ProbeInfo:
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,width,height,r_frame_rate,bit_rate,sample_rate,channels:format=duration,bit_rate",
         "-of", "json", local_url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if r.returncode != 0:
        raise MediaError(f"ffprobe 失败：{r.stderr.strip()[:200]}")
    j = json.loads(r.stdout)
    streams = j.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return ProbeInfo(float(j.get("format", {}).get("duration") or 0), a is not None, v, a)


def grab_frame(local_url: str, t: float, out_path: Path, max_width: int = 1920) -> bool:
    """Decode the frame at ``t`` seconds (input seek: only the GOP is fetched)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(), "-v", "error", "-y", "-ss", f"{max(t, 0):.2f}", "-i", local_url,
        "-frames:v", "1", "-vf", f"scale='min({max_width},iw)':-2", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def stream_audio_full(local_url: str, out_wav: Path) -> None:
    """Fallback: decode audio while streaming the whole file (nothing kept but the wav)."""
    cmd = [ffmpeg_bin(), "-v", "error", "-y", "-i", local_url, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise MediaError(f"音频提取失败：{r.stderr.strip()[:200]}")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


def disk_usage(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def now() -> float:
    return time.time()
