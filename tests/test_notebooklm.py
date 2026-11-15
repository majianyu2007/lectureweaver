import json
import sqlite3
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import make_course, make_lecture
from lectureweaver import backup as B
from lectureweaver import consent as C
from lectureweaver import learning as Lg
from lectureweaver import materials as M
from lectureweaver import notebooklm as NB
from lectureweaver import notes as N
from lectureweaver.kb import KB


def test_clean_note_markdown_preserves_code_and_latex():
    raw = """---
lw_id: N-1
tags:
  - lectureweaver
---

# 条件概率与独立性

课堂推导 [10:12](https://ylb.nwafu.edu.cn/TeachingCenterStudentWeb/index.html#/aiClassroom/aiClassroom.htm?courseTableId=CT1&videoId=V1&latestWatchTime=180) 给出公式：

$$P(A\\mid B) = \\frac{P(AB)}{P(B)}, \\quad P(B) > 0$$

行内公式 $x_1, x_2 \\in \\mathbb{R}$ 与资料引用 [《概率论》 p.12](file:///Users/test/LectureWeaver/materials/m_abcdef123456/ch1.pdf#page=12)。

![F001 · 录屏 10:14:00](attachments/2026-10-20/F001.webp)
![[attachments/board.png|板书推导]]

```python
# 代码块里的链接与图片语法绝不能被改写
url = "[10:12](https://ylb.nwafu.edu.cn/foo)"
img = "![alt](attachments/keep.webp)"
```

<!-- lw:sources -->
> [!info]- 来源
> - 第1段录像（[回看](https://ylb.nwafu.edu.cn/x)）
<!-- /lw:sources -->
"""
    cleaned = NB.clean_note_markdown(raw, lecture_date="2026-10-20")
    assert "lw_id:" not in cleaned
    assert "lw:sources" not in cleaned
    assert "[2026-10-20 10:12]" in cleaned
    assert "ylb.nwafu.edu.cn/TeachingCenterStudentWeb" not in cleaned
    assert "file://" not in cleaned
    assert "《概率论》 p.12" in cleaned
    assert "[课堂截图：F001 · 录屏 10:14:00]" in cleaned
    assert "[课堂截图：板书推导]" in cleaned
    # LaTeX and code block intact
    assert "$$P(A\\mid B) = \\frac{P(AB)}{P(B)}, \\quad P(B) > 0$$" in cleaned
    assert "$x_1, x_2 \\in \\mathbb{R}$" in cleaned
    assert 'url = "[10:12](https://ylb.nwafu.edu.cn/foo)"' in cleaned
    assert 'img = "![alt](attachments/keep.webp)"' in cleaned


def test_course_guide_and_obsidian_sync_in(kb, isolated_env):
    cid = make_course(kb, subject="math")
    kb.x(
        "UPDATE courses SET profile=? WHERE id=?",
        json.dumps({"subject": "math", "note_prefs": "分布记号统一用 X ~ N(mu, sigma^2)"}, ensure_ascii=False),
        cid,
    )
    kb.db.commit()
    lid = make_lecture(kb, cid)
    note = N.get_or_create(kb, "lecture", lid)
    N.save(kb, note, "# 全概率公式\n\n核心内容 {t 10:10:00}\n\n{f F001}\n", {})

    # Simulate a manual edit in Obsidian
    vpath = N.notes_root() / kb.q1("SELECT vault_path FROM notes WHERE id=?", note["id"])["vault_path"]
    vpath.write_text(vpath.read_text("utf-8") + "\n我在 Obsidian 手写补充的考前注意点\n", "utf-8")

    # Add concept + misconception
    c = Lg.ensure_concept(kb, "全概率公式", cid, summary="完备事件组分解样本空间")
    Lg.link_lecture(kb, c["id"], lid, "10:10:00")
    Lg.log_event(kb, c["id"], "answered", "wrong", "漏掉了互斥条件", misconception="忘记划分事件组必须两两互斥")

    bundle = NB.build_course_bundle(kb, dict(kb.q1("SELECT * FROM courses WHERE id=?", cid)))
    assert bundle.granularity == "lecture"
    assert len(bundle.items) == 2  # guide + 1 lecture note

    guide_text = bundle.items[0].content_bytes.decode("utf-8")
    assert "分布记号统一用 X ~ N(mu, sigma^2)" in guide_text
    assert "全概率公式" in guide_text
    assert "忘记划分事件组必须两两互斥" in guide_text
    assert "10:10" in guide_text

    note_text = bundle.items[1].content_bytes.decode("utf-8")
    assert "我在 Obsidian 手写补充的考前注意点" in note_text
    assert "[2026-10-20 10:10]" in note_text


