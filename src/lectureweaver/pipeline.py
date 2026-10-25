"""Per-lecture processing: resumable stages with honest accounting.

Stages (each stores its result under the lecture folder and a row in
``stages``; a finished stage is skipped on the next run unless forced):

``details``    platform detail per recording (video files, task id), the
               platform's slide OCR and its own AI summary (kept, labelled
               as platform-generated - never as the teacher's words).
``transcript`` the platform transcript when present, else local ASR on the
               audio samples only (cloud ASR only with consent).
``visual``     screenshots of the screen recording (keyframes only) + local
               OCR; teacher camera only for long static-screen stretches.
``packet``     a compact Markdown study packet for the note writer.

Media scratch files live in the machine tmp dir and are removed when a stage
ends, also on failure.
"""

from __future__ import annotations

import json
import shutil
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .config import MachineConfig, tmp_dir
from .kb import KB, dumps, loads
from .media import human_bytes

STAGES = ["details", "transcript", "visual", "packet"]
BOARD_CROP = 0.6  # fraction of the teacher-camera frame kept (from the top)

Log = Callable[[str], None]


@dataclass
class StageResult:
    status: str
    detail: dict = field(default_factory=dict)
    bytes_in: int = 0


class Pipeline:
    def __init__(self, kb: KB, session=None, log: Log = print, force: set[str] | None = None, asr: str = "auto", visual_params=None):
        self.kb = kb
        self.session = session
        self.log = log
        self.force = force or set()
        self.asr = asr  # auto | local | none | <cloud provider name>
        self.visual_params = visual_params

    # ------------------------------------------------------------------ helpers
    def ldir(self, lec) -> Path:
        p = self.kb.abs(lec["dir"])
        p.mkdir(parents=True, exist_ok=True)
        return p

    def recordings(self, lid: str) -> list:
        return self.kb.q("SELECT * FROM recordings WHERE lecture_id=? ORDER BY start_time, segment", lid)

    def ylb(self):
        if self.session is None:
            from .session import Session

            self.session = Session()
        return self.session.ylb()

    def run(self, lec, stages: list[str] | None = None) -> dict:
        lid = lec["id"]
        summary = {}
        upstream_changed = False  # a re-run stage makes everything after it stale
        for st in stages or STAGES:
            row = self.kb.stage(lid, st)
            if (row and row["status"] in ("done", "skipped") and not upstream_changed
                    and st not in self.force and "all" not in self.force):
                summary[st] = "已完成（跳过）"
                continue
            upstream_changed = True
            fn = getattr(self, f"stage_{st}")
            started = time.time()
            self.kb.set_stage(lid, st, "running", started_at=started)
            work = tmp_dir() / f"{lid}-{st}"
            try:
                res: StageResult = fn(lec, work)
                self.kb.set_stage(lid, st, res.status, res.detail, bytes_in=res.bytes_in, started_at=started)
                summary[st] = f"{res.status}（传输 {human_bytes(res.bytes_in)}，{time.time() - started:.0f}s）"
            except Exception as e:  # noqa: BLE001 - record and continue with the next lecture
                from .platform.cas import AuthError

                self.kb.set_stage(lid, st, "failed", {"trace": traceback.format_exc()[-1500:]}, error=str(e), started_at=started)
                summary[st] = f"失败：{e}"
                self.log(f"  [{st}] 失败：{e}")
                if isinstance(e, AuthError):
                    raise
                break  # later stages depend on this one
            finally:
                shutil.rmtree(work, ignore_errors=True)
        return summary

    # ------------------------------------------------------------------ details
    def stage_details(self, lec, work: Path) -> StageResult:
        y = self.ylb()
        d = self.ldir(lec)
        raw = d / "platform"
        raw.mkdir(exist_ok=True)
        info = {"recordings": []}
        for i, r in enumerate(self.recordings(lec["id"]), 1):
            det = y.record_video(r["id"])
            self.kb.x("UPDATE recordings SET detail=? WHERE id=?", dumps(det), r["id"])
            (raw / f"detail_{r['id']}.json").write_text(dumps(det), "utf-8")
            streams = {}
            for key, name in (("vgaViewFiles", "vga"), ("teacherViewFiles", "teacher"), ("studentViewFiles", "student")):
                files = det.get(key) or []
                if files:
                    f = files[0]
                    streams[name] = {"id": f.get("id"), "size": f.get("videoFileSize"), "seconds": f.get("fileLength"), "start": f.get("startTime"), "parts": len(files)}
            ppt = []
            for f in (det.get("vgaViewFiles") or [])[:1] + (det.get("teacherViewFiles") or [])[:1]:
                try:
                    ppt = y.ppt_pages(f["id"]) or []
                except Exception:  # noqa: BLE001
                    ppt = []
                if ppt:
                    break
            if ppt:
                (raw / f"ppt_{r['id']}.json").write_text(dumps(ppt), "utf-8")
            ai = {}
            for kind in ("summarize", "abstracts", "mindmap"):
                try:
                    v = y.platform_ai(r["id"], kind)
                except Exception:  # noqa: BLE001
                    v = None
                if v:
                    ai[kind] = v
            if ai:
                (raw / f"platform_ai_{r['id']}.json").write_text(dumps(ai), "utf-8")
            info["recordings"].append({"id": r["id"], "part": i, "streams": streams, "ppt_pages": len(ppt), "platform_ai": sorted(ai)})
        self.kb.db.commit()
        return StageResult("done", info)

    # ------------------------------------------------------------------ transcript
    def stage_transcript(self, lec, work: Path) -> StageResult:
        y = self.ylb()
        d = self.ldir(lec)
        segments, sources = [], []
        bytes_in = 0
        missing = []
        for part, r in enumerate(self.recordings(lec["id"]), 1):
            det = loads(r["detail"], {}) or {}
            rec_start = _start_dt(r, det)
            segs, src = [], None
            task = det.get("taskId") or r["task_id"]
            if task:
                try:
                    sp = y.speech(task) or []
                    res = sp[0].get("speechRecognitionResults") if sp else []
                except Exception:  # noqa: BLE001
                    res = []
                if res:
                    segs = [{"t0": float(x["startTimeStamp"]), "t1": float(x["endTimeStamp"]), "text": x["recognitionResult"].strip()} for x in res if (x.get("recognitionResult") or "").strip()]
                    src = {"kind": "platform", "model": "云课堂语音转写"}
            if not segs and self.asr != "none":
                self.no_audio_reason = None
                segs, src, b = self._asr(r, det, work)
                bytes_in += b
            if not segs:
                missing.append(part)
                reason = getattr(self, "no_audio_reason", None) or ("平台无转写，且未启用语音识别" if self.asr == "none" else "平台无转写，本地识别未得到文字")
                sources.append({"rec": r["id"], "part": part, "kind": "none", "reason": reason})
                continue
            sources.append({"rec": r["id"], "part": part, **src, "segments": len(segs)})
            for s in segs:
                segments.append({
                    "rec": r["id"], "part": part, "t0": round(s["t0"], 2), "t1": round(s["t1"], 2),
                    "clock": _clock(rec_start, s["t0"]), "text": s["text"], "src": src["kind"],
                })
        doc = {"lecture": lec["id"], "sources": sources, "segments": segments, "created_at": time.time()}
        (d / "transcript.json").write_text(json.dumps(doc, ensure_ascii=False, indent=0), "utf-8")
        self.kb.index_replace("transcript", lec["id"], [(f"[{s['clock']}] {s['text']}", lec["id"], lec["course_id"], None) for s in segments] if segments else [])
        status = "done" if not missing else ("partial" if segments else "failed")
        detail = {"sources": sources, "segments": len(segments), "missing_parts": missing}
        if missing and not segments:
            status = "partial"
        return StageResult(status, detail, bytes_in)

    def _asr(self, r, det, work: Path):
        """Speech recognition over every time slot of the recording.

        A recording usually has one file per camera. Some rooms add a second
        screen-recording file covering the *same* time (two projector inputs);
        that is one slot. Files that follow each other in time are separate
        slots and are all recognised, with times shifted onto one timeline.
        """
        from .asr import whisper_available

        mode = self.asr
        if mode in ("auto", "local"):
            ok, why = whisper_available()
            if not ok:
                self.log(f"  本地语音识别不可用：{why}")
                return [], None, 0
        slots = audio_slots(det)
        if not slots:
            return [], None, 0
        rec_start = _start_dt(r, det)
        if len(slots) > 1:
            self.log(f"  该录像按时间分成 {len(slots)} 个文件，逐个识别")
        segs, src, total = [], None, 0
        for slot_start, cands in slots:
            got, s_src, b = self._asr_files(r, cands, work)
            total += b
            off = (slot_start - rec_start).total_seconds() if (slot_start and rec_start) else 0.0
            segs += [{**x, "t0": x["t0"] + off, "t1": x["t1"] + off} for x in got]
            src = src or s_src
        return segs, src, total

    def _asr_files(self, r, cands, work: Path):
        from .asr import AsrError, transcribe_openai_compatible, wav_duration
        from .audio import choose_audio_source, fetch_audio_wav
        from .platform.ylb import video_url
        from .visual import RangeReader

        mode = self.asr
        readers = [RangeReader(video_url(f)) for _n, f in cands]
        work.mkdir(parents=True, exist_ok=True)
        try:
            pick = choose_audio_source(list(zip([n for n, _ in cands], readers)), work)
            wav = work / f"{r['id']}.wav"
            if pick and pick.speech_s < 3:
                for n in pick.notes:
                    self.log(f"  {n}")
                self.no_audio_reason = "录像三路音频抽样均接近静音（可能当天未开麦或录音故障），无法生成转写"
                self.log(f"  {self.no_audio_reason}")
                return [], None, sum(x.bytes for x in readers)
            if pick:
                for n in pick.notes:
                    self.log(f"  {n}")
                how = "按样本表只取音频" if not pick.max_gap else "音频与视频逐帧交错，只能整段读取（只保留音频）"
                self.log(f"  采用 {pick.name} 音轨：{how}，约 {human_bytes(pick.est_bytes)}")
                fetch_audio_wav(pick.reader, pick.track, wav, max_gap=pick.max_gap)
            else:
                from .media import RangeProxy, stream_audio_full

                self.log("  无法按样本表取音频，降级为整段流式解码（只保存音频，不保存视频）")
                with RangeProxy() as px:
                    stream_audio_full(px.local(video_url(cands[0][1])), wav)
                    extra = px.meter.bytes
                readers[0].bytes += extra
            if mode in ("auto", "local"):
                segs, model = self._local_asr(wav)
                src = {"kind": "asr", "model": model}
            else:
                from .consent import require

                prov = require(self.kb, f"asr:{mode}", "audio")
                from .secrets import get_secret

                key = get_secret(f"apikey:{mode}")
                if not key:
                    raise AsrError(f"未配置 {mode} 的 API Key（lw provider set-key {mode}）")
                self.log(f"  云端语音识别中（{wav_duration(wav) / 60:.0f} 分钟音频）…")
                segs, model = transcribe_openai_compatible(wav, prov, key)
                self.kb.x("INSERT INTO usage(service,ts,units,unit,note) VALUES(?,?,?,?,?)", f"asr:{mode}", time.time(), wav_duration(wav) / 60, "min", r["id"])
                self.kb.db.commit()
                src = {"kind": "asr", "model": model}
            return segs, src, sum(x.bytes for x in readers)
        finally:
            for x in readers:
                x.close()

    def _local_asr(self, wav: Path):
        """whisper.cpp uses every CPU core: with several lectures processed at
        once, recognise one at a time (machine-wide lock) and let the others
        keep downloading screenshots meanwhile."""
        from .asr import transcribe_whisper, wav_duration
        from .config import state_dir
        from .kb import try_lock

        path = state_dir() / "locks" / "local-asr.lock"
        lock = try_lock(path)
        if lock is None:
            self.log("  等待另一个课时的本地语音识别结束…")
            lock = try_lock(path, wait=True)
        try:
            self.log(f"  语音识别中（{wav_duration(wav) / 60:.0f} 分钟音频）…")
            return transcribe_whisper(wav)
        finally:
            lock.close()

    # ------------------------------------------------------------------ visual
    def stage_visual(self, lec, work: Path) -> StageResult:
        from .ocr import OcrUnavailable
        from .platform.ylb import video_url
        from .visual import KeyframeSource, RangeReader, VisualParams, dedupe, open_track, save_webp

        d = self.ldir(lec)
        fdir = d / "frames"
        fdir.mkdir(exist_ok=True)
        params = self.visual_params or VisualParams()
        frames_out, bytes_in, notes = [], 0, []
        transcript = _load(d / "transcript.json", {}).get("segments", [])
        n = 0
        for part, r in enumerate(self.recordings(lec["id"]), 1):
            det = loads(r["detail"], {}) or {}
            vga = (det.get("vgaViewFiles") or [])
            teacher = (det.get("teacherViewFiles") or [])
            src_files = [("vga", f) for f in vga] or [("teacher", f) for f in teacher]
            if not vga and teacher:
                notes.append(f"第{part}段没有电脑录屏，改用教师机位")
            for stream, f in src_files:
                rd = RangeReader(video_url(f))
                try:
                    movie, track = open_track(rd)
                    ks = KeyframeSource(rd, movie, track)
                    picked = select_frames_logged(ks, params, self.log)
                    kept = dedupe(picked, params.dedupe_thr)
                    start = _parse_dt(f.get("startTime")) or _start_dt(r, det)
                    for fr, also in kept:
                        n += 1
                        fid = f"F{n:03d}"
                        path = fdir / f"{fid}.webp"
                        save_webp(fr.img, path)
                        frames_out.append({
                            "id": fid, "rec": r["id"], "part": part, "stream": stream, "t": round(fr.t, 2),
                            "clock": _clock(start, fr.t), "first_seen": _clock(start, getattr(fr, "first_seen", fr.t)),
                            "also_at": [_clock(start, a) for a in also], "dwell_s": round(getattr(fr, "dwell", 0)),
                            "file": f"frames/{fid}.webp", "w": fr.img.width, "h": fr.img.height,
                        })
                        fr.release()
                    # Teacher camera for long static screens while teaching continues (board work).
                    if stream == "vga" and teacher:
                        extra = self._board_frames(lec, r, part, teacher[0], [fx for fx in frames_out if fx["rec"] == r["id"]], transcript, fdir, n, params)
                        n += len(extra)
                        frames_out += extra
                        bytes_in += sum(e.pop("_bytes", 0) for e in extra)
                finally:
                    bytes_in += rd.bytes
                    rd.close()
        try:
            run_ocr(frames_out, fdir)
        except OcrUnavailable as e:
            notes.append(f"本机没有可用的 OCR，截图未识别文字（{e}）")
        (d / "frames.json").write_text(json.dumps({"frames": frames_out, "notes": notes}, ensure_ascii=False, indent=1), "utf-8")
        self.kb.index_replace("frame", lec["id"], [(f"[{x['clock']}] {x.get('ocr', '')}", lec["id"], lec["course_id"], None) for x in frames_out])
        size = sum((fdir / Path(x["file"]).name).stat().st_size for x in frames_out)
        return StageResult("done", {"frames": len(frames_out), "stored_bytes": size, "notes": notes}, bytes_in)

    def _board_frames(self, lec, r, part, tfile, vga_frames, transcript, fdir, n0, params) -> list[dict]:
        """Sample the teacher camera where the screen sat unchanged ≥ 4 min while speech continued."""
        from .platform.ylb import video_url
        from .visual import KeyframeSource, RangeReader, diff_fraction, open_track, save_webp, signature

        spans = []
        ts = sorted((x["t"], x.get("dwell_s", 0)) for x in vga_frames)
        for t, dwell in ts:
            if dwell >= 240:
                a, b = t - dwell, t
                talk = sum(1 for s in transcript if s["rec"] == r["id"] and a <= s["t0"] <= b)
                if talk >= 10:
                    spans.append((a, b))
        if not spans:
            return []
        rd = RangeReader(video_url(tfile))
        out = []
        try:
            movie, track = open_track(rd)
            ks = KeyframeSource(rd, movie, track)
            times = [a + i * 90 for a, b in spans for i in range(int((b - a) // 90) + 1)]
            frames = [f for f in ks.get(sorted({ks.nearest(t) for t in times})) if f.sig is not None]
            # Keep the front of the room (screen, board, teacher) at full
            # resolution; the lower part is students, of no teaching value.
            for f in frames:
                f.img = f.img.crop((0, 0, f.img.width, int(f.img.height * BOARD_CROP)))
                f.sig = signature(f.img)
            kept = []
            for f in frames:
                if not kept or diff_fraction(kept[-1].sig, f.sig) > 0.01:
                    kept.append(f)
                else:
                    kept[-1] = f
            start = _parse_dt(tfile.get("startTime")) or _start_dt(r, {})
            for i, f in enumerate(kept[:8], 1):
                fid = f"F{n0 + i:03d}"
                save_webp(f.img, fdir / f"{fid}.webp")
                out.append({"id": fid, "rec": r["id"], "part": part, "stream": "teacher", "t": round(f.t, 2), "clock": _clock(start, f.t),
                            "first_seen": _clock(start, f.t), "also_at": [], "dwell_s": 0, "file": f"frames/{fid}.webp",
                            "w": f.img.width, "h": f.img.height, "crop": f"上部 {int(BOARD_CROP * 100)}%",
                            "reason": "录屏长时间不变，补充教师机位（可能有板书；已裁掉画面下部的学生区域）"})
                f.release()
            if out:
                out[0]["_bytes"] = rd.bytes
        finally:
            rd.close()
        return out

    # ------------------------------------------------------------------ packet
    def stage_packet(self, lec, work: Path) -> StageResult:
        from .packet import build_packet

        fj = _load(self.ldir(lec) / "frames.json", {})
        if fj.get("frames") and "ocr_lines" not in fj["frames"][0]:
            from .ocr import OcrUnavailable

            try:
                refresh_ocr(self.kb, lec)
            except OcrUnavailable:
                pass
        path, stats = build_packet(self.kb, lec)
        return StageResult("done", {"path": self.kb.rel(path), **stats})


def run_ocr(frames: list[dict], fdir: Path) -> None:
    """Local OCR; keeps per-line confidence so later steps can filter noise."""
    from .ocr import lines_text, mean_conf, ocr_images

    res = ocr_images([fdir / Path(x["file"]).name for x in frames])
    for x in frames:
        j = res.get(str(fdir / Path(x["file"]).name), {})
        lines = sorted(j.get("lines", []), key=lambda l: (round(l["box"][1] * 40), l["box"][0]))
        x["ocr"] = lines_text(j)
        x["ocr_lines"] = [[l["text"], round(l.get("conf", 0), 2)] for l in lines]
        x["ocr_conf"] = round(mean_conf(j) or 0, 2)


def refresh_ocr(kb: KB, lec) -> int:
    """Re-run OCR on saved screenshots (no network). Returns frame count."""
    d = kb.abs(lec["dir"])
    data = _load(d / "frames.json", {})
    frames = data.get("frames", [])
    if frames:
        run_ocr(frames, d / "frames")
        (d / "frames.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        kb.index_replace("frame", lec["id"], [(f"[{x['clock']}] {x.get('ocr', '')}", lec["id"], lec["course_id"], None) for x in frames])
    return len(frames)


def select_frames_logged(ks, params, log):
    from .visual import select_frames

    return select_frames(ks, params, log=lambda m: None)


def _load(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip()[:19])
    except ValueError:
        return None


def _start_dt(r, det: dict) -> datetime | None:
    for k in ("startTime",):
        v = _parse_dt((det or {}).get(k))
        if v:
            return v
    for f in (det or {}).get("teacherViewFiles") or (det or {}).get("vgaViewFiles") or []:
        v = _parse_dt(f.get("startTime"))
        if v:
            return v
    return _parse_dt(r["start_time"])


def _clock(start: datetime | None, t: float) -> str:
    if start is None:
        s = int(t)
        return f"+{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return (start + timedelta(seconds=float(t))).strftime("%H:%M:%S")


def audio_slots(det: dict, tol_s: float = 60.0) -> list[tuple[datetime | None, list[tuple[str, dict]]]]:
    """Group a recording's files into time slots: [(slot start, [(stream, file)])].

    Files of different cameras starting within ``tol_s`` of each other cover
    the same time; within one stream only the first file of a slot is used.
    """
    files = []
    for name, key in (("teacher", "teacherViewFiles"), ("student", "studentViewFiles"), ("vga", "vgaViewFiles")):
        for f in det.get(key) or []:
            files.append((_parse_dt(f.get("startTime")), name, f))
    files.sort(key=lambda x: (x[0] is None, x[0] or datetime.min))
    slots: list[tuple[datetime | None, list[tuple[str, dict]]]] = []
    for start, name, f in files:
        cur = slots[-1] if slots else None
        same = cur is not None and (start is None or cur[0] is None or abs((start - cur[0]).total_seconds()) <= tol_s)
        if not same:
            slots.append((start, []))
            cur = slots[-1]
        if name not in {n for n, _ in cur[1]}:
            cur[1].append((name, f))
    return slots


def cleanup_tmp() -> int:
    """Remove leftover scratch media (e.g. after a crash). Returns bytes freed."""
    from .media import disk_usage

    root = tmp_dir()
    freed = disk_usage(root)
    for p in root.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    return freed


def machine_config() -> MachineConfig:
    return MachineConfig.load()
