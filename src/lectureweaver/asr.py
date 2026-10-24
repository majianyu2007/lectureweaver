"""Speech recognition backends.

* ``whisper.cpp`` (local, default): ``whisper-cli`` + a ggml model, Metal on
  Apple silicon, silero VAD to avoid hallucinated text during silence.
* ``openai-compatible`` (cloud, optional): any ``/v1/audio/transcriptions``
  endpoint (OpenAI, Groq, SiliconFlow …). Needs an API key in the Keychain
  and a recorded consent for the ``asr:<name>`` service; usage is logged and
  checked against a monthly minute budget.

Output: segments ``[{"t0": s, "t1": s, "text": str}]`` relative to the audio.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import MachineConfig

PROMPT_ZH = "以下是大学课堂的普通话录音，使用简体中文，带标点。"


class AsrError(Exception):
    pass


def default_model() -> Path | None:
    cfg = MachineConfig.load()
    if cfg.whisper_model and Path(cfg.whisper_model).expanduser().exists():
        return Path(cfg.whisper_model).expanduser()
    cache = Path.home() / ".cache" / "whisper"
    for name in ("ggml-large-v3-turbo-q5_0.bin", "ggml-large-v3-turbo.bin", "ggml-large-v3.bin", "ggml-medium.bin", "ggml-small.bin"):
        if (cache / name).exists():
            return cache / name
    return None


def vad_tool() -> str | None:
    """whisper.cpp's VAD helper: on PATH, or next to the configured whisper-cli."""
    found = shutil.which("whisper-vad-speech-segments")
    if found:
        return found
    wb = shutil.which(MachineConfig.load().whisper_bin)
    if wb:
        for name in ("whisper-vad-speech-segments", "whisper-vad-speech-segments.exe"):
            cand = Path(wb).with_name(name)
            if cand.exists():
                return str(cand)
    return None


def vad_model() -> Path | None:
    p = Path.home() / ".cache" / "whisper" / "ggml-silero-v5.1.2.bin"
    return p if p.exists() else None


def whisper_available() -> tuple[bool, str]:
    cfg = MachineConfig.load()
    if not shutil.which(cfg.whisper_bin):
        hint = "brew install whisper-cpp" if sys.platform == "darwin" else "从 github.com/ggml-org/whisper.cpp 安装，或用 lw init --whisper-bin 指定路径"
        return False, f"未找到 whisper-cli（{hint}）"
    m = default_model()
    if not m:
        return False, "未找到 whisper 模型（建议下载 ggml-large-v3-turbo-q5_0.bin 到 ~/.cache/whisper/）"
    return True, m.name