def test_material_export_garbled_pdf_and_pptx_fallback(kb, isolated_env, monkeypatch):
    cid = make_course(kb)
    # 1. Garbled PDF -> should export clean OCR content.md instead of mojibake PDF
    pdf_file = isolated_env / "garbled.pdf"
    from pypdf import PdfWriter

    wr = PdfWriter()
    wr.add_blank_page(width=200, height=200)
    with open(pdf_file, "wb") as f:
        wr.write(f)
    m_pdf, _, _ = M.import_file(kb, pdf_file, title="第5章大数定律")
    kb.abs(m_pdf["text_path"]).write_text("# 第5章大数定律\n\n## p.1\n\n切比雪夫不等式清晰OCR文本\n", "utf-8")
    M.link(kb, m_pdf["id"], "course", cid, "slides")

    # 2. PPTX without soffice -> fallback to Markdown with warning
    from pptx import Presentation

    pptx_file = isolated_env / "ch1.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "数据库绪论"
    prs.save(str(pptx_file))
    m_pptx, _, _ = M.import_file(kb, pptx_file, title="第1章绪论")
    M.link(kb, m_pptx["id"], "course", cid, "slides")

    monkeypatch.setattr(NB, "_pdf_has_garbled_layer", lambda p: p.name == "garbled.pdf")
    monkeypatch.setattr(NB, "find_soffice", lambda *a, **k: None)

    items, warnings = NB.prepare_material_items(kb, cid)
    assert len(items) == 2
    by_key = {it.key: it for it in items}
    pdf_item = by_key[f"mat:{m_pdf['id']}"]
    assert pdf_item.mime_type == "text/markdown"
    assert "OCR文字版" in pdf_item.title
    assert "切比雪夫不等式清晰OCR文本" in pdf_item.content_bytes.decode("utf-8")

    pptx_item = by_key[f"mat:{m_pptx['id']}"]
    assert pptx_item.mime_type == "text/markdown"
    assert "数据库绪论" in pptx_item.content_bytes.decode("utf-8")
    assert any("LibreOffice" in w for w in warnings)


def test_granularity_auto_adapts_between_paid_and_free_tiers():
    fake_notes = [{"id": f"N-{i}", "kind": "lecture", "lec_week": (i % 12) + 1} for i in range(40)]
    # Paid Plus/Pro (limit 300): 1 + 40 + 15 = 56 <= 295 -> per-lecture!
    gran_paid, msg_paid = NB.resolve_granularity("auto", fake_notes, n_material_items=15, source_limit=300)
    assert gran_paid == "lecture"
    assert msg_paid is None

    # Free tier (limit 50): 56 > 45 -> automatically consolidates into 4-week stages!
    gran_free, msg_free = NB.resolve_granularity("auto", fake_notes, n_material_items=15, source_limit=50)
    assert gran_free == "stage"
    assert msg_free and "50" in msg_free


