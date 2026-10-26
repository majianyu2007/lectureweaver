"""Notes: validated rendering, version history, export to the notes folder,
and protection of the user's own edits.

Draft markup (written by the AI harness, see packet.MARKUP):
    {t 10:12:48}        classroom time (wall clock) → link into the player
    {f F012}            screenshot → image copied into the vault
    {m m_ab12cd p.3}    material reference → link to the file/page

Rules enforced here (not left to the model's good behaviour):
* every time reference must fall inside one of the lecture's recordings,
  every screenshot id must exist, every material id must be known;
* a note file the user changed (in Obsidian or any editor) is never
  overwritten: the change is recorded as a user version first, and a new AI
  version is written next to it as a proposal unless the user edits are
  provably kept (3-way merge without conflicts) and nothing the user added is
  missing from the new text;
* every version is kept; rollback creates a new version, it never deletes.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .kb import KB, loads
from .platform.ylb import player_url

T_RE = re.compile(r"\{t\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*\}")
T_RUN_RE = re.compile(r"(\{t\s+[^}]*\})(?:[ \t]*\{t\s+[^}]*\})+")
F_RE = re.compile(r"\{f\s+(F\d{3,})\s*\}")
M_RE = re.compile(r"\{m\s+(m_[0-9a-f]{6,})(?:\s+([^}]*))?\}")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


class NoteError(Exception):
    pass


@dataclass
class RenderResult:
    text: str
    attachments: list[tuple[Path, str]]  # (source file, vault-relative dest)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    checks: list[str] = field(default_factory=list)  # statements to verify, not to rewrite


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def notes_root() -> Path:
    from .config import MachineConfig

    p = MachineConfig.load().notes_path
    if not p:
        raise NoteError("还没有设置笔记目录：运行 lw init --notes <Obsidian Vault 内的文件夹或任意目录>")
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_name(s: str, limit: int = 80) -> str:
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[\\/:*?\"<>|#^\[\]]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:limit] or "未命名"


# ---------------------------------------------------------------- lecture context
def _recs_with_windows(kb: KB, lecture_id: str) -> list[dict]:
    out = []
    for r in kb.q("SELECT * FROM recordings WHERE lecture_id=? ORDER BY start_time", lecture_id):
        det = loads(r["detail"], {}) or {}
        files = det.get("vgaViewFiles") or det.get("teacherViewFiles") or []
        starts = [f.get("startTime") for f in files if f.get("startTime")] or [r["start_time"]]
        ends = [f.get("endTime") for f in files if f.get("endTime")] or [r["end_time"]]
        try:
            # the player plays from the first file; later files extend the window
            s = datetime.fromisoformat(starts[0][:19])
            e = max(datetime.fromisoformat(x[:19]) for x in ends)
        except (TypeError, ValueError):
            continue
        out.append({"id": r["id"], "start": s, "end": e, "ctid": r["course_table_id"]})
    return out


def _load(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def render(kb: KB, note: dict, source: str, note_path: Path, root: Path) -> RenderResult:
    """Turn draft markup into Markdown for the vault (also validates)."""
    errors: list[str] = []
    warnings: list[str] = []
    attachments: list[tuple[Path, str]] = []
    lec = kb.q1("SELECT * FROM lectures WHERE id=?", note["lecture_id"]) if note.get("lecture_id") else None
    recs = _recs_with_windows(kb, lec["id"]) if lec else []
    ldir = kb.abs(lec["dir"]) if lec else None
    segs = _load(ldir / "transcript.json", {}).get("segments", []) if ldir else []
    frames = {f["id"]: f for f in _load(ldir / "frames.json", {}).get("frames", [])} if ldir else {}
    clocks = sorted(_secs(s["clock"]) for s in segs if ":" in s["clock"]) + sorted(_secs(f["clock"]) for f in frames.values())
    n_t = n_f = n_m = 0

    def t_sub(m):
        nonlocal n_t
        clock = m.group(1)
        if clock.count(":") == 1:
            clock += ":00"
        if not lec:
            errors.append(f"{{t {clock}}}：这篇笔记没有关联课时，不能引用课堂时间")
            return clock
        day = datetime.fromisoformat(lec["date"])
        h, mi, se = (int(x) for x in clock.split(":"))
        when = day.replace(hour=h, minute=mi, second=se)
        rec = next((r for r in recs if r["start"] - timedelta(seconds=5) <= when <= r["end"] + timedelta(seconds=5)), None)
        if rec is None:
            spans = "、".join(f"{r['start']:%H:%M:%S}–{r['end']:%H:%M:%S}" for r in recs)
            errors.append(f"{{t {clock}}} 不在本课时录像时段内（{spans}）")
            return clock
        if clocks and min(abs(_secs(clock) - c) for c in clocks) > 90:
            warnings.append(f"{clock} 附近 90 秒内没有转写或截图，请确认这个时间点")
        n_t += 1
        off = max(0, int((when - rec["start"]).total_seconds()))
        # minutes are enough to read; the link itself seeks to the second
        return f"[{clock[:5]}]({player_url(rec['ctid'], rec['id'], off)})"

    def f_sub(m):
        nonlocal n_f
        fid = m.group(1)
        f = frames.get(fid)
        if not f:
            errors.append(f"截图 {fid} 不存在（只能用学习包列出的截图）")
            return f"（截图 {fid} 缺失）"
        n_f += 1
        src = ldir / f["file"]
        dest_rel = f"attachments/{lec['id']}/{fid}.webp"
        attachments.append((src, dest_rel))
        rel_from_note = os.path.relpath(root / note_folder(kb, note) / dest_rel, note_path.parent)
        stream = {"vga": "录屏", "teacher": "教师机位"}.get(f["stream"], f["stream"])
        return f"![{fid} · {stream} {f['clock']}]({_url(rel_from_note)})"

    def m_sub(m):
        nonlocal n_m
        mid, loc = m.group(1), (m.group(2) or "").strip()
        r = kb.q1("SELECT * FROM materials WHERE id=?", mid)
        if not r:
            errors.append(f"资料 {mid} 不存在")
            return f"（资料 {mid}）"
        n_m += 1
        target = kb.abs(r["path"])
        anchor = ""
        pm = re.search(r"p\.?\s*(\d+)", loc)
        if pm and target.suffix.lower() == ".pdf":
            if r["units"] and int(pm.group(1)) > r["units"]:
                errors.append(f"{{m {mid} {loc}}}：《{r['title'] or r['filename']}》只有 {r['units']} 页。p. 后写 PDF 的第几页，不是页脚印的页码")
            anchor = f"#page={pm.group(1)}"
        label = f"《{r['title'] or r['filename']}》{(' ' + loc) if loc else ''}"
        return f"[{label}]({target.as_uri()}{anchor})"

    warnings += style_warnings(kb, note, source)
    # "{t a} {t b} {t c}" in a row reads as noise: one link, to the first.
    body = T_RUN_RE.sub(lambda m: m.group(1), source)
    body = T_RE.sub(t_sub, body)
    body = F_RE.sub(f_sub, body)
    body = M_RE.sub(m_sub, body)
    leftovers = re.findall(r"\{[tfm]\s+[^}]*\}", body)
    for x in leftovers:
        errors.append(f"无法识别的标记：{x}")
    if lec and n_t == 0 and segs:
        warnings.append("笔记没有引用任何课堂时间点（{t HH:MM:SS}），课堂内容将无法追溯")
    elif lec and note.get("kind") == "lecture":
        cited = [_secs(c) for c in T_RE.findall(source)] + [_secs(frames[f]["clock"]) for f in F_RE.findall(source) if f in frames]
        gaps = coverage_gaps(segs, cited)
        if gaps:
            warnings.append("这些时段老师讲了不少，但笔记没有引用（可能漏了内容；点名、闲聊、课间可以忽略）："
                            + "；".join(f"{a}–{b}（约 {n} 字转写）" for a, b, n in gaps[:8])
                            + "。笔记要让人不看视频也能学全，请回到学习包核对这些时段")
    checks = logic_checks(kb, note, source)
    return RenderResult(body, attachments, errors, warnings, {"times": n_t, "frames": n_f, "materials": n_m}, checks)


# Phrases where the writer talks about its own sourcing instead of the subject
# ("不是课堂给出的结论", "未伪造运行结果"). Provenance is already carried by the
# layout (body = classroom, [!tip] = AI) and the source block; in the text it
# only makes the note read like a compliance report.
META_RE = re.compile(
    r"不是课堂(给出|讲)|并非(本节|课堂|老师)|伪造|补造|冒充|编造|不还原|"
    r"如实(保留|记录|标注)|(以|按)截图(为准|核对)|截图(未|没有)给出|画面未给出|"
    r"(课堂|课上|本节|老师)(并)?(未|没有)(展开|给出|演示|讲解|说明)|不声称|未在此展开"
)
CONTRAST_RE = re.compile(r"不是[^。；\n]{1,30}而是|不等于|不要把|不能把|并不是|不意味着")


def style_warnings(kb: KB, note: dict, source: str) -> list[str]:
    """Readability checks on a draft (warnings only; see note-guide.md)."""
    out: list[str] = []
    body = re.sub(r"```.*?```", "", source, flags=re.S)
    m = re.search(r"^#\s+(.+)$", source, re.M)
    if m and note.get("kind") == "lecture":
        title = m.group(1).strip()
        course = None
        if note.get("course_id"):
            c = kb.q1("SELECT name FROM courses WHERE id=?", note["course_id"])
            course = c["name"] if c else None
        if len(title) > 20 or re.search(r"第\s*\d+\s*周|\d{4}-\d{2}-\d{2}|周[一二三四五六日]", title) or (course and course in title):
            out.append(f"标题“{title}”太长或含课程名/日期/周次（文件名已有日期和周次）：写成 ≤20 字的主题，如“递归与回溯”")
    paras = []
    for p in re.split(r"\n\s*\n", body):
        lines = p.strip().splitlines()
        # each list item / table row is its own point and may carry its own time
        if lines and all(re.match(r"\s*(\d+[.)、]|[-*+]|\|)", ln) for ln in lines):
            paras += lines
        elif p.strip():
            paras.append(p)
    crowded = [p for p in paras if len(T_RE.findall(T_RUN_RE.sub(lambda x: x.group(1), p))) > 2]
    if crowded:
        out.append(f"有 {len(crowded)} 段挂了 3 个以上时间点：每段（或每个要点）放 1 个，放在这段内容开始的地方即可")
    if "\n## 资料说明" in "\n" + source:
        out.append("“资料说明”由 lw note save 自动生成，草稿里不用写这一节")
    meta = [x.group(0) for x in META_RE.finditer(body)]
    if meta:
        out.append("正文里有交代出处/自证没编造的话（" + "、".join(dict.fromkeys(meta[:6])) + "）："
                   "来源靠排版区分（正文=课堂，[!tip]=AI 补充），素材缺口只在受影响处写一句，删掉这些句子")
    n_contrast = len(CONTRAST_RE.findall(body))
    if n_contrast >= 4:
        out.append(f"“不是……而是/不等于/不要把……”出现 {n_contrast} 次：除非老师本人做了这个对比，直接写正面结论")
    n_f = len(F_RE.findall(source))
    if n_f > 18:
        out.append(f"插了 {n_f} 张截图：只留文字说不清的（最终代码、关键图表/板书、演示过程图），一般 6–15 张")
    if note.get("kind") == "lecture":
        loose = unanchored_sections(source)
        if loose:
            out.append("这些小节没有任何课堂时间点或截图：" + "、".join(f"“{h}”" for h in loose[:6])
                       + "。课时笔记只写这节课讲过的内容：课上讲过的补上时间点；资料里有、课上没讲的不要写进来"
                         "（下次课讲到时再写，或另写 topic 笔记）；你自己的归纳放进 [!tip] AI 补充")
    return out


# Headings that summarise or quiz rather than record a stretch of the class.
_SUMMARY_HEAD = re.compile(r"自测|练习|小结|总结|要点|作业|复习|思考题")


def unanchored_sections(source: str) -> list[str]:
    """Headings (## and deeper) whose section, sub-sections included, cites no
    class time or screenshot: in a lecture note that is content from a
    material or from the writer, not from this lecture."""
    text = re.sub(r"```.*?```", "", source, flags=re.S)
    heads = [(m.start(), m.end(), len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{2,6})\s+(.+)$", text, re.M)]
    out = []
    for i, (_a, b, level, title) in enumerate(heads):
        end = next((h[0] for h in heads[i + 1 :] if h[2] <= level), len(text))
        body = text[b:end]
        if _SUMMARY_HEAD.search(title) or len(re.sub(r"\s", "", body)) < 40:
            continue
        if not (T_RE.search(body) or F_RE.search(body)):
            out.append(title)
    return out


# Words that turn a statement into a claim about logic: spoken explanations
# often state these too strongly or the wrong way round ("必须独立" for a
# sufficient condition), and a fluent note then repeats it as a theorem.
LOGIC_RE = re.compile(r"必须(?!记|掌握|会|背|熟|理解)|才(?:能|有|成立|可以)|只有|当且仅当|充要|充分必要|必要条件|充分条件|\\iff|\\Leftrightarrow|⟺")


def logic_checks(kb: KB, note: dict, source: str) -> list[str]:
    """For maths-type courses: the body sentences that assert a condition,
    listed for verification against the slides (they are not errors)."""
    if note.get("kind") != "lecture" or not note.get("course_id"):
        return []
    c = kb.q1("SELECT profile FROM courses WHERE id=?", note["course_id"])
    if not c or loads(c["profile"], {}).get("subject") != "math":
        return []
    text = re.sub(r"```.*?```", "", source, flags=re.S)
    # only the classroom text (callouts are quotes from materials or AI
    # additions), and only statements: not headings or "必须记住"
    plain = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith((">", "#")))
    out: list[str] = []
    for m in LOGIC_RE.finditer(plain):
        a = max(plain.rfind("\n", 0, m.start()), plain.rfind("。", 0, m.start()), m.start() - 40)
        b = min(x for x in (plain.find("\n", m.end()), plain.find("。", m.end()), m.end() + 40) if x >= 0)
        snippet = T_RE.sub("", plain[a + 1 : b]).strip()
        if snippet and snippet not in out:
            out.append(snippet)
    return out[:12]


def coverage_gaps(segs: list[dict], cited: list[int], radius: int = 180, min_chars: int = 600) -> list[tuple[str, str, int]]:
    """Stretches of transcript with no time point or screenshot of the note
    within ``radius`` seconds, holding at least ``min_chars`` of speech.
    Returns [(from clock, to clock, chars)]."""
    cited = sorted(cited)
    out, cur = [], None
    for sg in sorted((x for x in segs if ":" in x.get("clock", "")), key=lambda x: (x.get("rec", ""), _secs(x["clock"]))):
        t = _secs(sg["clock"])
        near = cited and min(abs(t - c) for c in cited) <= radius
        if near or (cur and cur["rec"] != sg.get("rec")):
            if cur and cur["chars"] >= min_chars:
                out.append((cur["a"], cur["b"], cur["chars"]))
            cur = None
        if not near:
            if cur is None:
                cur = {"rec": sg.get("rec"), "a": sg["clock"][:5], "b": sg["clock"][:5], "chars": 0}
            cur["b"] = sg["clock"][:5]
            cur["chars"] += len(sg.get("text", ""))
    if cur and cur["chars"] >= min_chars:
        out.append((cur["a"], cur["b"], cur["chars"]))
    return out


def _url(rel: str) -> str:
    return "/".join(p.replace(" ", "%20") for p in Path(rel).parts)


def _secs(clock: str) -> int:
    parts = [int(x) for x in clock.lstrip("+").split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


# ---------------------------------------------------------------- frontmatter + provenance
def frontmatter(kb: KB, note: dict, version: int, meta: dict) -> str:
    fm = {"lw_id": note["id"], "lw_version": version, "lw_kind": note["kind"]}
    if note.get("course_id"):
        c = kb.q1("SELECT name FROM courses WHERE id=?", note["course_id"])
        fm["course"] = c["name"] if c else note["course_id"]
    if note.get("lecture_id"):
        l = kb.q1("SELECT * FROM lectures WHERE id=?", note["lecture_id"])
        if l:
            fm.update({"lecture": l["id"], "date": l["date"], "week": l["week"], "teacher": l["teacher"]})
    if meta.get("model"):
        fm["generated_by"] = meta["model"]
    if meta.get("style"):
        fm["style"] = meta["style"]
    fm["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    tags = ["lectureweaver"] + ([f"lw/{note['kind']}"] if note.get("kind") else [])
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, str) and (':' in v or v.startswith(('[', '{'))) else v}")
    lines.append("tags: [" + ", ".join(tags) + "]")
    lines.append("---")
    return "\n".join(lines) + "\n"


def strip_frontmatter(text: str) -> str:
    return FRONT_RE.sub("", text, count=1)


PROV_START = "<!-- lw:sources -->"
PROV_END = "<!-- /lw:sources -->"


def provenance_block(kb: KB, note: dict) -> str:
    if not note.get("lecture_id"):
        return ""
    lec = kb.q1("SELECT * FROM lectures WHERE id=?", note["lecture_id"])
    if not lec:
        return ""
    d = kb.abs(lec["dir"])
    tr = _load(d / "transcript.json", {})
    fr = _load(d / "frames.json", {})
    # A folded callout: available when needed, out of the way when reading.
    items = []
    for i, r in enumerate(_recs_with_windows(kb, lec["id"]), 1):
        src = next((s for s in tr.get("sources", []) if s.get("rec") == r["id"]), {})
        kind = {"platform": "云课堂转写", "asr": f"本地语音识别（{src.get('model', '')}）", "none": "无转写"}.get(src.get("kind", "none"), "无转写")
        if src.get("reason"):
            kind += f"（{src['reason']}）"
        items.append(f"第{i}段录像 {r['start']:%H:%M}–{r['end']:%H:%M}（[回看]({player_url(r['ctid'], r['id'], 0)})）：{kind}")
    nf = len(fr.get("frames", []))
    items.append(f"截图从录屏关键帧中挑选（共 {nf} 张，未保存视频），文字由本地 OCR 识别")
    if lec["match_note"]:
        items.append(f"课表对应：{lec['match_note']}")
    items.append("文中时间可点击，在云课堂从该处播放（需在浏览器登录）。正文是课堂内容，“AI 补充”是课后补充的解释")
    lines = [PROV_START, "> [!info]- 来源", *[f"> - {x}" for x in items], PROV_END]
    return "\n".join(lines)


def assemble(kb: KB, note: dict, version: int, body: str, meta: dict) -> str:
    body = strip_frontmatter(body).strip()
    body = re.sub(re.escape(PROV_START) + r".*?" + re.escape(PROV_END), "", body, flags=re.S).strip()
    prov = provenance_block(kb, note)
    return frontmatter(kb, note, version, meta) + "\n" + body + ("\n\n" + prov if prov else "") + "\n"


# ---------------------------------------------------------------- note records
def note_folder(kb: KB, note: dict) -> str:
    if note.get("course_id"):
        c = kb.q1("SELECT name, term FROM courses WHERE id=?", note["course_id"])
        if c:
            return safe_name(c["name"])
    return "LectureWeaver"


def default_title(kb: KB, note: dict, body: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if m:
        return m.group(1).strip()
    if note.get("lecture_id"):
        l = kb.q1("SELECT * FROM lectures WHERE id=?", note["lecture_id"])
        if l:
            return f"{l['date']} 第{l['week']}周"
    return note["id"]


def get_or_create(kb: KB, kind: str, lecture_id: str | None = None, course_id: str | None = None, key: str | None = None) -> dict:
    if kind == "lecture":
        if not lecture_id:
            raise NoteError("课时笔记需要 --lecture")
        nid = f"N-{lecture_id}"
        lec = kb.q1("SELECT course_id FROM lectures WHERE id=?", lecture_id)
        if not lec:
            raise NoteError(f"找不到课时 {lecture_id}")
        course_id = lec["course_id"]
    else:
        if not key:
            raise NoteError("非课时笔记需要 --key（如 第7周复习、关系代数除法）")
        nid = f"N-{kind}-{hashlib.sha1(((course_id or '') + key).encode()).hexdigest()[:10]}"
    r = kb.q1("SELECT * FROM notes WHERE id=?", nid)
    if r:
        return dict(r)
    now = time.time()
    kb.x("INSERT INTO notes(id,kind,course_id,lecture_id,title,current_version,created_at,updated_at) VALUES(?,?,?,?,?,0,?,?)",
         nid, kind, course_id, lecture_id, key, now, now)
    kb.db.commit()
    return dict(kb.q1("SELECT * FROM notes WHERE id=?", nid))


def _store_version(kb: KB, note: dict, text: str, author: str, meta: dict) -> int:
    v = (kb.q1("SELECT MAX(version) AS v FROM note_versions WHERE note_id=?", note["id"])["v"] or 0) + 1
    rel = f"notes/{note['id']}/v{v:04d}-{author}.md"
    p = kb.abs(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    kb.x(
        "INSERT INTO note_versions(note_id,version,author,model,style,message,path,sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        note["id"], v, author, meta.get("model"), meta.get("style"), meta.get("message"), rel, sha(text), time.time(),
    )
    kb.x("UPDATE notes SET current_version=?, updated_at=? WHERE id=?", v, time.time(), note["id"])
    kb.db.commit()
    kb.index_replace("note", note["id"], [(strip_frontmatter(text), note.get("lecture_id"), note.get("course_id"), None)])
    return v


def version_text(kb: KB, note_id: str, version: int) -> str:
    r = kb.q1("SELECT path FROM note_versions WHERE note_id=? AND version=?", note_id, version)
    if not r:
        raise NoteError(f"{note_id} 没有版本 {version}")
    return kb.abs(r["path"]).read_text("utf-8")


def exported_text(kb: KB, note: dict) -> str | None:
    if not note.get("exported_version"):
        return None
    return version_text(kb, note["id"], note["exported_version"])


# ---------------------------------------------------------------- sync-in of user edits
def sync_in(kb: KB, note: dict) -> str:
    """Record edits the user made in the vault. Returns: none|edited|missing|unexported."""
    if not note.get("vault_path"):
        return "unexported"
    p = notes_root() / note["vault_path"]
    if not p.exists():
        return "missing"
    cur = p.read_text("utf-8")
    if sha(cur) == note.get("exported_hash"):
        return "none"
    # Record each distinct hand-edited state once.
    if not kb.q1("SELECT 1 FROM note_versions WHERE note_id=? AND sha256=?", note["id"], sha(cur)):
        _store_version(kb, note, cur, "user", {"message": "在笔记软件中的修改"})
    return "edited"


def user_added_lines(base: str, user: str) -> list[str]:
    """Lines the user added or rewrote relative to what we exported."""
    b = strip_frontmatter(base).splitlines()
    u = strip_frontmatter(user).splitlines()
    out = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, b, u, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            for ln in u[j1:j2]:
                s = _norm(ln)
                if len(s) >= 4:
                    out.append(ln.strip())
    return out


def user_removed_lines(base: str, user: str) -> list[str]:
    b = strip_frontmatter(base).splitlines()
    u = strip_frontmatter(user).splitlines()
    out = []
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, b, u, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            out += [ln.strip() for ln in b[i1:i2] if len(_norm(ln)) >= 4]
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).strip("-*>#| ")


def missing_user_lines(added: list[str], new_text: str) -> list[str]:
    hay = _norm(new_text)
    return [ln for ln in added if _norm(ln) not in hay]


def three_way_merge(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """git merge-file; returns (text, clean)."""
    git = shutil.which("git")
    if not git:
        return theirs, False
    with tempfile.TemporaryDirectory() as td:
        fo, fb, ft = (Path(td) / n for n in ("ours", "base", "theirs"))
        fo.write_text(ours, "utf-8")
        fb.write_text(base, "utf-8")
        ft.write_text(theirs, "utf-8")
        r = subprocess.run([git, "merge-file", "-p", "--diff3", "-L", "我的修改", "-L", "上次导出", "-L", "新版本", str(fo), str(fb), str(ft)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.stdout, r.returncode == 0


# ---------------------------------------------------------------- save / export
@dataclass
class SaveOutcome:
    note_id: str
    version: int
    exported: bool
    vault_path: str | None
    proposal_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    missing_user_lines: list[str] = field(default_factory=list)
    message: str = ""


def save(kb: KB, note: dict, draft: str, meta: dict, mode: str = "safe", title: str | None = None, allow_drop: bool = False) -> SaveOutcome:
    """Validate + store a new AI version and export it if that is safe.

    mode: ``safe`` (proposal if the user edited), ``merge`` (3-way merge,
    proposal on conflict) or ``replace`` (explicit overwrite; the user's
    version stays in history).
    """
    root = notes_root()
    note = dict(kb.q1("SELECT * FROM notes WHERE id=?", note["id"]))  # never trust a stale copy
    state = sync_in(kb, note)
    note = dict(kb.q1("SELECT * FROM notes WHERE id=?", note["id"]))
    if title:
        kb.x("UPDATE notes SET title=? WHERE id=?", title, note["id"])
        note["title"] = title
    if not note.get("title") or note["title"] == note["id"]:
        note["title"] = default_title(kb, note, draft)
        kb.x("UPDATE notes SET title=? WHERE id=?", note["title"], note["id"])
    vault_rel = note.get("vault_path") or _vault_rel(kb, note)
    note_path = root / vault_rel
    rr = render(kb, note, draft, note_path, root)
    if rr.errors:
        raise NoteError("笔记草稿有问题，未保存：\n- " + "\n- ".join(rr.errors))
    version = (kb.q1("SELECT MAX(version) AS v FROM note_versions WHERE note_id=?", note["id"])["v"] or 0) + 1
    final = assemble(kb, note, version, rr.text, meta)
    _store_version(kb, note, final, "ai", meta)
    # Keep the draft with its {t}/{f}/{m} markup, so the note can be rendered
    # again (e.g. after a layout change) without asking a model to rewrite it.
    kb.abs(f"notes/{note['id']}/v{version:04d}-ai.draft.md").write_text(draft, "utf-8")
    _copy_attachments(rr.attachments, root / note_folder(kb, note))
    out = SaveOutcome(note["id"], version, False, vault_rel, warnings=rr.warnings)

    if state in ("unexported", "missing", "none") or mode == "replace":
        if state == "missing" and note.get("vault_path"):
            out.warnings.append("笔记文件在笔记目录中被删除了，已重新写出")
        _write_export(kb, note, vault_rel, final, version)
        out.exported = True
        out.message = "已写入笔记目录" + ("（覆盖了你的修改，旧内容仍在版本历史中）" if state == "edited" else "")
        return out

    # The user edited the exported file.
    user_text = (root / vault_rel).read_text("utf-8")
    base = exported_text(kb, note) or ""
    added = user_added_lines(base, user_text)
    missing = missing_user_lines(added, final)
    if mode == "merge":
        merged, clean = three_way_merge(base, user_text, final)
        if clean and not missing_user_lines(added, merged):
            mv = _store_version(kb, note, merged, "merge", {"message": f"合并你的修改与 v{version}"})
            _write_export(kb, note, vault_rel, merged, mv)
            out.version, out.exported = mv, True
            out.message = "已合并：保留了你的修改，同时应用了新版本"
            return out
        out.missing_user_lines = missing
        prop = _write_proposal(root, vault_rel, merged if not clean else final, conflict=not clean)
        out.proposal_path = prop
        out.message = "存在冲突：你的笔记未被改动，合并结果（含冲突标记）放在旁边的 .lw-proposed.md"
        return out
    out.missing_user_lines = [] if allow_drop else missing
    removed = user_removed_lines(base, user_text)
    revived = [ln for ln in removed if _norm(ln) and _norm(ln) in _norm(final) and _norm(ln) not in _norm(user_text)]
    if (not missing and not revived) or allow_drop:
        # Everything the user wrote is in the new text and nothing they deleted
        # comes back (or the user explicitly allowed it).
        _write_export(kb, note, vault_rel, final, version)
        out.exported = True
        out.message = "你的修改都已包含在新版本中，已更新笔记" if not allow_drop else "已按你的明确要求更新（旧内容在版本历史中）"
        return out
    out.proposal_path = _write_proposal(root, vault_rel, final, conflict=False)
    out.message = (
        "你在笔记软件里改过这篇笔记，为避免覆盖，新版本放在旁边的 .lw-proposed.md；"
        "可用 lw note diff / lw note accept / lw note save --merge 处理"
    )
    return out


def _vault_rel(kb: KB, note: dict) -> str:
    folder = note_folder(kb, note)
    if note["kind"] == "lecture":
        l = kb.q1("SELECT * FROM lectures WHERE id=?", note["lecture_id"])
        name = f"{l['date']} 第{l['week'] or '?'}周 {safe_name(note['title'] or '', 50)}".strip()
        if note["title"] and note["title"].startswith(l["date"] or "###"):
            name = safe_name(note["title"])
    else:
        sub = {"review": "复习", "cards": "知识卡片", "topic": "专题", "explain": "讲解", "index": ""}.get(note["kind"], note["kind"])
        name = (sub + "/" if sub else "") + safe_name(note["title"] or note["id"])
    rel = f"{folder}/{name}.md"
    n = 2
    while (notes_root() / rel).exists() and not kb.q1("SELECT 1 FROM notes WHERE vault_path=? AND id=?", rel, note["id"]):
        rel = f"{folder}/{name} ({n}).md"
        n += 1
    return rel


def _write_export(kb: KB, note: dict, rel: str, text: str, version: int) -> None:
    p = notes_root() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.lw-tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, p)
    kb.x("UPDATE notes SET vault_path=?, exported_version=?, exported_hash=?, updated_at=? WHERE id=?", rel, version, sha(text), time.time(), note["id"])
    kb.db.commit()
    prop = p.with_name(p.stem + ".lw-proposed.md")
    if prop.exists():
        prop.unlink()


def _write_proposal(root: Path, rel: str, text: str, conflict: bool) -> str:
    p = root / rel
    prop = p.with_name(p.stem + ".lw-proposed.md")
    header = "<!-- LectureWeaver 新版本提案：你的原笔记未被修改。" + ("包含冲突标记 <<<<<<< ======= >>>>>>>，请手动选择。" if conflict else "") + " 处理：lw note accept / lw note diff -->\n"
    prop.write_text(header + text, "utf-8")
    return str(prop.relative_to(root))


def _copy_attachments(atts: list[tuple[Path, str]], folder: Path) -> None:
    for src, rel in atts:
        dst = folder / rel
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def accept_proposal(kb: KB, note: dict) -> SaveOutcome:
    note = dict(kb.q1("SELECT * FROM notes WHERE id=?", note["id"]))
    """Take the latest AI version over the user's (user version stays in history)."""
    sync_in(kb, note)
    note = dict(kb.q1("SELECT * FROM notes WHERE id=?", note["id"]))
    r = kb.q1("SELECT version FROM note_versions WHERE note_id=? AND author IN ('ai','rollback') ORDER BY version DESC LIMIT 1", note["id"])
    if not r:
        raise NoteError("没有可接受的新版本")
    text = version_text(kb, note["id"], r["version"])
    _write_export(kb, note, note["vault_path"] or _vault_rel(kb, note), text, r["version"])
    return SaveOutcome(note["id"], r["version"], True, note["vault_path"], message=f"已采用 v{r['version']}；你之前的版本仍可在历史中找到")


def rollback(kb: KB, note: dict, to_version: int) -> SaveOutcome:
    note = dict(kb.q1("SELECT * FROM notes WHERE id=?", note["id"]))
    sync_in(kb, note)
    note = dict(kb.q1("SELECT * FROM notes WHERE id=?", note["id"]))
    text = version_text(kb, note["id"], to_version)
    v = _store_version(kb, note, text, "rollback", {"message": f"回滚到 v{to_version}"})
    rel = note["vault_path"] or _vault_rel(kb, note)
    _write_export(kb, note, rel, text, v)
    return SaveOutcome(note["id"], v, True, rel, message=f"已回滚到 v{to_version}（记为新版本 v{v}，历史未删除）")


def history(kb: KB, note_id: str) -> list[dict]:
    return [dict(r) for r in kb.q("SELECT version,author,model,style,message,created_at,sha256 FROM note_versions WHERE note_id=? ORDER BY version", note_id)]


def diff(kb: KB, note: dict, a: int | None, b: int | None) -> str:
    note = dict(kb.q1("SELECT * FROM notes WHERE id=?", note["id"]))
    sync_in(kb, note)
    hist = history(kb, note["id"])
    if not hist:
        return ""
    b = b or hist[-1]["version"]
    a = a or (hist[-2]["version"] if len(hist) > 1 else b)
    ta, tb = version_text(kb, note["id"], a), version_text(kb, note["id"], b)
    return "".join(difflib.unified_diff(ta.splitlines(True), tb.splitlines(True), f"v{a}", f"v{b}"))


def reexport_all(kb: KB) -> dict:
    """Write every note's exported version to the (possibly new) notes dir.

    Used after restoring a backup on another Mac or changing the notes dir.
    Existing files that differ are not overwritten (a proposal is written).
    """
    done = skipped = 0
    root = notes_root()
    for n in kb.q("SELECT * FROM notes WHERE exported_version IS NOT NULL"):
        n = dict(n)
        text = version_text(kb, n["id"], n["exported_version"])
        # re-render attachments + links for this machine's paths
        lec_frames = re.findall(r"attachments/([^/]+)/(F\d+)\.webp", text)
        for lid, fid in set(lec_frames):
            l = kb.q1("SELECT dir FROM lectures WHERE id=?", lid)
            if l:
                src = kb.abs(l["dir"]) / "frames" / f"{fid}.webp"
                if src.exists():
                    _copy_attachments([(src, f"attachments/{lid}/{fid}.webp")], root / note_folder(kb, n))
        text = _relink_materials(kb, text)
        p = root / n["vault_path"]
        if p.exists() and sha(p.read_text("utf-8")) not in (sha(text), n["exported_hash"]):
            _write_proposal(root, n["vault_path"], text, conflict=False)
            skipped += 1
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")
        kb.x("UPDATE notes SET exported_hash=? WHERE id=?", sha(text), n["id"])
        done += 1
    kb.db.commit()
    return {"written": done, "kept_user_files": skipped}


_LINK_T = re.compile(r"\[(\d{1,2}:\d{2})\]\(https://[^)\s]*[?&]videoId=([^&)\s]+)&latestWatchTime=(\d+)\)")
_LINK_F = re.compile(r"!\[(F\d{3,}) · [^\]]*\]\([^)]*\)")
_LINK_M = re.compile(r"\[《[^》]*》\s*([^\]]*)\]\(file://[^)\s]*/materials/(m_[0-9a-f]+)/[^)#\s]*(?:#page=(\d+))?\)")


def to_draft(kb: KB, note: dict, text: str) -> str:
    """The inverse of render(): a saved note (with the user's edits) back in
    draft markup, so it can be revised and saved again."""
    recs = {r["id"]: r for r in _recs_with_windows(kb, note["lecture_id"])} if note.get("lecture_id") else {}

    def t_back(m):
        r = recs.get(m.group(2))
        if not r:
            return m.group(0)
        return f"{{t {(r['start'] + timedelta(seconds=int(m.group(3)))):%H:%M:%S}}}"

    def m_back(m):
        loc = m.group(1).strip() or (f"p.{m.group(3)}" if m.group(3) else "")
        return f"{{m {m.group(2)}{(' ' + loc) if loc else ''}}}"

    body = strip_frontmatter(text)
    body = re.sub(re.escape(PROV_START) + r".*?" + re.escape(PROV_END) + r"\n?", "", body, flags=re.S)
    body = _LINK_F.sub(lambda m: f"{{f {m.group(1)}}}", body)
    body = _LINK_T.sub(t_back, body)
    body = _LINK_M.sub(m_back, body)
    return body.strip() + "\n"


def _relink_materials(kb: KB, text: str) -> str:
    """Material links are absolute file URLs; point them at this machine's KB."""
    def fix(m):
        mid = m.group(1)
        r = kb.q1("SELECT path FROM materials WHERE id=?", mid)
        return m.group(0) if not r else kb.abs(r["path"]).as_uri() + (m.group(2) or "")

    return re.sub(r"file://[^)\s]*/materials/(m_[0-9a-f]+)/[^)#\s]*(#page=\d+)?", fix, text)
