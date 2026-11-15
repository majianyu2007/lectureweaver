"""Google NotebookLM source builder and one-way incremental sync.

Design boundaries:
* LectureWeaver prepares clean, traceable sources per course (1 course guide +
  cleaned Markdown notes + converted/OCR-backed course materials) and syncs
  them one-way into NotebookLM (``web`` backend via ``notebooklm-py``, or
  ``gdrive`` fallback via official OAuth ``drive.file`` Google Docs).
* Credentials never enter ``kb.sqlite`` or backups: browser storage lives in
  ``secrets_dir() / "notebooklm_storage.json"`` (0600), and Google Drive OAuth
  tokens live in the OS Keychain via :mod:`lectureweaver.secrets`.
* Incremental sync tracks ``(course_id, backend, source_key) -> (remote_source_id, sha256)``
  in ``kb.sqlite``. Only sources created by LectureWeaver are updated or removed;
  sources added manually by the user in NotebookLM are never touched.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html.parser
import http.server
import inspect
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .config import MachineConfig, find_soffice, nb_cache_dir, outbound_proxy, secrets_dir
from .consent import check as check_consent
from .kb import KB, loads, try_lock
from .learning import STATE_NAMES, overview as learning_overview
from .materials import CODE_EXT, is_garbled, sha256_file
from .notes import NoteError, PROV_END, PROV_START, safe_name, strip_frontmatter, sync_in, version_text
from .secrets import delete_secret, get_secret, set_secret, write_private

GRANULARITIES = ("auto", "lecture", "week", "stage", "course")
BACKENDS = ("web", "gdrive")
CONSENT_SCOPES = ["notes", "materials", "learning"]
MAX_PDF_BYTES = 180 * 1024 * 1024
MAX_PDF_PAGES = 450
MAX_MD_BYTES = 1_200_000

GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GDRIVE_CLIENT_KEY = "gdrive:client_config"
GDRIVE_TOKEN_KEY = "gdrive:token"


class NotebookLMIntegrationError(Exception):
    """User-facing error raised by NotebookLM build/login/sync operations."""


@dataclass
class SourceItem:
    key: str
    title: str
    filename: str
    kind: str  # guide | note | material
    mime_type: str
    sha256: str
    content_bytes: bytes | None = None
    file_path: Path | None = None

    def write_to(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.content_bytes is not None:
            dest.write_bytes(self.content_bytes)
        elif self.file_path is not None:
            shutil.copy2(self.file_path, dest)
        else:
            raise NotebookLMIntegrationError(f"信源 {self.key} 没有可用内容")
        return dest


@dataclass
class CourseBundle:
    course_id: str
    course_name: str
    term: str
    notebook_title: str
    granularity: str
    items: list[SourceItem]
    warnings: list[str] = field(default_factory=list)
    note_count: int = 0
    material_count: int = 0


# ---------------------------------------------------------------- proxy helper
@contextlib.contextmanager
def with_outbound_proxy() -> Iterator[str | None]:
    """Temporarily expose ``outbound_proxy()`` via env vars for Google/Playwright
    while keeping campus ``*.nwafu.edu.cn`` hosts direct."""
    proxy = outbound_proxy()
    saved: dict[str, str | None] = {}
    if proxy:
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
            saved[k] = os.environ.get(k)
        if not os.environ.get("HTTPS_PROXY") and not os.environ.get("https_proxy"):
            os.environ["HTTPS_PROXY"] = proxy
        if not os.environ.get("HTTP_PROXY") and not os.environ.get("http_proxy"):
            os.environ["HTTP_PROXY"] = proxy
        no_proxy_parts = [x.strip() for x in (os.environ.get("NO_PROXY") or "").split(",") if x.strip()]
        for bypass in (".nwafu.edu.cn", ".nwsuaf.edu.cn", "127.0.0.1", "localhost"):
            if bypass not in no_proxy_parts:
                no_proxy_parts.append(bypass)
        os.environ["NO_PROXY"] = ",".join(no_proxy_parts)
    try:
        yield proxy
    finally:
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val


# ---------------------------------------------------------------- markdown cleaning
_FENCE_RE = re.compile(r"(^```[^\n]*\n.*?^```[ \t]*$|^~~~[^\n]*\n.*?^~~~[ \t]*$)", re.M | re.S)
_INLINE_CODE_RE = re.compile(r"(`+[^`\n]+?`+)")
_CAMPUS_TIME_LINK_RE = re.compile(
    r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]\(https?://[^)\s]*(?:nwafu|nwsuaf)\.edu\.cn[^)\s]*\)"
)
_CAMPUS_ANY_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(https?://[^)\s]*(?:nwafu|nwsuaf)\.edu\.cn[^)\s]*\)"
)
_FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file://[^)\s]*\)")
_MD_LOCAL_IMG_RE = re.compile(r"!\[([^\]]*)\]\((?!(?:https?|data):)[^)\s]+\)")
_WIKI_IMG_RE = re.compile(
    r"!\[\[([^\]|]+?\.(?:webp|png|jpe?g|gif|svg|bmp))(?:\|([^\]]*))?\]\]",
    re.I,
)
_PROV_BLOCK_RE = re.compile(re.escape(PROV_START) + r".*?" + re.escape(PROV_END) + r"[ \t]*\n?", re.S)


def clean_note_markdown(text: str, lecture_date: str | None = None) -> str:
    """Clean a saved note for NotebookLM while preserving code blocks and LaTeX."""
    body = strip_frontmatter(text)
    body = _PROV_BLOCK_RE.sub("", body)

    placeholders: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append(m.group(0))
        return f"\x00LWCODE{idx}\x00"

    protected = _FENCE_RE.sub(_stash, body)
    protected = _INLINE_CODE_RE.sub(_stash, protected)

    def _time_repl(m: re.Match[str]) -> str:
        clock = m.group(1)
        return f"[{lecture_date} {clock}]" if lecture_date else f"[{clock}]"

    protected = _CAMPUS_TIME_LINK_RE.sub(_time_repl, protected)
    protected = _CAMPUS_ANY_LINK_RE.sub(r"\1", protected)
    protected = _FILE_LINK_RE.sub(r"\1", protected)

    def _md_img_repl(m: re.Match[str]) -> str:
        alt = m.group(1).strip()
        return f"[课堂截图：{alt}]" if alt else "[课堂截图]"

    def _wiki_img_repl(m: re.Match[str]) -> str:
        target, alias = m.group(1).strip(), (m.group(2) or "").strip()
        label = alias or Path(target).stem
        return f"[课堂截图：{label}]"

    protected = _MD_LOCAL_IMG_RE.sub(_md_img_repl, protected)
    protected = _WIKI_IMG_RE.sub(_wiki_img_repl, protected)

    def _unstash(s: str) -> str:
        # Inline code may have been stashed after fenced blocks; restore in reverse.
        for idx in range(len(placeholders) - 1, -1, -1):
            s = s.replace(f"\x00LWCODE{idx}\x00", placeholders[idx])
        return s

    cleaned = _unstash(protected).strip()
    return cleaned + "\n"


# ---------------------------------------------------------------- course guide
def _fmt_clock(sec: float | int | None) -> str:
    if sec is None or sec < 0:
        return ""
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s2 = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s2:02d}" if s2 else f"{h:02d}:{m:02d}"


def build_course_guide(kb: KB, course: dict) -> str:
    """Generate ``00-课程导读与知识点索引.md`` for a course."""
    cid = course["id"]
    prof = loads(course.get("profile"), {}) or {}
    aliases = loads(course.get("aliases"), []) or []

    note_rows = kb.q(
        """SELECT n.*, l.date AS lec_date, l.week AS lec_week
           FROM notes n LEFT JOIN lectures l ON l.id=n.lecture_id
           WHERE n.course_id=? AND n.current_version > 0
           ORDER BY CASE WHEN l.date IS NULL THEN 1 ELSE 0 END, l.date, l.start_period, n.created_at""",
        cid,
    )
    mat_rows = _course_materials(kb, cid)
    learn_items = learning_overview(kb, cid)

    lines = [
        f"# {course['name']} · 课程导读与知识点索引",
        "",
        "## 一、课程概况与约定",
        "",
        f"- **课程名称**：{course['name']}" + (f"（别名：{'、'.join(aliases)}）" if aliases else ""),
        f"- **学期**：{course['term']}",
    ]
    if course.get("code"):
        lines.append(f"- **课程号**：{course['code']}")
    if course.get("teacher"):
        lines.append(f"- **任课教师**：{course['teacher']}")
    if prof.get("subject"):
        lines.append(f"- **学科类型**：{prof['subject']}")
    if prof.get("note_prefs"):
        lines.append(f"- **学习与符号约定**：{prof['note_prefs']}")
    lines.append(
        f"- **信源规模**：课堂及专题笔记 {len(note_rows)} 篇，关联课程资料 {len(mat_rows)} 份，已登记知识点 {len(learn_items)} 个"
    )
    lines.append("")

    # Section 2: Learning weak spots & misconceptions
    lines.append("## 二、当前重点关注（薄弱点与待解决误区）")
    lines.append("")
    focus = [
        it for it in learn_items
        if it["open_misconceptions"] or it["state"] in ("shaky", "introduced")
    ]
    if not focus:
        lines.append("当前暂无标记为“不稳”或“未解决误解”的知识点。")
    else:
        for it in focus:
            badge = it["state_name"]
            lines.append(f"- **{it['name']}**（{badge}）：{it['rationale']}")
            for mis in it["open_misconceptions"]:
                lines.append(f"  - 待纠正误解：{mis}")
    lines.append("")

    # Section 3: Lecture & note timeline
    lines.append("## 三、课堂与专题笔记目录")
    lines.append("")
    if not note_rows:
        lines.append("暂无已保存的课堂笔记。")
    else:
        for n in note_rows:
            title = n["title"] or n["id"]
            if n["kind"] == "lecture" and n["lec_date"]:
                wk = f"第{n['lec_week']}周" if n["lec_week"] else "课时"
                lines.append(f"- `{n['lec_date']}`（{wk}）：**{title}**")
            else:
                lines.append(f"- [{n['kind']}] **{title}**")
    lines.append("")

    # Section 4: Materials list
    if mat_rows:
        lines.append("## 四、关联课程资料清单")
        lines.append("")
        for m in mat_rows:
            roles = sorted({
                r["role"] for r in kb.q(
                    "SELECT role FROM material_links WHERE material_id=? AND role IS NOT NULL",
                    m["id"],
                ) if r["role"]
            })
            role_str = f"（{', '.join(roles)}）" if roles else ""
            lines.append(f"- 《{m['title'] or m['filename']}》{role_str} — {m['kind']}，共 {m['units'] or '?'} 页/段")
        lines.append("")

    # Section 5: Concept index with lecture timestamps
    lines.append("## 五、知识点索引与课堂出处")
    lines.append("")
    if not learn_items:
        lines.append("暂未登记结构化知识点（在日常辅导答疑后会自动积累）。")
    else:
        for it in learn_items:
            c_row = kb.q1("SELECT summary, aliases FROM concepts WHERE id=?", it["id"])
            summary = f" — {c_row['summary']}" if c_row and c_row["summary"] else ""
            c_aliases = loads(c_row["aliases"], []) if c_row else []
            alias_txt = f"（又称：{'、'.join(c_aliases)}）" if c_aliases else ""
            refs = kb.q(
                """SELECT cr.t, l.date, l.week FROM concept_refs cr
                   JOIN lectures l ON l.id=cr.lecture_id
                   WHERE cr.concept_id=? ORDER BY l.date, cr.t""",
                it["id"],
            )
            ref_parts = []
            for rf in refs:
                clk = _fmt_clock(rf["t"])
                wk = f"W{rf['week']:02d}" if rf["week"] else ""
                ref_parts.append(f"{rf['date']} {wk} {clk}".strip())
            where_txt = f" | 出处：{'；'.join(ref_parts)}" if ref_parts else ""
            state_txt = STATE_NAMES.get(it["state"], it["state"])
            lines.append(f"- **{it['name']}**{alias_txt} [{state_txt}]{summary}{where_txt}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- materials
def _course_materials(kb: KB, course_id: str) -> list[dict]:
    rows = kb.q(
        """SELECT DISTINCT m.* FROM materials m
           JOIN material_links ml ON ml.material_id=m.id
           WHERE (ml.target_type='course' AND ml.target_id=?)
              OR (ml.target_type='lecture' AND ml.target_id IN (SELECT id FROM lectures WHERE course_id=?))
           ORDER BY m.title, m.id""",
        course_id,
        course_id,
    )
    return [dict(r) for r in rows]


def _pdf_has_garbled_layer(pdf_path: Path) -> bool:
    """Detect custom-encoded/mojibake PDF text layers (fast sample of up to 8 pages)."""
    try:
        from pypdf import PdfReader

        rd = PdfReader(str(pdf_path))
        pages = rd.pages
        if not pages:
            return False
        step = max(1, len(pages) // 8)
        sample = [(pages[i].extract_text() or "").strip() for i in range(0, len(pages), step)][:8]
        non_empty = [t for t in sample if len(t) >= 20]
        if not non_empty:
            return False
        garbled = sum(1 for t in non_empty if is_garbled(t))
        return garbled / len(non_empty) >= 0.35
    except Exception:  # noqa: BLE001
        return False


def _convert_pptx_to_pdf(pptx_path: Path, sha256_hex: str) -> Path | None:
    """Convert ``.pptx`` to ``.pdf`` via headless LibreOffice, cached by SHA-256."""
    cached = nb_cache_dir() / f"pptx-{sha256_hex}.pdf"
    if cached.is_file() and cached.stat().st_size > 0:
        return cached
    soffice = find_soffice()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory(prefix="lw-soffice-") as td:
        td_path = Path(td)
        profile_uri = (td_path / "profile").as_uri()
        out_dir = td_path / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile_uri}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(out_dir),
                    str(pptx_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        produced = out_dir / (pptx_path.stem + ".pdf")
        if not produced.is_file() or produced.stat().st_size == 0:
            pdfs = list(out_dir.glob("*.pdf"))
            produced = pdfs[0] if pdfs else produced
        if produced.is_file() and produced.stat().st_size > 0:
            tmp_dst = cached.with_suffix(".pdf.lw-tmp")
            shutil.copy2(produced, tmp_dst)
            os.replace(tmp_dst, cached)
            return cached
    return None


def _split_large_pdf(pdf_path: Path, sha256_hex: str) -> list[Path]:
    """Return ``[pdf_path]`` unless it exceeds NotebookLM size/page limits."""
    from pypdf import PdfReader, PdfWriter

    size = pdf_path.stat().st_size
    try:
        rd = PdfReader(str(pdf_path))
        total_pages = len(rd.pages)
    except Exception:  # noqa: BLE001
        return [pdf_path]
    if size <= MAX_PDF_BYTES and total_pages <= MAX_PDF_PAGES:
        return [pdf_path]

    n_parts = max(
        (size + MAX_PDF_BYTES - 1) // MAX_PDF_BYTES,
        (total_pages + MAX_PDF_PAGES - 1) // MAX_PDF_PAGES,
        2,
    )
    chunk = (total_pages + n_parts - 1) // n_parts
    out: list[Path] = []
    for idx, start in enumerate(range(0, total_pages, chunk), 1):
        part_path = nb_cache_dir() / f"pdf-{sha256_hex}-p{idx}of{n_parts}.pdf"
        if not part_path.is_file() or part_path.stat().st_size == 0:
            wr = PdfWriter()
            for pg in rd.pages[start : start + chunk]:
                wr.add_page(pg)
            tmp = part_path.with_suffix(".pdf.lw-tmp")
            with open(tmp, "wb") as f:
                wr.write(f)
            os.replace(tmp, part_path)
        out.append(part_path)
    return out


def _extract_embedded_json_sources(html_text: str) -> list[tuple[str, str, str]]:
    """Extract clean source-code snippets from ``<script type="application/json">``
    blocks inside interactive single-page HTML handouts, skipping Pygments HTML
    spans and base64 archives."""
    extracted: list[tuple[str, str, str]] = []
    seen_code: set[str] = set()

    def _walk(node: Any, hint: str = "") -> None:
        if isinstance(node, dict):
            label = str(node.get("label") or node.get("title") or node.get("filename") or hint or "").strip()
            fname = str(node.get("filename") or "").strip()
            lexer = str(node.get("lexer") or CODE_EXT.get(Path(fname).suffix.lower(), "")).strip()
            for code_key in ("source", "code", "demo"):
                val = node.get(code_key)
                if isinstance(val, str):
                    code = val.strip()
                    if 20 <= len(code) <= 200_000 and not code.startswith("data:") and "<span " not in code:
                        sig = hashlib.sha256(code.encode("utf-8")).hexdigest()
                        if sig not in seen_code:
                            seen_code.add(sig)
                            heading = f"{label} ({fname})" if (label and fname and fname not in label) else (label or fname or code_key)
                            if code_key == "demo" and "demoName" in node:
                                heading = f"{label} 示例 ({node['demoName']})"
                            extracted.append((heading, lexer, code))
            for k, v in node.items():
                if isinstance(k, str) and (k.lower().endswith(("html", "zip", "base64")) or k == "parts"):
                    continue
                _walk(v, k if isinstance(k, str) else hint)
        elif isinstance(node, list):
            for item in node:
                _walk(item, hint)

    for attrs, body in re.findall(r"<script\b([^>]*)>([\s\S]*?)</script>", html_text, re.I):
        if "json" not in attrs.lower():
            continue
        with contextlib.suppress(Exception):
            _walk(json.loads(body))
    return extracted


class _HtmlToMarkdownParser(html.parser.HTMLParser):
    _SKIP_TAGS = {"style", "script", "noscript", "svg", "button", "select", "option"}
    _BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "figcaption", "summary", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_pre = 0
        self._in_code = 0
        self._in_table = 0
        self._cur_row: list[str] | None = None
        self._cur_cell: list[str] | None = None
        self._row_is_header = False
        self._table_has_header = False
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            lvl = int(t[1])
            self.out.append(f"\n\n{'#' * lvl} ")
        elif t == "pre":
            self._in_pre += 1
            self.out.append("\n\n```\n")
        elif t == "code" and not self._in_pre:
            self._in_code += 1
            (self._cur_cell if self._cur_cell is not None else self.out).append("`")
        elif t == "li":
            self.out.append("\n- ")
        elif t == "br":
            if self._cur_cell is not None:
                self._cur_cell.append(" / ")
            else:
                self.out.append("\n")
        elif t == "table":
            self._in_table += 1
            self._table_has_header = False
            self.out.append("\n\n")
        elif t == "tr" and self._in_table:
            self._cur_row = []
            self._row_is_header = False
        elif t in ("th", "td") and self._cur_row is not None:
            if t == "th":
                self._row_is_header = True
            self._cur_cell = []
        elif t in self._BLOCK_TAGS and self._cur_cell is None and not self._in_pre:
            self.out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n")
        elif t == "pre" and self._in_pre:
            self._in_pre -= 1
            self.out.append("\n```\n\n")
        elif t == "code" and self._in_code and not self._in_pre:
            self._in_code -= 1
            (self._cur_cell if self._cur_cell is not None else self.out).append("`")
        elif t in ("th", "td") and self._cur_cell is not None and self._cur_row is not None:
            cell_txt = re.sub(r"\s+", " ", "".join(self._cur_cell)).strip().replace("|", "\\|")
            self._cur_row.append(cell_txt)
            self._cur_cell = None
        elif t == "tr" and self._cur_row is not None:
            if any(self._cur_row):
                self.out.append("| " + " | ".join(self._cur_row) + " |\n")
                if self._row_is_header and not self._table_has_header:
                    self.out.append("| " + " | ".join(["---"] * len(self._cur_row)) + " |\n")
                    self._table_has_header = True
            self._cur_row = None
        elif t == "table" and self._in_table:
            self._in_table -= 1
            self.out.append("\n")
        elif t in self._BLOCK_TAGS and self._cur_cell is None and not self._in_pre:
            self.out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._cur_cell is not None:
            self._cur_cell.append(data)
        elif self._in_pre:
            self.out.append(data)
        else:
            cleaned = re.sub(r"[ \t\r\f\v]+", " ", data)
            if "\n" in cleaned:
                cleaned = re.sub(r"\s*\n\s*", " ", cleaned)
            self.out.append(cleaned)


def html_to_clean_markdown(html_text: str, title: str) -> str:
    """Convert an HTML handout/page into clean Markdown without CSS/JS/base64 bloat."""
    embedded_code = _extract_embedded_json_sources(html_text)
    stripped = re.sub(r"<!--[\s\S]*?-->", "", html_text)
    stripped = re.sub(r"<(style|script|noscript|svg)\b[^>]*>[\s\S]*?</\1>", "", stripped, flags=re.I)

    parser = _HtmlToMarkdownParser()
    parser.feed(stripped)
    parser.close()

    md = "".join(parser.out)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    if not md.startswith("# "):
        md = f"# {title}\n\n{md}".strip()

    appended: list[str] = []
    for heading, lexer, code in embedded_code:
        if code[:120] not in md:
            appended.extend([f"## 源码 · {heading}", "", f"```{lexer}", code, "```", ""])
    if appended:
        md = md + "\n\n" + "\n".join(appended).strip()
    return clean_note_markdown(md)


def _make_markdown_items(
    key_base: str,
    title_base: str,
    fname_prefix: str,
    kind: str,
    md_text: str,
    mime_type: str = "text/markdown",
    ext: str = ".md",
) -> list[SourceItem]:
    """Build 1 SourceItem, or split at ``## `` headings if exceeding ``MAX_MD_BYTES``."""
    raw = md_text.encode("utf-8")
    if len(raw) <= MAX_MD_BYTES:
        return [
            SourceItem(
                key=key_base,
                title=title_base,
                filename=f"{fname_prefix}{ext}",
                kind=kind,
                mime_type=mime_type,
                sha256=hashlib.sha256(raw).hexdigest(),
                content_bytes=raw,
            )
        ]

    blocks = re.split(r"(?m)(?=^## )", md_text)
    chunks: list[str] = []
    cur: list[str] = []
    cur_bytes = 0
    for blk in blocks:
        b_len = len(blk.encode("utf-8"))
        if cur and cur_bytes + b_len > MAX_MD_BYTES:
            chunks.append("".join(cur).strip() + "\n")
            cur, cur_bytes = [blk], b_len
        else:
            cur.append(blk)
            cur_bytes += b_len
    if cur:
        chunks.append("".join(cur).strip() + "\n")

    if len(chunks) == 1:
        # Single giant block without ## headings: split by lines safely.
        lines = md_text.splitlines(keepends=True)
        chunks, cur, cur_bytes = [], [], 0
        for ln in lines:
            l_len = len(ln.encode("utf-8"))
            if cur and cur_bytes + l_len > MAX_MD_BYTES:
                chunks.append("".join(cur))
                cur, cur_bytes = [ln], l_len
            else:
                cur.append(ln)
                cur_bytes += l_len
        if cur:
            chunks.append("".join(cur))

    out: list[SourceItem] = []
    for p_i, ch in enumerate(chunks, 1):
        b = ch.encode("utf-8")
        out.append(
            SourceItem(
                key=f"{key_base}:part{p_i}",
                title=f"{title_base}（第{p_i}/{len(chunks)}部分）",
                filename=f"{fname_prefix}-part{p_i}{ext}",
                kind=kind,
                mime_type=mime_type,
                sha256=hashlib.sha256(b).hexdigest(),
                content_bytes=b,
            )
        )
    return out