def test_web_sync_incremental_add_before_delete_and_preserves_manual(kb, isolated_env, monkeypatch):
    cid = make_course(kb)
    lid = make_lecture(kb, cid)
    note = N.get_or_create(kb, "lecture", lid)
    N.save(kb, note, "# 第一次课\n\n初始内容 {t 10:10:00}\n", {})

    # Must require consent first
    with pytest.raises(C.ConsentRequired):
        NB.sync_courses(kb, backend="web")
    C.grant(kb, "notebooklm", ["notes", "materials", "learning"])

    # Prepare fake storage file + fake NotebookLMClient
    NB.web_storage_path().write_text('{"cookies": [{"name": "SID", "value": "secret-sid-cookie"}]}', "utf-8")

    @dataclass
    class FakeObj:
        id: str
        title: str = ""

    state = {
        "notebooks": [],
        "sources": {},  # nb_id -> list[FakeObj]
        "ops": [],
        "counter": 0,
        "tier_limit": 300,
    }

    class FakeSettings:
        async def get_account_limits(self):
            return type("Lim", (), {"source_limit": state["tier_limit"], "notebook_limit": 500, "tier": 2})()

    class FakeNotebooks:
        async def list(self):
            return list(state["notebooks"])

        async def create(self, title: str):
            nb = FakeObj(id="nb-100", title=title)
            state["notebooks"].append(nb)
            # Pre-populate a manual source the user added on the web UI!
            state["sources"][nb.id] = [FakeObj(id="manual-user-yt-1", title="用户自己加的参考视频")]
            state["ops"].append(("create_nb", title))
            return nb

    class FakeSources:
        async def list(self, nb_id: str):
            return list(state["sources"].get(nb_id, []))

        async def add_file(self, nb_id: str, file_path: Path, mime_type=None, *, wait=False, wait_timeout=120.0, title=None, on_progress=None):
            assert Path(file_path).is_file()
            state["counter"] += 1
            src = FakeObj(id=f"src-{state['counter']}", title=title or Path(file_path).name)
            state["sources"].setdefault(nb_id, []).append(src)
            state["ops"].append(("add", src.id, title))
            return src

        async def delete(self, nb_id: str, source_id: str):
            state["ops"].append(("delete", source_id))
            state["sources"][nb_id] = [s for s in state["sources"].get(nb_id, []) if s.id != source_id]

    class FakeClient:
        settings = FakeSettings()
        notebooks = FakeNotebooks()
        sources = FakeSources()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @staticmethod
        async def from_storage(path, **kwargs):
            return FakeClient()

    import notebooklm as upstream_nb

    monkeypatch.setattr(upstream_nb, "NotebookLMClient", FakeClient)

    # 1. Initial sync: creates notebook + uploads guide + 1 note
    r1 = NB.sync_courses(kb, backend="web", log=lambda _: None)
    c1 = r1["courses"][0]
    assert len(c1["added"]) == 2
    assert c1["updated"] == []
    assert any(s.id == "manual-user-yt-1" for s in state["sources"]["nb-100"])

    # 2. Second sync with no changes: 0 uploads, and detects the 1 manual source!
    state["ops"].clear()
    r2 = NB.sync_courses(kb, backend="web", log=lambda _: None)
    c2 = r2["courses"][0]
    assert c2["unchanged"] == 2
    assert c2["preserved_manual_sources"] == 1
    assert c2["added"] == [] and c2["updated"] == []
    assert state["ops"] == []

    # 3. Modify note -> must upload new version BEFORE deleting old version, and keep manual source!
    old_note_src_id = kb.q1("SELECT remote_source_id FROM nb_sources WHERE source_key LIKE 'note:%'")["remote_source_id"]
    N.save(kb, note, "# 第一次课\n\n更新后的课堂内容 {t 10:10:00}\n", {}, mode="replace")
    state["ops"].clear()
    r3 = NB.sync_courses(kb, backend="web", log=lambda _: None)
    c3 = r3["courses"][0]
    assert len(c3["updated"]) == 1
    # Verify add happened strictly before delete of old_note_src_id!
    op_kinds = [op[0] for op in state["ops"]]
    assert op_kinds.index("add") < op_kinds.index("delete")
    assert ("delete", old_note_src_id) in state["ops"]
    assert any(s.id == "manual-user-yt-1" for s in state["sources"]["nb-100"])

    # 4. Switch granularity from lecture -> course: cleans up old note:* source, keeps manual source!
    state["ops"].clear()
    r4 = NB.sync_courses(kb, backend="web", granularity="course", log=lambda _: None)
    c4 = r4["courses"][0]
    assert len(c4["added"]) == 1
    assert len(c4["removed_stale"]) == 1
    assert any(s.id == "manual-user-yt-1" for s in state["sources"]["nb-100"])


