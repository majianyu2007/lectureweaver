"""Local OCR, with one backend per platform. Nothing leaves the machine.

Backends (chosen automatically; override with ``LW_OCR_BACKEND``):
* ``vision``    - Apple Vision via a small Swift helper (``native/lw_ocr.swift``),
                  compiled on first use. macOS only; best quality there.
* ``rapidocr``  - RapidOCR (PaddleOCR models on ONNX Runtime), any OS.
                  ``pip install "lectureweaver[ocr]"``.
* ``tesseract`` - the ``tesseract`` CLI with the ``chi_sim`` language pack.
* ``none``      - no OCR; screenshots are still saved, the packet says so.

All backends return the same shape per image::

    {"file": str, "w": int, "h": int,
     "lines": [{"text": str, "conf": float 0..1, "box": [x, y, w, h]}]}

with boxes normalised to the image, origin top-left. Confidence is kept so
later steps can drop noise (e.g. tiny IDE chrome text).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from .config import tools_dir

SRC = Path(__file__).parent / "native" / "lw_ocr.swift"


class OcrUnavailable(Exception):
    pass


# ------------------------------------------------------------------ backend choice
def _rapid_engine():
    try:
        from rapidocr import RapidOCR  # rapidocr >= 2
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR  # rapidocr-onnxruntime 1.x
        except ImportError:
            return None
    return RapidOCR


def _tesseract_ok() -> bool:
    exe = shutil.which("tesseract")
    if not exe:
        return False
    r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "chi_sim" in (r.stdout + r.stderr)


@lru_cache(maxsize=1)
def backend() -> str:
    forced = os.environ.get("LW_OCR_BACKEND")
    if forced:
        return forced
    if sys.platform == "darwin" and shutil.which("swiftc"):
        return "vision"
    if _rapid_engine() is not None:
        return "rapidocr"
    if _tesseract_ok():
        return "tesseract"
    return "none"


def backend_description() -> str:
    return {
        "vision": "Apple Vision（本机）",
        "rapidocr": "RapidOCR（本机，ONNX）",
        "tesseract": "Tesseract（本机，chi_sim）",
        "none": "未安装 OCR",
    }.get(backend(), backend())


def install_hint() -> str:
    if sys.platform == "darwin":
        return "xcode-select --install（提供 swiftc，用于 Apple Vision OCR）"
    return 'pip install "lectureweaver[ocr]"（RapidOCR），或安装 tesseract 及 chi_sim 语言包'


# ------------------------------------------------------------------ Apple Vision
def ocr_binary() -> Path:
    out = tools_dir() / "lw_ocr"
    if out.exists() and out.stat().st_mtime >= SRC.stat().st_mtime:
        return out
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise OcrUnavailable("需要 Xcode 命令行工具（xcode-select --install）才能使用 Apple Vision OCR")
    r = subprocess.run([swiftc, "-O", str(SRC), "-o", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise OcrUnavailable(f"编译 OCR 工具失败：{r.stderr[-300:]}")
    return out


def _vision(paths: list[Path], fast: bool, correction: bool) -> dict[str, dict]:
    cmd = [str(ocr_binary())] + (["--fast"] if fast else []) + ([] if correction else ["--no-correction"])
    out: dict[str, dict] = {}
    for i in range(0, len(paths), 32):
        batch = [str(p) for p in paths[i : i + 32]]
        r = subprocess.run(cmd + batch, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        for line in r.stdout.splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            out[j["file"]] = j
    return out


# ------------------------------------------------------------------ RapidOCR
@lru_cache(maxsize=1)
def _rapid():
    cls = _rapid_engine()
    if cls is None:
        raise OcrUnavailable('未安装 RapidOCR：pip install "lectureweaver[ocr]"')
    return cls()


def _rapid_image(img) -> dict:
    """OCR one PIL image with RapidOCR (1.x or 2.x API)."""
    import numpy as np

    w, h = img.size
    res = _rapid()(np.array(img.convert("RGB")))
    items = []
    if isinstance(res, tuple):  # 1.x: (list of [box, text, score], elapse)
        for box, text, score in res[0] or []:
            items.append((box, text, score))
    else:  # 2.x: object with boxes / txts / scores
        boxes = getattr(res, "boxes", None)
        if boxes is not None:
            items = list(zip(boxes, res.txts, res.scores))
    lines = []
    for box, text, score in items:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        lines.append({"text": str(text), "conf": round(float(score), 3),
                      "box": [min(xs) / w, min(ys) / h, (max(xs) - min(xs)) / w, (max(ys) - min(ys)) / h]})
    return {"w": w, "h": h, "lines": lines}


def _rapidocr(paths: list[Path]) -> dict[str, dict]:
    from PIL import Image

    out = {}
    for p in paths:
        with Image.open(p) as img:
            out[str(p)] = {"file": str(p), **_rapid_image(img)}
    return out


# ------------------------------------------------------------------ Tesseract
def _tesseract_image(path: Path) -> dict:
    from PIL import Image

    with Image.open(path) as img:
        w, h = img.size
    r = subprocess.run([shutil.which("tesseract"), str(path), "-", "-l", "chi_sim+eng", "tsv"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    groups: dict[tuple, dict] = {}
    for row in r.stdout.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        key = (c[2], c[3], c[4])  # block, paragraph, line
        x, y, bw, bh, conf = int(c[6]), int(c[7]), int(c[8]), int(c[9]), float(c[10])
        g = groups.setdefault(key, {"words": [], "conf": [], "x0": x, "y0": y, "x1": x + bw, "y1": y + bh})
        g["words"].append(c[11])
        g["conf"].append(max(conf, 0) / 100)
        g["x0"], g["y0"] = min(g["x0"], x), min(g["y0"], y)
        g["x1"], g["y1"] = max(g["x1"], x + bw), max(g["y1"], y + bh)
    lines = []
    for g in groups.values():
        cjk = any("一" <= ch <= "鿿" for ch in "".join(g["words"]))
        text = ("" if cjk else " ").join(g["words"])
        lines.append({"text": text, "conf": round(sum(g["conf"]) / len(g["conf"]), 3),
                      "box": [g["x0"] / w, g["y0"] / h, (g["x1"] - g["x0"]) / w, (g["y1"] - g["y0"]) / h]})
    return {"file": str(path), "w": w, "h": h, "lines": lines}


# ------------------------------------------------------------------ public API
def ocr_images(paths: list[Path], fast: bool = False, correction: bool = True) -> dict[str, dict]:
    """OCR images; keys are ``str(path)``. Raises OcrUnavailable if no backend."""
    if not paths:
        return {}
    b = backend()
    if b == "vision":
        return _vision(paths, fast, correction)
    if b == "rapidocr":
        return _rapidocr(paths)
    if b == "tesseract":
        return {str(p): _tesseract_image(p) for p in paths}
    raise OcrUnavailable(f"没有可用的本地 OCR：{install_hint()}")


def ocr_pdf(path: Path) -> list[dict]:
    """Render every page of a (scanned) PDF and OCR it. One result per page."""
    if backend() == "vision":
        r = subprocess.run([str(ocr_binary()), "--pdf", str(path)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=3600)
        out = []
        for line in r.stdout.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        raise OcrUnavailable('识别扫描版 PDF 需要 pypdfium2：pip install "lectureweaver[ocr]"') from e
    import tempfile

    pdf = pdfium.PdfDocument(str(path))
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(len(pdf)):
            page = pdf[i]
            scale = 2200 / max(page.get_width(), page.get_height())
            img_path = Path(td) / f"p{i + 1:04d}.png"
            page.render(scale=scale).to_pil().save(img_path)
            res = ocr_images([img_path])
            out.append(res.get(str(img_path), {"lines": []}))
    return out


def lines_text(result: dict, min_conf: float = 0.0) -> str:
    """Lines in reading order (top-to-bottom, then left-to-right)."""
    lines = [l for l in result.get("lines", []) if l.get("conf", 1) >= min_conf]
    lines.sort(key=lambda l: (round(l["box"][1] * 40), l["box"][0]))
    return "\n".join(l["text"] for l in lines)


def mean_conf(result: dict) -> float | None:
    ls = result.get("lines") or []
    if not ls:
        return None
    return sum(l.get("conf", 0) for l in ls) / len(ls)