def vad_segments(wav: Path) -> list[tuple[float, float]]:
    """Speech regions (seconds) from whisper.cpp's silero VAD tool."""
    vm = vad_model()
    tool = vad_tool()
    if not vm or not tool:
        return []
    r = subprocess.run([tool, "-vm", str(vm), "-f", str(wav), "-np", "-vsd", "300", "-vp", "200"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = []
    for m in re.finditer(r"start = ([\d.]+), end = ([\d.]+)", r.stdout):
        out.append((float(m.group(1)) / 100, float(m.group(2)) / 100))
    return out


def plan_chunks(regions: list[tuple[float, float]], total: float, max_len: float = 28.0, max_gap: float = 1.2) -> list[tuple[float, float]]:
    """Merge speech regions into chunks that never contain removed silence > max_gap."""
    if not regions:
        # No VAD: fixed windows over the whole file.
        return [(t, min(t + max_len, total)) for t in _frange(0, total, max_len)]
    chunks: list[list[float]] = []
    for a, b in regions:
        a, b = max(0.0, a - 0.2), min(total, b + 0.2)
        while b - a > max_len:  # very long speech: hard split
            chunks.append([a, a + max_len])
            a += max_len
        if chunks and a - chunks[-1][1] <= max_gap and b - chunks[-1][0] <= max_len:
            chunks[-1][1] = b
        else:
            chunks.append([a, b])
    return [(a, b) for a, b in chunks if b - a >= 0.3]


def _frange(a, b, step):
    while a < b:
        yield a
        a += step


def transcribe_whisper(wav: Path, language: str = "zh", prompt: str = PROMPT_ZH, threads: int = 8) -> tuple[list[dict], str]:
    """VAD → speech chunks → one whisper run over all chunks → absolute times.

    Silence is removed only *between* chunks, so token times inside a chunk
    plus the chunk start are on the original timeline (unlike whisper's own
    --vad, whose token times drift by up to ~30 s; measured 2026-10-23).
    """
    import wave

    ok, info = whisper_available()
    if not ok:
        raise AsrError(info)
    cfg = MachineConfig.load()
    model = default_model()
    with wave.open(str(wav)) as w:
        rate, width, nch, n = w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes()
        pcm = w.readframes(n)
    total = n / rate
    regions = vad_segments(wav)
    if not regions and vad_model() and vad_tool():
        # VAD ran and found no speech: do not let whisper invent text from silence.
        return [], f"whisper.cpp:{model.stem.replace('ggml-', '')}"
    chunks = plan_chunks(regions, total)
    segs: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        files = []
        for i, (a, b) in enumerate(chunks):
            fp = Path(td) / f"c{i:05d}.wav"
            with wave.open(str(fp), "wb") as o:
                o.setnchannels(nch)
                o.setsampwidth(width)
                o.setframerate(rate)
                o.writeframes(pcm[int(a * rate) * width * nch : int(b * rate) * width * nch])
            files.append(fp)
        for i in range(0, len(files), 200):
            batch = files[i : i + 200]
            cmd = [cfg.whisper_bin, "-m", str(model), "-l", language, "-t", str(threads), "--prompt", prompt, "-ojf", "-np"]
            r = subprocess.run(cmd + [str(f) for f in batch], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise AsrError(f"whisper 失败：{r.stderr[-300:]}")
        for (a, _b), fp in zip(chunks, files):
            js = Path(str(fp) + ".json")
            if not js.exists():
                continue
            data = json.loads(js.read_text("utf-8", errors="replace"))
            for sg in split_sentences(data.get("transcription", [])):
                sg["t0"] = round(sg["t0"] + a, 2)
                sg["t1"] = round(sg["t1"] + a, 2)
                segs.append(sg)
    return drop_hallucinations(segs), f"whisper.cpp:{model.stem.replace('ggml-', '')}"


_SENT_END = set("。？！；?!;")


def split_sentences(transcription: list[dict], max_chars: int = 80) -> list[dict]:
    """Split whisper segments at sentence punctuation using real token times."""
    out: list[dict] = []
    for seg in transcription:
        tokens = [t for t in seg.get("tokens", []) if not (t.get("text", "").startswith("[_") or t.get("text", "").startswith("<|"))]
        if not tokens:
            text = (seg.get("text") or "").strip()
            if text:
                off = seg.get("offsets", {})
                out.append({"t0": off.get("from", 0) / 1000, "t1": off.get("to", 0) / 1000, "text": text})
            continue
        # With VAD, segment offsets are on the original timeline but token
        # offsets are not: anchor tokens to their segment and clamp.
        seg_from = seg.get("offsets", {}).get("from", 0) / 1000
        seg_to = seg.get("offsets", {}).get("to", 0) / 1000
        tok0 = tokens[0].get("offsets", {}).get("from", 0) / 1000

        def mapped(ms: int) -> float:
            return round(min(max(seg_from + ms / 1000 - tok0, seg_from), seg_to or 1e9), 2)

        buf, t0, t1 = "", None, None
        for tok in tokens:
            txt = tok.get("text", "")
            off = tok.get("offsets", {})
            if t0 is None:
                t0 = mapped(off.get("from", 0))
            t1 = mapped(off.get("to", 0))
            buf += txt
            if (txt and txt.strip()[-1:] in _SENT_END) or len(buf) >= max_chars:
                if buf.strip():
                    out.append({"t0": t0, "t1": t1, "text": buf.strip()})
                buf, t0 = "", None
        if buf.strip():
            out.append({"t0": t0 or 0, "t1": t1 or 0, "text": buf.strip()})
    return out


def transcribe_openai_compatible(wav: Path, provider: dict, api_key: str, language: str = "zh") -> tuple[list[dict], str]:
    """Upload audio to an OpenAI-compatible endpoint (only after consent)."""
    import httpx

    url = provider["base_url"].rstrip("/") + "/audio/transcriptions"
    model = provider.get("model", "whisper-1")
    with open(wav, "rb") as f:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": model, "language": language, "response_format": "verbose_json", "prompt": PROMPT_ZH},
            files={"file": (wav.name, f, "audio/wav")},
            timeout=1800,
        )
    if r.status_code >= 400:
        raise AsrError(f"云端识别失败 HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    segs = [{"t0": s["start"], "t1": s["end"], "text": s["text"].strip()} for s in j.get("segments", []) if s.get("text", "").strip()]
    if not segs and j.get("text"):
        segs = [{"t0": 0, "t1": j.get("duration", 0), "text": j["text"]}]
    return segs, f"{provider.get('name', 'cloud')}:{model}"


def wav_duration(wav: Path) -> float:
    import wave

    with wave.open(str(wav)) as w:
        return w.getnframes() / w.getframerate()


def now() -> float:
    return time.time()


_HALLU = ("字幕", "订阅", "点赞", "Amara", "感谢观看", "请不吝", "谢谢观看", "李宗盛")


def drop_hallucinations(segs: list[dict]) -> list[dict]:
    """Drop whisper's stock phrases and 3+ identical lines in a row."""
    out: list[dict] = []
    for s in segs:
        t = s["text"]
        if any(h in t for h in _HALLU) and len(t) < 30:
            continue
        if len(out) >= 2 and out[-1]["text"] == t and out[-2]["text"] == t:
            continue
        out.append(s)
    return out