def test_backup_excludes_nb_secrets_and_migrates_v1_archive(kb, isolated_env):
    cid = make_course(kb)
    lid = make_lecture(kb, cid)
    note = N.get_or_create(kb, "lecture", lid)
    N.save(kb, note, "# 备份测试\n\n内容 {t 10:10:00}\n", {})
    kb.x("INSERT INTO nb_notebooks(course_id,backend,notebook_id,title,granularity,created_at,updated_at) VALUES(?,?,?,?,?,1,1)",
         cid, "web", "nb-xyz", "LW · Java", "lecture")
    kb.x("INSERT INTO nb_sources(course_id,backend,source_key,remote_source_id,title,kind,sha256,synced_at) VALUES(?,?,?,?,?,?,?,1)",
         cid, "web", "guide", "src-001", "导读", "guide", "abc123")
    kb.db.commit()

    # Store sensitive NotebookLM session & GDrive token
    NB.web_storage_path().write_text('{"cookies":[{"name":"SID","value":"SUPER_SECRET_GOOGLE_SID"}]}', "utf-8")
    from lectureweaver.secrets import set_secret

    set_secret(NB.GDRIVE_TOKEN_KEY, '{"refresh_token":"SUPER_SECRET_GDRIVE_REFRESH"}')

    bk = B.create(kb, isolated_env / "bk")
    with tarfile.open(bk["path"]) as tar:
        blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
    assert b"SUPER_SECRET_GOOGLE_SID" not in blob
    assert b"SUPER_SECRET_GDRIVE_REFRESH" not in blob

    out = B.restore(bk["path"], isolated_env / "restored")
    assert out["ok"]
    kb2 = KB(isolated_env / "restored")
    assert kb2.q1("SELECT notebook_id FROM nb_notebooks")["notebook_id"] == "nb-xyz"
    assert kb2.q1("SELECT remote_source_id FROM nb_sources")["remote_source_id"] == "src-001"
    kb2.close()

    # Simulate restoring a legacy schema_version=1 archive that lacked nb_notebooks / nb_sources
    v1_dir = isolated_env / "legacy_v1"
    shutil_kb = KB(v1_dir)
    shutil_kb.close()
    raw_db = sqlite3.connect(v1_dir / "kb.sqlite")
    raw_db.execute("DROP TABLE nb_sources")
    raw_db.execute("DROP TABLE nb_notebooks")
    raw_db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    raw_db.commit()
    raw_db.close()
    assert B.verify_dir(v1_dir)["ok"]
    migrated = KB(v1_dir)
    assert migrated.get_meta("schema_version") == "2"
    assert migrated.q1("SELECT COUNT(*) AS n FROM nb_notebooks")["n"] == 0
    migrated.close()


def test_html_handout_to_clean_markdown_and_large_md_split():
    html_doc = """<!doctype html><html><head><style>.big{color:red;}</style>
    <script type="application/json">
    {"languages":{"cpp":{"label":"C++11","lexer":"cpp","filename":"SeqList.hpp",
      "source":"#include <vector>\\nstruct SeqList { int n = 0; };",
      "fullHtml":"<span class=\\"k\\">#include</span>","sourceZip":"UEsDBBQAAAAI"}}}
    </script>
    <script>console.log('ui runtime');</script></head>
    <body><h1>顺序表讲义</h1><button>复制代码</button>
    <table><tr><th>操作</th><th>复杂度</th></tr><tr><td><code>Insert(i, x)</code></td><td><code>O(n)</code></td></tr></table>
    </body></html>"""
    md = NB.html_to_clean_markdown(html_doc, "顺序表讲义")
    assert ".big{color:red;}" not in md
    assert "console.log" not in md
    assert "UEsDBBQAAAAI" not in md
    assert "复制代码" not in md
    assert "| `Insert(i, x)` | `O(n)` |" in md
    assert "```cpp\n#include <vector>\nstruct SeqList { int n = 0; };\n```" in md

    # Oversized markdown splits cleanly at ## headings
    big_md = "# 总标题\n\n" + "\n\n".join(f"## 第 {i} 节\n\n" + ("知识内容段落 " * 600) for i in range(1, 8))
    parts = NB._make_markdown_items("mat:m_big", "资料 · 巨型讲义", "20-01-巨型讲义", "material", big_md)
    assert len(parts) == 1  # ~50KB < MAX_MD_BYTES
    old_limit = NB.MAX_MD_BYTES
    try:
        NB.MAX_MD_BYTES = 12_000
        split_parts = NB._make_markdown_items("mat:m_big", "资料 · 巨型讲义", "20-01-巨型讲义", "material", big_md)
        assert len(split_parts) >= 3
        assert split_parts[0].key == "mat:m_big:part1"
        assert "第1/" in split_parts[0].title
    finally:
        NB.MAX_MD_BYTES = old_limit


