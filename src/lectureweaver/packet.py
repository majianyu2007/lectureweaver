"""The study packet: everything a note writer needs about one lecture, in one
Markdown file, with every item labelled by source.

The packet is input for the AI harness (or any model), not a note. It keeps
three kinds of content visibly apart:

* 课堂 - what was said (transcript) or shown (screenshots) in class;
* 资料 - user-provided course materials;
* 平台AI - the platform's own generated summary (useful for orientation,
  but never to be quoted as the teacher's words).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .kb import KB, loads
from .sync import lecture_label

SUBJECT_FOCUS = {
    "programming": "以录屏为准记录关键代码（逐字核对截图，看不清的地方标注“看不清”而不是猜）；记录操作步骤、运行结果、报错及原因、类/接口之间的关系；说明“为什么这样写”。",
    "programming_lab": "记录实验要求与操作步骤、关键代码与配置、遇到的错误与解决办法；区分老师演示过的与实验还要求自己完成的。",
    "math": "写清定义、成立条件、定理/公式、推导步骤与例题解法；定义、定理和条件照幻灯片/板书写（口头讲解常把条件说强或说反，如把充分条件说成“必须”），只有口述的公式注明来自口述；例题数值自己复算一遍；标出老师强调的易错点。",
    "humanities": "梳理论点结构与概念辨析，保留老师举的例子；标出老师明确说“会考/重点/问答题”的内容。",
    "language": "整理词汇短语、句型、课文要点、练习与答案、老师纠正的常见错误。",
    "lab": "记录实验目的、原理、步骤、注意事项与数据处理方法。",
    "general": "按老师讲授顺序整理要点、例子和强调内容。",
}

MARKUP = """\
笔记草稿用以下标记（`lw note save` 会校验并渲染，链接/图片会自动生成）：
- `{t 08:12:30}` 课堂时间点（录像墙钟时间，必须是本课时实际存在的时间）
- `{f F012}` 插入截图 F012（只能用本 packet 列出的截图 ID）
- `{m <资料ID> p.3}` 引用资料及页码/幻灯片号
- 课堂内容写在正文；资料内容放 `> [!quote] 资料：…`；AI 补充解释/类比/例题放 `> [!tip] AI 补充`
- 内容要对：定义、定理、公式、代码以屏幕上的幻灯片/板书/代码为准（见“截图文字”），口头说法和屏幕不一致时按屏幕写；
  例题数值复算，代码能运行就运行；拿不准的写“（待核实）”。资料只用来核对和补全本课讲过的内容，课上没讲的不写进本课时笔记
- 转写有同音错字：可按上下文纠正明显错误；不确定的写“（转写不清）”，不要编造老师的话
- 读者可能不看录像：课上讲的每段内容（例题完整解法、逐步演示、幻灯片和代码、易错点、作业）都要写进笔记
- 写给人复习用：直接写知识本身（定义、结论、步骤、代码、例题），不要写“老师用……说明……”式的转述，
  不要在正文里交代出处或声明“没有编造/以截图为准”；时间点每段最多一个。详见 skill 的 references/note-guide.md