def prepare_material_items(kb: KB, course_id: str) -> tuple[list[SourceItem], list[str]]:
    """Build exportable :class:`SourceItem` list for a course's linked materials."""
    items: list[SourceItem] = []
    warnings: list[str] = []
    mats = _course_materials(kb, course_id)
    for idx, m in enumerate(mats, 1):
        src = kb.abs(m["path"])
        text_md = kb.abs(m["text_path"]) if m.get("text_path") else None
        if not src.is_file():
            warnings.append(f"资料文件缺失，已跳过：{m['title'] or m['id']}")
            continue
        ext = src.suffix.lower()
        base = safe_name(m["title"] or src.stem, 65)

        if ext == ".pdf":
            if _pdf_has_garbled_layer(src) and text_md and text_md.is_file():
                cleaned = clean_note_markdown(text_md.read_text("utf-8"))
                items.extend(
                    _make_markdown_items(
                        f"mat:{m['id']}",
                        f"资料 · {base}（OCR文字版）",
                        f"20-{idx:02d}-资料-{base}-OCR文字版",
                        "material",
                        cleaned,
                    )
                )
                continue
            parts = _split_large_pdf(src, m["sha256"])
            if len(parts) == 1:
                items.append(
                    SourceItem(
                        key=f"mat:{m['id']}",
                        title=f"资料 · {base}",
                        filename=f"20-{idx:02d}-资料-{base}.pdf",
                        kind="material",
                        mime_type="application/pdf",
                        sha256=m["sha256"],
                        file_path=src,
                    )
                )
            else:
                for p_i, part_file in enumerate(parts, 1):
                    items.append(
                        SourceItem(
                            key=f"mat:{m['id']}:part{p_i}",
                            title=f"资料 · {base}（第{p_i}/{len(parts)}部分）",
                            filename=f"20-{idx:02d}-资料-{base}-part{p_i}.pdf",
                            kind="material",
                            mime_type="application/pdf",
                            sha256=sha256_file(part_file),
                            file_path=part_file,
                        )
                    )
            continue

        if ext == ".pptx":
            pdf = _convert_pptx_to_pdf(src, m["sha256"])
            if pdf is not None:
                items.append(
                    SourceItem(
                        key=f"mat:{m['id']}",
                        title=f"资料 · {base}",
                        filename=f"20-{idx:02d}-资料-{base}.pdf",
                        kind="material",
                        mime_type="application/pdf",
                        sha256=sha256_file(pdf),
                        file_path=pdf,
                    )
                )
            elif text_md and text_md.is_file():
                warnings.append(f"《{base}》为 PPTX，未检测到可用的 LibreOffice（soffice），已回退使用提取文字版 Markdown")
                cleaned = clean_note_markdown(text_md.read_text("utf-8"))
                items.extend(
                    _make_markdown_items(
                        f"mat:{m['id']}",
                        f"资料 · {base}（幻灯片文字版）",
                        f"20-{idx:02d}-资料-{base}",
                        "material",
                        cleaned,
                    )
                )
            continue

        if ext == ".docx":
            items.append(
                SourceItem(
                    key=f"mat:{m['id']}",
                    title=f"资料 · {base}",
                    filename=f"20-{idx:02d}-资料-{base}.docx",
                    kind="material",
                    mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    sha256=m["sha256"],
                    file_path=src,
                )
            )
            continue

        if ext in (".html", ".htm"):
            cleaned_html_md = html_to_clean_markdown(src.read_text("utf-8", errors="replace"), m["title"] or src.stem)
            items.extend(
                _make_markdown_items(
                    f"mat:{m['id']}",
                    f"资料 · {base}",
                    f"20-{idx:02d}-资料-{base}",
                    "material",
                    cleaned_html_md,
                )
            )
            continue

        if ext in (".md", ".markdown"):
            cleaned = clean_note_markdown(src.read_text("utf-8", errors="replace"))
            items.extend(
                _make_markdown_items(
                    f"mat:{m['id']}",
                    f"资料 · {base}",
                    f"20-{idx:02d}-资料-{base}",
                    "material",
                    cleaned,
                )
            )
            continue

        if ext == ".txt":
            txt = src.read_text("utf-8", errors="replace")
            items.extend(
                _make_markdown_items(
                    f"mat:{m['id']}",
                    f"资料 · {base}",
                    f"20-{idx:02d}-资料-{base}",
                    "material",
                    txt,
                    mime_type="text/plain",
                    ext=".txt",
                )
            )
            continue

        # Code / CSV / TeX / other extracted text -> use structured content.md
        if text_md and text_md.is_file():
            cleaned = clean_note_markdown(text_md.read_text("utf-8", errors="replace"))
            items.extend(
                _make_markdown_items(
                    f"mat:{m['id']}",
                    f"资料 · {base}",
                    f"20-{idx:02d}-资料-{base}",
                    "material",
                    cleaned,
                )
            )
    return items, warnings