def test_sync_lock_blocks_concurrent_writer_but_allows_dry_run(kb, isolated_env, monkeypatch):
    cid = make_course(kb)
    lid = make_lecture(kb, cid)
    note = N.get_or_create(kb, "lecture", lid)
    N.save(kb, note, "# 并发锁测试\n\n正文 {t 10:10:00}\n", {})
    C.grant(kb, "notebooklm", ["notes", "materials", "learning"])
    NB.web_storage_path().write_text('{"cookies":[]}', "utf-8")

    from lectureweaver.kb import try_lock

    held = try_lock(kb.root / ".locks" / "nb_sync_web.lock", wait=False)
    assert held is not None
    try:
        with pytest.raises(NB.NotebookLMIntegrationError, match="正在同步"):
            NB.sync_courses(kb, backend="web", dry_run=False, log=lambda _: None)
    finally:
        held.close()


def test_partial_upload_failure_cleans_orphan_source_and_continues(kb, isolated_env, monkeypatch):
    from types import SimpleNamespace
    from notebooklm.exceptions import RateLimitError, SourceProcessingError

    cid = make_course(kb)
    lid = make_lecture(kb, cid)
    note = N.get_or_create(kb, "lecture", lid)
    N.save(kb, note, "# 容错测试\n\n正文 {t 10:10:00}\n", {})
    C.grant(kb, "notebooklm", ["notes", "materials", "learning"])
    NB.web_storage_path().write_text('{"cookies":[]}', "utf-8")

    deleted_ids: list[str] = []
    attempts: dict[str, int] = {}

    class FakeSources:
        async def list(self, nb_id):
            return []

        async def add_file(self, nb_id, path, mime_type=None, title=None, wait=True, wait_timeout=180.0):
            attempts[title] = attempts.get(title, 0) + 1
            if "课程导读" in title and attempts[title] == 1:
                raise RateLimitError("transient rate limit")
            if "容错测试" in title:
                err = SourceProcessingError("bad source")
                err.source_id = "orphan-src-999"
                raise err
            return SimpleNamespace(id="ok-src-1", title=title)

        async def delete(self, nb_id, src_id):
            deleted_ids.append(src_id)

    class FakeClient:
        settings = SimpleNamespace(get_account_limits=lambda: _async_val(SimpleNamespace(tier=2, source_limit=300, notebook_limit=500)))
        notebooks = SimpleNamespace(
            list=lambda: _async_val([]),
            create=lambda title: _async_val(SimpleNamespace(id="nb-ft", title=title)),
        )
        sources = FakeSources()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def _async_val(v):
        return v

    async def _from_storage(path, timeout=60.0):
        return FakeClient()

    import notebooklm

    monkeypatch.setattr(notebooklm.NotebookLMClient, "from_storage", _from_storage)
    async def _fast_sleep(_s):
        return None
    monkeypatch.setattr(NB.asyncio, "sleep", _fast_sleep)

    res = NB.sync_courses(kb, backend="web", log=lambda _: None)
    assert res["any_failed"] is True
    c = res["courses"][0]
    assert len(c["added"]) == 1  # guide succeeded after RateLimitError retry!
    assert len(c["failed"]) == 1  # note failed cleanly
    assert "orphan-src-999" in deleted_ids  # orphan stub cleaned up!


def test_web_account_status_awaits_async_get_account_email(isolated_env, monkeypatch):
    from types import SimpleNamespace
    import notebooklm

    NB.web_storage_path().write_text('{"cookies":[]}', "utf-8")

    class FakeAsyncEmailClient:
        async def get_account_email(self):
            return "student@example.com"

        settings = SimpleNamespace(
            get_account_limits=lambda: _async_val(SimpleNamespace(tier=6, source_limit=500, notebook_limit=500))
        )
        notebooks = SimpleNamespace(list=lambda: _async_val([SimpleNamespace(id="n1", title="T1")]))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def _async_val(v):
        return v

    async def _from_storage(path, timeout=15.0):
        return FakeAsyncEmailClient()

    monkeypatch.setattr(notebooklm.NotebookLMClient, "from_storage", _from_storage)
    st = NB.web_account_status(verify_online=True)
    assert st["online_ok"] is True
    assert st["email"] == "student@example.com"
    assert st["source_limit"] == 500
    assert "Ultra" in st["tier_label"]
    json.dumps(st)  # Must be JSON-serializable without unawaited coroutine objects