"""


def _merge_paragraphs(segs: list[dict], max_gap: float = 6.0, max_len: int = 160) -> list[dict]:
    out: list[dict] = []
    for s in segs:
        if out and out[-1]["rec"] == s["rec"] and s["t0"] - out[-1]["t1"] <= max_gap and len(out[-1]["text"]) < max_len:
            out[-1]["text"] += s["text"]
            out[-1]["t1"] = s["t1"]
        else:
            out.append(dict(s))
    return out


def build_packet(kb: KB, lec) -> tuple[Path, dict]:
    d = kb.abs(lec["dir"])
    course = kb.q1("SELECT * FROM courses WHERE id=?", lec["course_id"])
    profile = loads(course["profile"], {})
    subject = profile.get("subject", "general")
    tr = _load(d / "transcript.json", {})
    fr = _load(d / "frames.json", {})
    segs = tr.get("segments", [])
    frames = fr.get("frames", [])
    recs = kb.q("SELECT * FROM recordings WHERE lecture_id=? ORDER BY start_time, segment", lec["id"])

    L: list[str] = []
    L.append(f"# 学习包：{course['name']} · {lecture_label(lec)}")
    L.append("")
    L.append(f"- 课时 ID：`{lec['id']}`；教师：{lec['teacher'] or '未知'}；教室：{lec['room'] or '未知'}；时间：{lec['start_time'] or '?'}–{lec['end_time'] or '?'}")
    if lec["match_status"] != "matched" or lec["match_note"]:
        L.append(f"- 课表匹配：{lec['match_status']}；{lec['match_note'] or ''}")
    L.append(f"- 课程类型：{subject}。整理重点：{SUBJECT_FOCUS.get(subject, SUBJECT_FOCUS['general'])}")
    if profile.get("note_prefs"):
        L.append(f"- 用户对本课程笔记的偏好：{profile['note_prefs']}")
    # data quality
    L.append("")
    L.append("## 素材情况（用来判断哪些内容可靠；“来源”一节由 lw note save 自动生成，笔记里不用复述）")
    for i, r in enumerate(recs, 1):
        det = loads(r["detail"], {}) or {}
        src = next((s for s in tr.get("sources", []) if s.get("rec") == r["id"]), {})
        kinds = [k for k, key in (("录屏", "vgaViewFiles"), ("教师机位", "teacherViewFiles"), ("学生机位", "studentViewFiles")) if det.get(key)]
        nfr = sum(1 for x in frames if x["rec"] == r["id"])
        trans = {"platform": "云课堂自带转写", "asr": f"本地语音识别（{src.get('model', '')}）", "none": "无转写"}.get(src.get("kind", "none"), "无转写")
        if src.get("reason"):
            trans += f"（{src['reason']}）"
        L.append(f"- 第{i}段 {(r['start_time'] or '')[11:16]}–{(r['end_time'] or '')[11:16]}（第{r['segment'] or '?'}节）：{trans}，{len([s for s in segs if s['rec'] == r['id']])} 句；截图 {nfr} 张；视频路数：{'、'.join(kinds) or '未知'}")
    for n in fr.get("notes", []):
        L.append(f"- 注意：{n}")
    L.append("- 转写来自教室麦克风，常有同音错字（如“唯心”识别成“维心”）；时间点以录像开始时刻换算为墙钟时间。")

    # neighbours
    prev = kb.q("SELECT l.id, l.date, l.title, n.title AS ntitle FROM lectures l LEFT JOIN notes n ON n.lecture_id=l.id AND n.kind='lecture' WHERE l.course_id=? AND l.date<? ORDER BY l.date DESC LIMIT 3", lec["course_id"], lec["date"] or "")
    nxt = kb.q("SELECT l.id, l.date, l.title, n.title AS ntitle FROM lectures l LEFT JOIN notes n ON n.lecture_id=l.id AND n.kind='lecture' WHERE l.course_id=? AND l.date>? ORDER BY l.date LIMIT 1", lec["course_id"], lec["date"] or "9999")
    L.append("")
    L.append("## 前后课时")
    for r in prev:
        concepts = [c["name"] for c in kb.q("SELECT c.name FROM concepts c JOIN concept_refs cr ON cr.concept_id=c.id WHERE cr.lecture_id=? LIMIT 12", r["id"])]
        L.append(f"- 之前 {r['date']} `{r['id']}`：{r['ntitle'] or r['title'] or '（尚无笔记）'}" + (f"；知识点：{'、'.join(concepts)}" if concepts else ""))
    for r in nxt:
        L.append(f"- 之后 {r['date']} `{r['id']}`：{r['ntitle'] or r['title'] or '（尚无笔记）'}")
    if not prev and not nxt:
        L.append("- （无）")
    L.append("- 需要细看某次课：`lw lecture show <课时ID>`；查概念出处：`lw search <关键词>`")

    # materials
    mats = kb.q(
        """SELECT m.id, m.title, m.filename, ml.role, ml.target_type, ml.note FROM materials m JOIN material_links ml ON ml.material_id=m.id
           WHERE (ml.target_type='lecture' AND ml.target_id=?) OR (ml.target_type='course' AND ml.target_id=?)""",
        lec["id"], lec["course_id"],
    )
    L.append("")
    L.append("## 关联资料（资料内容不是课堂讲授内容：只用来核对和补全本课讲过的部分，课上没讲的不写进本课时笔记）")
    by_id: dict = {}
    for m in mats:  # a material linked to both the course and this lecture is listed once
        if m["id"] not in by_id or m["target_type"] == "lecture":
            by_id[m["id"]] = m
    mats = list(by_id.values())
    if mats:
        for m in mats:
            L.append(f"- `{m['id']}` 《{m['title'] or m['filename']}》（{m['role'] or '参考'}，关联到{'本课时' if m['target_type'] == 'lecture' else '整门课'}）{m['note'] or ''}；阅读：`lw material show {m['id']}`")
    else:
        L.append("- （无。用户可以用 `lw material add` 导入 PPT/PDF/Word/代码等）")

    # platform AI
    ai_lines = []
    for r in recs:
        p = d / "platform" / f"platform_ai_{r['id']}.json"
        ai = _load(p, {})
        s = ai.get("summarize")
        text = s.get("summarizeContent") if isinstance(s, dict) else None
        if text:
            ai_lines.append(f"- 第{(r['segment'] or '?')}节：{text}")
    if ai_lines:
        L.append("")
        L.append("## 平台 AI 摘要（云课堂自动生成，仅供定位，不得当作老师原话引用）")
        L += ai_lines

    # platform slide OCR
    ppt_lines = []
    for r in recs:
        for pg in _load(d / "platform" / f"ppt_{r['id']}.json", []) or []:
            txt = (pg.get("recognitionContent") or pg.get("content") or pg.get("pptContent") or "").strip()
            if txt:
                ppt_lines.append(f"- {pg.get('timePoint', '')} {txt[:200]}")
    if ppt_lines:
        L.append("")
        L.append("## 平台识别的课件页（时间点相对该段录像开始）")
        L += ppt_lines[:80]

    # timeline
    L.append("")
    L.append("## 时间线（课堂转写 + 截图）")
    L.append("截图标记：🖼 ID（视频路，显示时段）OCR 前几行。完整 OCR 见文末；需要看图时直接打开图片路径。")
    events = [("s", p["rec"], p["t0"], p) for p in _merge_paragraphs(segs)] + [("f", x["rec"], x["t"], x) for x in frames]
    order = {r["id"]: i for i, r in enumerate(recs)}
    events.sort(key=lambda e: (order.get(e[1], 99), e[2]))
    last_rec = None
    for kind, rec, _t, p in events:
        if rec != last_rec:
            r = next((x for x in recs if x["id"] == rec), None)
            L.append("")
            L.append(f"### 第{order.get(rec, 0) + 1}段录像（第{r['segment'] if r else '?'}节）")
            last_rec = rec
        if kind == "s":
            L.append(f"[{p['clock']}] {p['text']}")
        else:
            ocr = clean_ocr_lines(frame_text(p))
            # slides: the first lines are deck/chapter titles; show enough to
            # reach the statement on the slide, next to what was said about it
            head = " / ".join(ocr[:6])[:240] if p["stream"] == "vga" else " / ".join(ocr[:3])[:120]
            span = p["clock"] if p.get("first_seen") in (None, p["clock"]) else f"{p['first_seen']}–{p['clock']}"
            L.append(f"🖼 {p['id']}（{_stream_name(p['stream'])}，{span}）{head}")
    if not segs:
        L.append("（本课时没有转写；只能依据截图和资料，笔记中须说明）")

    # OCR, compacted: only lines that are new compared with recent screenshots
    if frames:
        L.append("")
        L.append("## 截图文字（OCR，只列出相对前几张截图新出现的行）")
        L.append(f"图片目录：`{kb.abs(lec['dir']) / 'frames'}`；单张完整 OCR：`lw lecture frame {lec['id']} F012`。OCR 可能有错，代码以图片为准。")
        recent: list[set[str]] = []
        for x in frames:
            lines = clean_ocr_lines(frame_text(x))
            seen = set().union(*recent) if recent else set()
            new = [ln for ln in lines if ln not in seen]
            recent = (recent + [set(lines)])[-4:]
            head = f"### {x['id']} · {x['clock']} · {_stream_name(x['stream'])}" + (f" · 也出现在 {', '.join(x['also_at'])}" if x.get("also_at") else "")
            if x.get("reason"):
                head += f" · {x['reason']}"
            L.append("")
            L.append(head)
            if not new:
                L.append("（文字与前几张相同）" if lines else "（无可识别文字）")
                continue
            omitted = len(lines) - len(new)
            L.append("```text")
            L.append("\n".join(new)[:1500])
            L.append("```")
            if omitted:
                L.append(f"（另有 {omitted} 行与前几张相同，已省略）")

    L.append("")
    L.append("## 写笔记前请注意")
    L.append(MARKUP)
    path = d / "packet.md"
    path.write_text("\n".join(L), "utf-8")
    return path, {"chars": sum(len(x) for x in L), "segments": len(segs), "frames": len(frames)}


_CHROME = re.compile(
    r"^(File|Edit|Source|Refactor|Navigate|Search|Project|Run|Window|Help|文件|编辑|视图|插入|开始|查看|帮助)"
    r"(\s+(File|Edit|Source|Refactor|Navigate|Search|Project|Run|Window|Help|文件|编辑|视图|插入|开始|查看|帮助))+\s*$|"
    r"workspace-|Eclipse IDE|Mindjet MindManager|^\W*$"
)


def frame_text(x: dict, min_conf: float = 0.45) -> str:
    """OCR text without low-confidence lines (garbage from tiny/blurred UI text)."""
    if "ocr_lines" in x:
        return "\n".join(t for t, c in x["ocr_lines"] if c >= min_conf)
    return x.get("ocr") or ""


def clean_ocr_lines(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) <= 1 or _CHROME.search(ln):
            continue
        if len(ln) <= 3 and not re.search(r"[\u4e00-\u9fff]", ln):
            continue
        out.append(ln)
    return out


def _stream_name(s: str) -> str:
    return {"vga": "录屏", "teacher": "教师机位", "student": "学生机位"}.get(s, s)


def _load(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default