# ---------------------------------------------------------------- granularity & bundling
def sync_course_notes_in(kb: KB, course_id: str) -> int:
    """Capture manual edits made in Obsidian before building NotebookLM sources."""
    edited = 0
    for n in kb.q("SELECT * FROM notes WHERE course_id=? AND vault_path IS NOT NULL", course_id):
        try:
            if sync_in(kb, dict(n)) == "edited":
                edited += 1
        except NoteError:
            # Notes folder not configured on this machine; fall back to stored versions.
            break
    return edited


def resolve_granularity(
    requested: str,
    note_rows: list[dict],
    n_material_items: int,
    source_limit: int | None = None,
) -> tuple[str, str | None]:
    """Return ``(chosen_granularity, warning_or_info)``."""
    if requested not in GRANULARITIES:
        raise NotebookLMIntegrationError(f"不支持的粒度：{requested}（可选：{', '.join(GRANULARITIES)}）")

    n_notes = len(note_rows)
    weeks = {(r["lec_week"] or 0) if r["kind"] == "lecture" else -1 for r in note_rows}
    stages = {(((r["lec_week"] or 1) - 1) // 4) if r["kind"] == "lecture" else -1 for r in note_rows}

    counts = {
        "lecture": 1 + n_notes + n_material_items,
        "week": 1 + len(weeks) + n_material_items,
        "stage": 1 + len(stages) + n_material_items,
        "course": 1 + (1 if n_notes else 0) + n_material_items,
    }

    if requested != "auto":
        if source_limit and counts[requested] > source_limit:
            return (
                requested,
                f"当前粒度 {requested} 预计生成 {counts[requested]} 个信源，超过账号单库上限 {source_limit}；建议改用 --granularity stage 或 course",
            )
        return requested, None

    effective_limit = source_limit or 300
    budget = max(10, effective_limit - 5)
    if counts["lecture"] <= budget:
        return "lecture", None
    if counts["stage"] <= budget:
        return (
            "stage",
            f"账号单库上限为 {effective_limit}，逐课时上传需 {counts['lecture']} 个信源，已自动合并为按阶段（每 4 周，共 {counts['stage']} 个信源）",
        )
    if counts["week"] <= budget:
        return (
            "week",
            f"账号单库上限为 {effective_limit}，已自动合并为按周（共 {counts['week']} 个信源）",
        )
    return (
        "course",
        f"账号单库上限为 {effective_limit}，资料较多（{n_material_items} 份），已自动将全部课堂笔记合并为 1 个信源（共 {counts['course']} 个信源）",
    )


def _render_note_entry(kb: KB, row: dict) -> tuple[str, str]:
    """Return ``(clean_heading_label, cleaned_markdown_body)`` for one note."""
    raw = version_text(kb, row["id"], row["current_version"])
    cleaned = clean_note_markdown(raw, lecture_date=row.get("lec_date"))
    title = row.get("title") or row["id"]
    if row["kind"] == "lecture" and row.get("lec_date"):
        wk = f"W{int(row['lec_week']):02d}" if row.get("lec_week") else "课时"
        label = f"{wk} · {row['lec_date']} {title}"
    else:
        kind_cn = {"review": "复习", "cards": "知识卡片", "topic": "专题", "explain": "讲解"}.get(row["kind"], row["kind"])
        label = f"{kind_cn} · {title}"
    return label, cleaned


def _build_note_items(kb: KB, course_name: str, note_rows: list[dict], granularity: str) -> list[SourceItem]:
    if not note_rows:
        return []

    items: list[SourceItem] = []
    if granularity == "lecture":
        for idx, r in enumerate(note_rows, 1):
            label, cleaned = _render_note_entry(kb, r)
            header = f"> 课程：{course_name}"
            if r.get("lec_date"):
                wk_txt = f"第 {r['lec_week']} 周" if r.get("lec_week") else ""
                header += f" | 日期：{r['lec_date']} {wk_txt}".rstrip()
            full_bytes = (header + "\n\n" + cleaned).encode("utf-8")
            fname_stem = safe_name(label.replace("·", "-"), 70)
            items.append(
                SourceItem(
                    key=f"note:{r['id']}",
                    title=label,
                    filename=f"10-{idx:03d}-{fname_stem}.md",
                    kind="note",
                    mime_type="text/markdown",
                    sha256=hashlib.sha256(full_bytes).hexdigest(),
                    content_bytes=full_bytes,
                )
            )
        return items

    if granularity == "course":
        sections = [f"# {course_name} · 全部课堂与专题笔记", ""]
        for r in note_rows:
            label, cleaned = _render_note_entry(kb, r)
            sections.extend([f"---", "", f"## {label}", "", cleaned.strip(), ""])
        full_bytes = ("\n".join(sections).strip() + "\n").encode("utf-8")
        return [
            SourceItem(
                key="course:notes",
                title=f"{course_name} · 课堂笔记全集",
                filename=f"10-001-{safe_name(course_name, 40)}-课堂笔记全集.md",
                kind="note",
                mime_type="text/markdown",
                sha256=hashlib.sha256(full_bytes).hexdigest(),
                content_bytes=full_bytes,
            )
        ]

    # Grouped by week or 4-week stage
    groups: dict[tuple[int, str, str], list[dict]] = {}
    for r in note_rows:
        if r["kind"] != "lecture" or not r.get("lec_week"):
            gkey = (999, "note_group:topics", f"{course_name} · 专题与复习笔记")
        elif granularity == "week":
            w = int(r["lec_week"])
            gkey = (w, f"week:W{w:02d}", f"第 {w:02d} 周课堂笔记")
        else:  # stage (4 weeks per chunk)
            w = int(r["lec_week"])
            s0 = ((w - 1) // 4) * 4 + 1
            s1 = s0 + 3
            gkey = (s0, f"stage:W{s0:02d}-W{s1:02d}", f"第 {s0:02d}–{s1:02d} 周课堂笔记")
        groups.setdefault(gkey, []).append(r)

    for idx, ((_order, key, gtitle), rows_in_group) in enumerate(sorted(groups.items(), key=lambda kv: kv[0][0]), 1):
        sections = [f"# {course_name} · {gtitle}", ""]
        for r in rows_in_group:
            label, cleaned = _render_note_entry(kb, r)
            sections.extend(["---", "", f"## {label}", "", cleaned.strip(), ""])
        full_bytes = ("\n".join(sections).strip() + "\n").encode("utf-8")
        items.append(
            SourceItem(
                key=key,
                title=gtitle,
                filename=f"10-{idx:02d}-{safe_name(gtitle, 60)}.md",
                kind="note",
                mime_type="text/markdown",
                sha256=hashlib.sha256(full_bytes).hexdigest(),
                content_bytes=full_bytes,
            )
        )
    return items


def build_course_bundle(
    kb: KB,
    course: dict,
    granularity: str | None = None,
    source_limit: int | None = None,
    sync_vault: bool = True,
) -> CourseBundle:
    """Build the complete ordered set of NotebookLM sources for one course."""
    cid = course["id"]
    if sync_vault:
        sync_course_notes_in(kb, cid)

    prof = loads(course.get("profile"), {}) or {}
    req_gran = granularity or prof.get("nb_granularity") or "auto"

    note_rows = [
        dict(r)
        for r in kb.q(
            """SELECT n.*, l.date AS lec_date, l.week AS lec_week, l.start_period AS lec_period
               FROM notes n LEFT JOIN lectures l ON l.id=n.lecture_id
               WHERE n.course_id=? AND n.current_version > 0
               ORDER BY CASE WHEN l.date IS NULL THEN 1 ELSE 0 END, l.date, l.start_period, n.created_at""",
            cid,
        )
    ]
    mat_items, warnings = prepare_material_items(kb, cid)
    chosen_gran, gran_msg = resolve_granularity(req_gran, note_rows, len(mat_items), source_limit=source_limit)
    if gran_msg:
        warnings.append(gran_msg)

    guide_bytes = build_course_guide(kb, course).encode("utf-8")
    guide_item = SourceItem(
        key="guide",
        title="00 · 课程导读与知识点索引",
        filename="00-课程导读与知识点索引.md",
        kind="guide",
        mime_type="text/markdown",
        sha256=hashlib.sha256(guide_bytes).hexdigest(),
        content_bytes=guide_bytes,
    )
    note_items = _build_note_items(kb, course["name"], note_rows, chosen_gran)
    all_items = [guide_item, *note_items, *mat_items]
    nb_title = f"LW · {course['name']} ({course['term']})"
    return CourseBundle(
        course_id=cid,
        course_name=course["name"],
        term=course["term"],
        notebook_title=nb_title,
        granularity=chosen_gran,
        items=all_items,
        warnings=warnings,
        note_count=len(note_rows),
        material_count=len(mat_items),
    )


def export_bundle_to_dir(bundle: CourseBundle, out_root: Path) -> Path:
    """Write ``bundle`` cleanly into ``out_root / <course_name>``."""
    course_dir = Path(out_root).expanduser() / safe_name(f"{bundle.course_name} ({bundle.term})", 80)
    course_dir.mkdir(parents=True, exist_ok=True)
    expected = {it.filename for it in bundle.items} | {"manifest.json"}
    # Remove stale files left by a previous local build of this course.
    for existing in course_dir.iterdir():
        if existing.is_file() and existing.name not in expected:
            existing.unlink()
    for it in bundle.items:
        it.write_to(course_dir / it.filename)
    manifest = {
        "course_id": bundle.course_id,
        "course_name": bundle.course_name,
        "term": bundle.term,
        "notebook_title": bundle.notebook_title,
        "granularity": bundle.granularity,
        "source_count": len(bundle.items),
        "sources": [
            {"key": it.key, "title": it.title, "filename": it.filename, "kind": it.kind, "sha256": it.sha256}
            for it in bundle.items
        ],
    }
    (course_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    return course_dir


def select_nb_courses(kb: KB, course_query: str | None = None, term: str | None = None) -> list[dict]:
    """Select courses for ``lw nb status/build/sync``."""
    from .select import current_term, find_courses

    if course_query:
        found = [dict(r) for r in find_courses(kb, course_query, term)]
        if not found:
            raise NotebookLMIntegrationError(f"找不到匹配“{course_query}”的课程")
        if len(found) > 1:
            names = "、".join(c["name"] for c in found)
            raise NotebookLMIntegrationError(f"课程“{course_query}”匹配到多门课（{names}），请写得更具体")
        return found

    t = term or current_term(kb)
    rows = kb.q(
        """SELECT * FROM courses c
           WHERE c.term=?
             AND EXISTS (SELECT 1 FROM notes n WHERE n.course_id=c.id AND n.current_version > 0)
           ORDER BY c.name""",
        t,
    )
    out = []
    for r in rows:
        prof = loads(r["profile"], {}) or {}
        if prof.get("no_nb"):
            continue
        out.append(dict(r))
    return out


def local_diff_summary(kb: KB, bundle: CourseBundle, backend: str) -> dict:
    """Compare a local :class:`CourseBundle` against ``nb_sources`` without network."""
    nb_row = kb.q1("SELECT * FROM nb_notebooks WHERE course_id=? AND backend=?", bundle.course_id, backend)
    tracked = {
        r["source_key"]: dict(r)
        for r in kb.q("SELECT * FROM nb_sources WHERE course_id=? AND backend=?", bundle.course_id, backend)
    }
    to_add, to_update, unchanged = [], [], []
    bundle_keys = {it.key for it in bundle.items}
    for it in bundle.items:
        old = tracked.get(it.key)
        if old is None:
            to_add.append(it.title)
        elif old["sha256"] != it.sha256:
            to_update.append(it.title)
        else:
            unchanged.append(it.title)
    stale = [r["title"] or k for k, r in tracked.items() if k not in bundle_keys]
    return {
        "course": bundle.course_name,
        "course_id": bundle.course_id,
        "backend": backend,
        "notebook_id": nb_row["notebook_id"] if nb_row else None,
        "notebook_title": nb_row["title"] if nb_row else bundle.notebook_title,
        "granularity": bundle.granularity,
        "total_sources": len(bundle.items),
        "unchanged": len(unchanged),
        "to_add": to_add,
        "to_update": to_update,
        "to_remove_stale": stale,
        "warnings": bundle.warnings,
    }


# ---------------------------------------------------------------- Web backend (notebooklm-py)
def web_storage_path() -> Path:
    return secrets_dir() / "notebooklm_storage.json"


def web_profile_dir() -> Path:
    p = secrets_dir() / "notebooklm_browser_profile"
    p.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(p, 0o700)
    return p


def detect_login_browser(preferred: str | None = None) -> str:
    if preferred:
        if preferred not in ("chrome", "msedge", "chromium"):
            raise NotebookLMIntegrationError("浏览器可选：chrome / msedge / chromium")
        return preferred
    if sys.platform == "darwin":
        if Path("/Applications/Google Chrome.app").exists():
            return "chrome"
        if Path("/Applications/Microsoft Edge.app").exists():
            return "msedge"
    elif os.name == "nt":
        for env_k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_k)
            if not base:
                continue
            if (Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe").is_file():
                return "chrome"
            if (Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe").is_file():
                return "msedge"
    else:
        for b in ("google-chrome", "google-chrome-stable"):
            if shutil.which(b):
                return "chrome"
        if shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable"):
            return "msedge"
    return "chromium"


def tier_label(source_limit: int | None, tier: int | None = None) -> str:
    if source_limit is not None:
        if source_limit >= 500:
            return f"付费版（Ultra，单库上限 {source_limit} 个信源）"
        if source_limit >= 200:
            return f"付费版（Plus/Pro，单库上限 {source_limit} 个信源）"
        if source_limit > 50:
            return f"付费版（单库上限 {source_limit} 个信源）"
        return f"免费版（单库上限 {source_limit} 个信源）"
    if tier and tier > 1:
        return f"付费版（Tier {tier}）"
    return "未知配额"


class _CleanLoginIO:
    """Adapter implementing ``notebooklm``'s ``LoginIO`` protocol cleanly."""

    def __init__(self, log: Callable[[str], None], timeout_s: int = 300):
        self._log = log
        self._timeout_s = timeout_s
        self.messages: list[str] = []

    def emit(self, *args: Any, **_kwargs: Any) -> None:
        msg = " ".join(str(a) for a in args)
        msg = re.sub(r"\[/?[a-zA-Z0-9_ #=]+\]", "", msg).strip()
        if not msg:
            return
        self.messages.append(msg)
        # Suppress upstream `notebooklm login` CLI hints that precede fail() so we
        # emit a single, accurate `lw nb login` error message instead of duplicates.
        if "Login not detected within" in msg or "notebooklm login" in msg:
            return
        self._log(msg)

    def fail(self, code: int):
        last = self.messages[-1] if self.messages else ""
        if "Login not detected within" in last:
            raise NotebookLMIntegrationError(
                f"未在 {self._timeout_s} 秒内检测到 Google 登录完成。请重新运行 `lw nb login` 并在弹出的浏览器窗口中完成登录（可加 `--timeout 600` 延长等待时间）。"
            )
        detail = last.replace("notebooklm login", "lw nb login") if last else f"浏览器登录中断（代码 {code}）"
        raise NotebookLMIntegrationError(detail)

    def run_async(self, coro: Any) -> Any:
        return asyncio.run(coro)


def web_login(
    browser: str | None = None,
    timeout_s: int = 300,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> dict:
    """Interactive browser login for NotebookLM web backend."""
    try:
        import notebooklm  # noqa: F401
    except ImportError as e:
        raise NotebookLMIntegrationError(f"缺少 notebooklm-py 依赖：{e}") from e

    chosen = detect_login_browser(browser)
    spath = web_storage_path()
    pdir = web_profile_dir()
    log(f"正在打开浏览器（{chosen}）登录 Google NotebookLM，完成后会自动保存并关闭窗口…")

    try:
        from notebooklm._app.login_browser import BrowserLoginPlan
        from notebooklm.cli.services.playwright_login import run_playwright_login
    except ImportError:
        BrowserLoginPlan = None  # type: ignore[assignment]
        run_playwright_login = None  # type: ignore[assignment]

    try:
        with with_outbound_proxy():
            if BrowserLoginPlan is not None and run_playwright_login is not None:
                io = _CleanLoginIO(log, timeout_s=timeout_s)
                plan = BrowserLoginPlan(
                    browser=chosen,
                    browser_profile=pdir,
                    storage_path=spath,
                    include_domains=None,
                    login_timeout_s=timeout_s,
                )
                run_playwright_login(plan, io)
            else:
                # Forward-compatible fallback if upstream refactors internal `_app` modules:
                # invoke the public `python -m notebooklm login` CLI isolated inside secrets_dir().
                nb_home = secrets_dir() / "notebooklm_home"
                nb_home.mkdir(parents=True, exist_ok=True)
                if os.name == "posix":
                    os.chmod(nb_home, 0o700)
                env = {**os.environ, "NOTEBOOKLM_HOME": str(nb_home)}
                r = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "notebooklm",
                        "login",
                        "--storage",
                        str(spath),
                        "--browser",
                        chosen,
                        "--browser-timeout",
                        str(timeout_s),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=timeout_s + 30,
                )
                if r.returncode != 0:
                    raise NotebookLMIntegrationError((r.stderr or r.stdout).strip() or f"浏览器登录失败（代码 {r.returncode}）")
    except NotebookLMIntegrationError:
        raise
    except Exception as e:  # noqa: BLE001
        err_s = str(e)
        if "Executable doesn't exist" in err_s or "playwright install" in err_s:
            raise NotebookLMIntegrationError(
                f"未能启动浏览器（{chosen}）：本机未检测到 Chrome/Edge，或尚未安装 Playwright 内置浏览器。\n"
                "解决办法：安装 Google Chrome / Microsoft Edge，或在当前环境执行 `playwright install chromium` 后重试。"
            ) from e
        raise NotebookLMIntegrationError(f"浏览器登录失败：{err_s}") from e

    if os.name == "posix" and spath.exists():
        os.chmod(spath, 0o600)

    info = web_account_status(verify_online=True)
    if not info["online_ok"]:
        raise NotebookLMIntegrationError(f"登录态已写入但连接校验未通过：{info.get('error') or '请检查网络/代理设置'}")
    info["browser"] = chosen
    return info


def web_logout() -> bool:
    removed = False
    spath = web_storage_path()
    for p in (spath, spath.with_suffix(".json.lock")):
        if p.exists():
            p.unlink()
            removed = True
    for sub_name in ("notebooklm_browser_profile", "notebooklm_home"):
        pdir = secrets_dir() / sub_name
        if pdir.exists():
            shutil.rmtree(pdir, ignore_errors=True)
            removed = True
    return removed


@contextlib.asynccontextmanager
async def _open_web_client(spath: Path):
    """Open :class:`NotebookLMClient` via its v1.0-clean ``async with`` contract."""
    from notebooklm import NotebookLMClient

    raw_ctx = NotebookLMClient.from_storage(str(spath))
    if inspect.isawaitable(raw_ctx) and not hasattr(raw_ctx, "__aenter__"):
        raw_ctx = await raw_ctx
    async with raw_ctx as client:
        yield client


async def _probe_web_account(spath: Path) -> dict:
    async with _open_web_client(spath) as client:
        email = None
        with contextlib.suppress(Exception):
            maybe_email = client.get_account_email()
            email = await maybe_email if inspect.isawaitable(maybe_email) else maybe_email
        if not email:
            with contextlib.suppress(Exception):
                from notebooklm.auth import get_account_email_for_storage

                email = get_account_email_for_storage(spath)
        limits = await client.settings.get_account_limits()
        nbs = await client.notebooks.list()
        return {
            "logged_in": True,
            "online_ok": True,
            "email": email,
            "tier": limits.tier,
            "source_limit": limits.source_limit,
            "notebook_limit": limits.notebook_limit,
            "tier_label": tier_label(limits.source_limit, limits.tier),
            "notebook_count": len(nbs),
            "error": None,
        }


def web_account_status(verify_online: bool = True) -> dict:
    spath = web_storage_path()
    if not spath.is_file():
        return {
            "logged_in": False,
            "online_ok": False,
            "email": None,
            "tier": None,
            "source_limit": None,
            "notebook_limit": None,
            "tier_label": "未登录",
            "notebook_count": None,
            "error": None,
        }
    if not verify_online:
        email = None
        try:
            from notebooklm.auth import get_account_email_for_storage

            email = get_account_email_for_storage(spath)
        except Exception:  # noqa: BLE001
            pass
        return {
            "logged_in": True,
            "online_ok": None,
            "email": email,
            "tier": None,
            "source_limit": None,
            "notebook_limit": None,
            "tier_label": "已保存会话（未联网校验）",
            "notebook_count": None,
            "error": None,
        }
    try:
        with with_outbound_proxy():
            return asyncio.run(_probe_web_account(spath))
    except Exception as e:  # noqa: BLE001
        return {
            "logged_in": True,
            "online_ok": False,
            "email": None,
            "tier": None,
            "source_limit": None,
            "notebook_limit": None,
            "tier_label": "会话不可用或网络受阻",
            "notebook_count": None,
            "error": str(e),
        }


async def _web_sync_courses_async(
    kb: KB,
    courses: list[dict],
    granularity: str | None,
    dry_run: bool,
    log: Callable[[str], None],
) -> dict:
    from notebooklm.exceptions import AuthError, AuthExtractionError, HeadlessReauthError, RateLimitError

    spath = web_storage_path()
    if not spath.is_file():
        raise NotebookLMIntegrationError("尚未登录 NotebookLM：请先运行 `lw nb login`")

    try:
        async with _open_web_client(spath) as client:
            return await _web_sync_with_client(kb, client, courses, granularity, dry_run, log)
    except (AuthError, AuthExtractionError, HeadlessReauthError) as e:
        raise NotebookLMIntegrationError(f"NotebookLM 登录会话已失效，请重新运行 `lw nb login`（{e}）") from e


async def _web_sync_with_client(
    kb: KB,
    client: Any,
    courses: list[dict],
    granularity: str | None,
    dry_run: bool,
    log: Callable[[str], None],
) -> dict:
    from notebooklm.exceptions import AuthError, AuthExtractionError, HeadlessReauthError, RateLimitError

    if True:
        source_limit: int | None = None
        tier_val: int | None = None
        with contextlib.suppress(Exception):
            lim = await client.settings.get_account_limits()
            source_limit = lim.source_limit
            tier_val = lim.tier

        try:
            remote_nbs = await client.notebooks.list()
        except (AuthError, AuthExtractionError, HeadlessReauthError) as e:
            raise NotebookLMIntegrationError(f"NotebookLM 登录会话已失效，请重新运行 `lw nb login`（{e}）") from e
        remote_by_id = {nb.id: nb for nb in remote_nbs}
        remote_by_title = {nb.title: nb for nb in remote_nbs}

        results = []
        any_failed = False
        for course in courses:
            cid = course["id"]
            bundle = build_course_bundle(kb, course, granularity=granularity, source_limit=source_limit, sync_vault=True)
            nb_row = kb.q1("SELECT * FROM nb_notebooks WHERE course_id=? AND backend='web'", cid)
            tracked = {
                r["source_key"]: dict(r)
                for r in kb.q("SELECT * FROM nb_sources WHERE course_id=? AND backend='web'", cid)
            }

            remote_nb = None
            if nb_row and nb_row["notebook_id"] in remote_by_id:
                remote_nb = remote_by_id[nb_row["notebook_id"]]
            elif bundle.notebook_title in remote_by_title:
                remote_nb = remote_by_title[bundle.notebook_title]

            remote_sources = await client.sources.list(remote_nb.id) if remote_nb else []
            remote_src_ids = {s.id for s in remote_sources}
            lw_tracked_ids = {r["remote_source_id"] for r in tracked.values()}
            manual_count = len(remote_src_ids - lw_tracked_ids)

            to_upload: list[tuple[SourceItem, str]] = []  # (item, 'add'|'update')
            unchanged = 0
            for it in bundle.items:
                old = tracked.get(it.key)
                if old and old["sha256"] == it.sha256 and old["remote_source_id"] in remote_src_ids:
                    unchanged += 1
                elif old:
                    to_upload.append((it, "update"))
                else:
                    to_upload.append((it, "add"))

            bundle_keys = {it.key for it in bundle.items}
            stale_keys = [k for k in tracked if k not in bundle_keys]

            if dry_run:
                results.append({
                    "course": bundle.course_name,
                    "notebook_id": remote_nb.id if remote_nb else None,
                    "notebook_title": remote_nb.title if remote_nb else bundle.notebook_title,
                    "granularity": bundle.granularity,
                    "unchanged": unchanged,
                    "added": [it.title for it, mode in to_upload if mode == "add"],
                    "updated": [it.title for it, mode in to_upload if mode == "update"],
                    "removed_stale": [tracked[k]["title"] or k for k in stale_keys],
                    "failed": [],
                    "preserved_manual_sources": manual_count,
                    "warnings": bundle.warnings,
                    "dry_run": True,
                })
                continue

            now = time.time()
            if remote_nb is None:
                log(f"[{bundle.course_name}] 创建笔记本：{bundle.notebook_title}")
                remote_nb = await client.notebooks.create(bundle.notebook_title)
                remote_by_id[remote_nb.id] = remote_nb
                remote_by_title[remote_nb.title] = remote_nb

            kb.x(
                """INSERT INTO nb_notebooks(course_id,backend,notebook_id,title,granularity,remote_folder_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,NULL,?,?)
                   ON CONFLICT(course_id,backend) DO UPDATE SET
                     notebook_id=excluded.notebook_id, title=excluded.title,
                     granularity=excluded.granularity, updated_at=excluded.updated_at""",
                cid, "web", remote_nb.id, remote_nb.title, bundle.granularity, now, now,
            )
            kb.db.commit()

            added_titles, updated_titles, removed_titles, failed_items = [], [], [], []
            with tempfile.TemporaryDirectory(prefix="lw-nb-upload-") as td:
                td_path = Path(td)
                for idx, (it, mode) in enumerate(to_upload, 1):
                    verb = "新增" if mode == "add" else "更新"
                    log(f"[{bundle.course_name}] ({idx}/{len(to_upload)}) {verb}信源：{it.title}")
                    upload_path = it.file_path if it.file_path is not None else it.write_to(td_path / it.filename)
                    try:
                        try:
                            new_src = await client.sources.add_file(
                                remote_nb.id,
                                upload_path,
                                mime_type=it.mime_type,
                                title=it.title,
                                wait=True,
                                wait_timeout=180.0,
                            )
                        except RateLimitError:
                            log(f"[{bundle.course_name}] 触发请求限速，等待 5 秒后重试：{it.title}")
                            await asyncio.sleep(5.0)
                            new_src = await client.sources.add_file(
                                remote_nb.id,
                                upload_path,
                                mime_type=it.mime_type,
                                title=it.title,
                                wait=True,
                                wait_timeout=180.0,
                            )
                    except (AuthError, AuthExtractionError, HeadlessReauthError) as e:
                        raise NotebookLMIntegrationError(f"NotebookLM 登录会话已失效，请重新运行 `lw nb login`（{e}）") from e
                    except Exception as exc:  # noqa: BLE001
                        orphan_id = getattr(exc, "source_id", None)
                        if orphan_id:
                            with contextlib.suppress(Exception):
                                await client.sources.delete(remote_nb.id, orphan_id)
                        any_failed = True
                        failed_items.append({"key": it.key, "title": it.title, "error": str(exc)})
                        log(f"[{bundle.course_name}] ✗ 上传失败（已清理残留并跳过，下次 sync 可重试）：{it.title} — {exc}")
                        continue

                    old = tracked.get(it.key)
                    if old and old["remote_source_id"] in remote_src_ids and old["remote_source_id"] != new_src.id:
                        with contextlib.suppress(Exception):
                            await client.sources.delete(remote_nb.id, old["remote_source_id"])
                    kb.x(
                        """INSERT INTO nb_sources(course_id,backend,source_key,remote_source_id,title,kind,sha256,synced_at)
                           VALUES(?,?,?,?,?,?,?,?)
                           ON CONFLICT(course_id,backend,source_key) DO UPDATE SET
                             remote_source_id=excluded.remote_source_id, title=excluded.title,
                             kind=excluded.kind, sha256=excluded.sha256, synced_at=excluded.synced_at""",
                        cid, "web", it.key, new_src.id, it.title, it.kind, it.sha256, time.time(),
                    )
                    kb.db.commit()
                    if mode == "add":
                        added_titles.append(it.title)
                    else:
                        updated_titles.append(it.title)
                    if idx < len(to_upload):
                        await asyncio.sleep(0.35)

            for sk in stale_keys:
                old = tracked[sk]
                if old["remote_source_id"] in remote_src_ids:
                    with contextlib.suppress(Exception):
                        await client.sources.delete(remote_nb.id, old["remote_source_id"])
                kb.x("DELETE FROM nb_sources WHERE course_id=? AND backend='web' AND source_key=?", cid, sk)
                kb.db.commit()
                removed_titles.append(old["title"] or sk)

            results.append({
                "course": bundle.course_name,
                "notebook_id": remote_nb.id,
                "notebook_title": remote_nb.title,
                "notebook_url": f"https://notebooklm.google.com/notebook/{remote_nb.id}",
                "granularity": bundle.granularity,
                "unchanged": unchanged,
                "added": added_titles,
                "updated": updated_titles,
                "removed_stale": removed_titles,
                "failed": failed_items,
                "preserved_manual_sources": manual_count,
                "warnings": bundle.warnings,
                "dry_run": False,
            })

        return {
            "backend": "web",
            "account_tier": tier_label(source_limit, tier_val),
            "source_limit": source_limit,
            "any_failed": any_failed,
            "courses": results,
        }


# ---------------------------------------------------------------- Google Drive OAuth backend
def _parse_oauth_client_secrets(raw_json: str) -> dict[str, str]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise NotebookLMIntegrationError(f"OAuth 客户端配置不是有效的 JSON：{e}") from e
    cfg = data.get("installed") or data.get("web") or data
    cid = cfg.get("client_id")
    csec = cfg.get("client_secret", "")
    if not cid:
        raise NotebookLMIntegrationError("OAuth 客户端 JSON 中缺少 client_id（请在 Google Cloud Console 下载桌面应用 OAuth 客户端 JSON）")
    return {
        "client_id": cid,
        "client_secret": csec,
        "auth_uri": cfg.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": cfg.get("token_uri") or "https://oauth2.googleapis.com/token",
    }


def gdrive_login(
    client_secrets_path: Path | None = None,
    timeout_s: int = 240,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> dict:
    """Loopback OAuth 2.0 + PKCE login for Google Drive (``drive.file`` scope)."""
    if client_secrets_path is not None:
        p = Path(client_secrets_path).expanduser()
        if not p.is_file():
            raise NotebookLMIntegrationError(f"找不到 OAuth 客户端配置文件：{p}")
        client_cfg = _parse_oauth_client_secrets(p.read_text("utf-8"))
        set_secret(GDRIVE_CLIENT_KEY, json.dumps(client_cfg))
    else:
        stored = get_secret(GDRIVE_CLIENT_KEY)
        if not stored:
            raise NotebookLMIntegrationError(
                "首次使用 gdrive 后端需提供 Google Cloud 桌面端 OAuth 客户端配置：\n"
                "1. 在 Google Cloud Console 启用 Google Drive API，创建“桌面应用 (Desktop app)” OAuth 客户端 ID 并下载 JSON；\n"
                "2. 运行 `lw nb login --backend gdrive --client-secrets <下载的.json>`（配置会存入系统钥匙串，之后无需重复指定）。"
            )
        client_cfg = _parse_oauth_client_secrets(stored)

    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state_token = pysecrets.token_urlsafe(24)
    received: dict[str, str] = {}

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            for k in ("code", "state", "error"):
                if qs.get(k):
                    received[k] = qs[k][0]
            ok = "code" in received and received.get("state") == state_token
            body = (
                "<html><meta charset='utf-8'><body style='font-family:sans-serif;padding:2rem'>"
                + ("<h2>LectureWeaver 授权成功</h2><p>可以关闭此浏览器标签页回到终端。</p>" if ok else "<h2>授权未完成</h2><p>请返回终端查看错误信息。</p>")
                + "</body></html>"
            ).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = timeout_s
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    auth_url = f"{client_cfg['auth_uri']}?" + urllib.parse.urlencode({
        "client_id": client_cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GDRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state_token,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    log("正在打开浏览器进行 Google Drive OAuth 授权（仅申请 drive.file 最小权限）…")
    if not webbrowser.open(auth_url):
        log(f"若浏览器未自动打开，请复制到浏览器访问：\n{auth_url}")

    try:
        server.handle_request()
    finally:
        server.server_close()

    if received.get("error"):
        raise NotebookLMIntegrationError(f"Google OAuth 授权被拒绝或出错：{received['error']}")
    if received.get("state") != state_token or not received.get("code"):
        raise NotebookLMIntegrationError("等待浏览器 OAuth 回调超时或状态校验失败")

    payload = {
        "client_id": client_cfg["client_id"],
        "code": received["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client_cfg.get("client_secret"):
        payload["client_secret"] = client_cfg["client_secret"]

    with httpx.Client(proxy=outbound_proxy(), timeout=30.0) as hc:
        resp = hc.post(client_cfg["token_uri"], data=payload)
    if resp.status_code >= 400:
        raise NotebookLMIntegrationError(f"换取 OAuth 令牌失败（HTTP {resp.status_code}）")
    tok = resp.json()
    token_record = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        "scope": tok.get("scope", GDRIVE_SCOPE),
    }
    set_secret(GDRIVE_TOKEN_KEY, json.dumps(token_record))
    return {"logged_in": True, "online_ok": True, "scope": token_record["scope"]}


def gdrive_logout(forget_client: bool = False) -> bool:
    removed = delete_secret(GDRIVE_TOKEN_KEY)
    if forget_client:
        removed = delete_secret(GDRIVE_CLIENT_KEY) or removed
    return removed


def gdrive_account_status() -> dict:
    has_client = bool(get_secret(GDRIVE_CLIENT_KEY))
    raw_tok = get_secret(GDRIVE_TOKEN_KEY)
    if not raw_tok:
        return {"logged_in": False, "has_client_config": has_client, "scope": None}
    try:
        tok = json.loads(raw_tok)
    except json.JSONDecodeError:
        return {"logged_in": False, "has_client_config": has_client, "scope": None}
    return {
        "logged_in": bool(tok.get("refresh_token") or tok.get("access_token")),
        "has_client_config": has_client,
        "scope": tok.get("scope", GDRIVE_SCOPE),
    }


def _gdrive_access_token(hc: httpx.Client) -> str:
    raw_tok = get_secret(GDRIVE_TOKEN_KEY)
    raw_cfg = get_secret(GDRIVE_CLIENT_KEY)
    if not raw_tok or not raw_cfg:
        raise NotebookLMIntegrationError("尚未登录 Google Drive：请先运行 `lw nb login --backend gdrive --client-secrets <path.json>`")
    tok = json.loads(raw_tok)
    if tok.get("access_token") and float(tok.get("expires_at", 0)) - 60 > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise NotebookLMIntegrationError("Google Drive 令牌已过期且无 refresh_token，请重新运行 `lw nb login --backend gdrive`")
    cfg = _parse_oauth_client_secrets(raw_cfg)
    data = {
        "client_id": cfg["client_id"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]
    resp = hc.post(cfg["token_uri"], data=data)
    if resp.status_code >= 400:
        raise NotebookLMIntegrationError(f"刷新 Google Drive OAuth 令牌失败（HTTP {resp.status_code}），请重新运行 `lw nb login --backend gdrive`")
    fresh = resp.json()
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
    if fresh.get("refresh_token"):
        tok["refresh_token"] = fresh["refresh_token"]
    set_secret(GDRIVE_TOKEN_KEY, json.dumps(tok))
    return tok["access_token"]


def _gdrive_ensure_folder(hc: httpx.Client, token: str, name: str, parent_id: str | None = None) -> str:
    escaped = name.replace("'", "\\'")
    q = f"mimeType='application/vnd.google-apps.folder' and name='{escaped}' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    headers = {"Authorization": f"Bearer {token}"}
    r = hc.get("https://www.googleapis.com/drive/v3/files", params={"q": q, "fields": "files(id,name)", "pageSize": 5}, headers=headers)
    r.raise_for_status()
    files = r.json().get("files") or []
    if files:
        return files[0]["id"]
    meta: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    cr = hc.post("https://www.googleapis.com/drive/v3/files", params={"fields": "id"}, headers=headers, json=meta)
    cr.raise_for_status()
    return cr.json()["id"]


def _gdrive_multipart_upload(
    hc: httpx.Client,
    token: str,
    metadata: dict[str, Any],
    media_bytes: bytes,
    media_mime: str,
    file_id: str | None = None,
) -> str:
    boundary = "===============lw_nb_" + pysecrets.token_hex(12) + "=="
    meta_part = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("utf-8")
        + meta_part
        + f"\r\n--{boundary}\r\nContent-Type: {media_mime}\r\n\r\n".encode("utf-8")
        + media_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/related; boundary={boundary}",
    }
    if file_id:
        url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}"
        resp = hc.patch(url, params={"uploadType": "multipart", "fields": "id"}, headers=headers, content=body)
        if resp.status_code == 404:
            file_id = None
        else:
            resp.raise_for_status()
            return resp.json()["id"]
    url = "https://www.googleapis.com/upload/drive/v3/files"
    resp = hc.post(url, params={"uploadType": "multipart", "fields": "id"}, headers=headers, content=body)
    resp.raise_for_status()
    return resp.json()["id"]


def _gdrive_sync_courses(
    kb: KB,
    courses: list[dict],
    granularity: str | None,
    dry_run: bool,
    log: Callable[[str], None],
) -> dict:
    results = []
    with httpx.Client(proxy=outbound_proxy(), timeout=120.0) as hc:
        token = None if dry_run else _gdrive_access_token(hc)
        root_folder_id = _gdrive_ensure_folder(hc, token, "LectureWeaver") if token else None

        for course in courses:
            cid = course["id"]
            bundle = build_course_bundle(kb, course, granularity=granularity, source_limit=None, sync_vault=True)
            nb_row = kb.q1("SELECT * FROM nb_notebooks WHERE course_id=? AND backend='gdrive'", cid)
            tracked = {
                r["source_key"]: dict(r)
                for r in kb.q("SELECT * FROM nb_sources WHERE course_id=? AND backend='gdrive'", cid)
            }

            to_upload: list[tuple[SourceItem, str]] = []
            unchanged = 0
            for it in bundle.items:
                old = tracked.get(it.key)
                if old and old["sha256"] == it.sha256:
                    unchanged += 1
                elif old:
                    to_upload.append((it, "update"))
                else:
                    to_upload.append((it, "add"))
            bundle_keys = {it.key for it in bundle.items}
            stale_keys = [k for k in tracked if k not in bundle_keys]

            if dry_run:
                results.append({
                    "course": bundle.course_name,
                    "folder_id": nb_row["remote_folder_id"] if nb_row else None,
                    "granularity": bundle.granularity,
                    "unchanged": unchanged,
                    "added": [it.title for it, m in to_upload if m == "add"],
                    "updated": [it.title for it, m in to_upload if m == "update"],
                    "removed_stale": [tracked[k]["title"] or k for k in stale_keys],
                    "failed": [],
                    "warnings": bundle.warnings,
                    "dry_run": True,
                })
                continue

            assert token is not None and root_folder_id is not None
            folder_name = f"{bundle.course_name} ({bundle.term})"
            folder_id = nb_row["remote_folder_id"] if (nb_row and nb_row["remote_folder_id"]) else _gdrive_ensure_folder(hc, token, folder_name, root_folder_id)
            now = time.time()
            kb.x(
                """INSERT INTO nb_notebooks(course_id,backend,notebook_id,title,granularity,remote_folder_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(course_id,backend) DO UPDATE SET
                     title=excluded.title, granularity=excluded.granularity,
                     remote_folder_id=excluded.remote_folder_id, updated_at=excluded.updated_at""",
                cid, "gdrive", folder_id, folder_name, bundle.granularity, folder_id, now, now,
            )
            kb.db.commit()

            added_titles, updated_titles, removed_titles, failed_items = [], [], [], []
            for idx, (it, mode) in enumerate(to_upload, 1):
                verb = "新增" if mode == "add" else "原地更新"
                log(f"[{bundle.course_name}] ({idx}/{len(to_upload)}) Drive {verb}：{it.title}")
                raw_bytes = it.content_bytes if it.content_bytes is not None else it.file_path.read_bytes()
                is_doc = it.mime_type in ("text/markdown", "text/plain")
                old = tracked.get(it.key)
                try:
                    if old and old["remote_source_id"]:
                        meta: dict[str, Any] = {"name": it.title}
                        fid = _gdrive_multipart_upload(hc, token, meta, raw_bytes, it.mime_type, file_id=old["remote_source_id"])
                    else:
                        meta = {"name": it.title, "parents": [folder_id]}
                        if is_doc:
                            meta["mimeType"] = "application/vnd.google-apps.document"
                        fid = _gdrive_multipart_upload(hc, token, meta, raw_bytes, it.mime_type, file_id=None)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (401, 403):
                        raise NotebookLMIntegrationError(
                            f"Google Drive 授权无效或被拒绝（HTTP {exc.response.status_code}），请重新运行 `lw nb login --backend gdrive`"
                        ) from exc
                    failed_items.append({"key": it.key, "title": it.title, "error": str(exc)})
                    log(f"[{bundle.course_name}] ✗ Drive 上传失败（已跳过）：{it.title} — {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    failed_items.append({"key": it.key, "title": it.title, "error": str(exc)})
                    log(f"[{bundle.course_name}] ✗ Drive 上传失败（已跳过）：{it.title} — {exc}")
                    continue

                kb.x(
                    """INSERT INTO nb_sources(course_id,backend,source_key,remote_source_id,title,kind,sha256,synced_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(course_id,backend,source_key) DO UPDATE SET
                         remote_source_id=excluded.remote_source_id, title=excluded.title,
                         kind=excluded.kind, sha256=excluded.sha256, synced_at=excluded.synced_at""",
                    cid, "gdrive", it.key, fid, it.title, it.kind, it.sha256, time.time(),
                )
                kb.db.commit()
                (added_titles if mode == "add" else updated_titles).append(it.title)

            for sk in stale_keys:
                old = tracked[sk]
                with contextlib.suppress(Exception):
                    hc.patch(
                        f"https://www.googleapis.com/drive/v3/files/{old['remote_source_id']}",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"trashed": True},
                    )
                kb.x("DELETE FROM nb_sources WHERE course_id=? AND backend='gdrive' AND source_key=?", cid, sk)
                kb.db.commit()
                removed_titles.append(old["title"] or sk)

            results.append({
                "course": bundle.course_name,
                "folder_id": folder_id,
                "folder_url": f"https://drive.google.com/drive/folders/{folder_id}",
                "granularity": bundle.granularity,
                "unchanged": unchanged,
                "added": added_titles,
                "updated": updated_titles,
                "removed_stale": removed_titles,
                "failed": failed_items,
                "warnings": bundle.warnings,
                "dry_run": False,
            })

    any_failed = any(bool(c.get("failed")) for c in results)
    return {"backend": "gdrive", "any_failed": any_failed, "courses": results}


# ---------------------------------------------------------------- top-level sync entry
def sync_courses(
    kb: KB,
    course_query: str | None = None,
    term: str | None = None,
    backend: str = "web",
    granularity: str | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> dict:
    """Validate consent, select target courses, and incrementally sync."""
    if backend not in BACKENDS:
        raise NotebookLMIntegrationError(f"不支持的后端：{backend}（可选：web, gdrive）")
    service = "notebooklm" if backend == "web" else "gdrive"
    if not dry_run:
        check_consent(kb, service, CONSENT_SCOPES)

    courses = select_nb_courses(kb, course_query=course_query, term=term)
    if not courses:
        return {"backend": backend, "any_failed": False, "courses": []}

    lock_fh = None
    if not dry_run:
        lock_fh = try_lock(kb.root / ".locks" / f"nb_sync_{backend}.lock", wait=False)
        if lock_fh is None:
            raise NotebookLMIntegrationError("已有另一个 `lw nb sync` 进程正在同步当前知识库，请等待其完成后再试")

    try:
        if backend == "web":
            with with_outbound_proxy():
                return asyncio.run(_web_sync_courses_async(kb, courses, granularity, dry_run, log))
        return _gdrive_sync_courses(kb, courses, granularity, dry_run, log)
    finally:
        if lock_fh is not None:
            with contextlib.suppress(Exception):
                lock_fh.close()
