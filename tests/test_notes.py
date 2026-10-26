
import pytest

from lectureweaver import notes as N
from conftest import make_course, make_lecture

DRAFT = """# 文件拷贝

老师讲了 read 的返回值 {t 10:10:00}，代码见截图：

{f F001}

> [!tip] AI 补充
> 最后一桶可能不满。
"""


@pytest.fixture
def lec(kb):
    cid = make_course(kb)
    return make_lecture(kb, cid)


def vault_file(kb, nid):
    n = kb.q1("SELECT vault_path FROM notes WHERE id=?", nid)
    return N.notes_root() / n["vault_path"]


def test_render_validates_and_links(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    out = N.save(kb, note, DRAFT, {"model": "test"})
    assert out.exported and out.version == 1
    text = vault_file(kb, note["id"]).read_text("utf-8")
    assert "[10:10](https://ylb.nwafu.edu.cn/" in text and "latestWatchTime=60" in text
    assert "](attachments/" in text and "F001.webp" in text
    assert (N.notes_root() / "Java语言程序设计/attachments" / lec / "F001.webp").exists()
    assert "> [!info]- 来源" in text and "lw_id: N-" in text
    # the draft with its markup is kept next to the rendered version
    assert kb.abs(f"notes/{note['id']}/v0001-ai.draft.md").read_text("utf-8") == DRAFT


def test_adjacent_time_points_collapse_to_one_link(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    N.save(kb, note, "# 拷贝\n\n读到 -1 结束 {t 10:10:00} {t 10:11:00} {t 10:12:00}\n", {})
    text = vault_file(kb, note["id"]).read_text("utf-8")
    body = text.split("<!-- lw:sources -->")[0]
    assert body.count("](https://ylb.nwafu.edu.cn/") == 1 and "latestWatchTime=60" in body


def test_style_warnings_flag_ai_habits(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    draft = (
        "# Java语言程序设计 第7周 周二：文件拷贝与缓冲流的完整讲解\n\n"
        "老师用例子说明返回值 {t 10:10:00}，这里 {t 10:11:00} 还有 {t 10:12:00} 以及 {t 10:13:00}。\n\n"
        "这是根据截图推导，不是课堂给出的结论；未伪造运行结果。\n\n## 资料说明\n略\n"
    )
    rr = N.render(kb, note, draft, N.notes_root() / "x.md", N.notes_root())
    w = "\n".join(rr.warnings)
    assert not rr.errors
    assert "标题" in w and "时间点" in w and "资料说明" in w and "伪造" in w
    clean = N.render(kb, note, "# 文件拷贝\n\nread 返回 -1 表示读完 {t 10:10:00}\n", N.notes_root() / "x.md", N.notes_root())
    assert clean.warnings == []


@pytest.mark.parametrize("bad, msg", [
    ("{t 09:00:00}", "不在本课时录像时段内"),
    ("{t 10:58:00}", "不在本课时录像时段内"),  # between the two periods
    ("{f F404}", "不存在"),
    ("{m m_abcdef p.3}", "不存在"),
    ("{x 1}", None),
])
def test_rejects_fabricated_references(kb, lec, bad, msg):
    note = N.get_or_create(kb, "lecture", lec)
    if msg is None:
        N.save(kb, note, "# a\n" + bad, {})  # unknown braces are plain text
        return
    with pytest.raises(N.NoteError, match=msg):
        N.save(kb, note, "# a\n" + bad, {})
    assert kb.q1("SELECT COUNT(*) AS n FROM note_versions")["n"] == 0


def test_user_edits_are_never_silently_overwritten(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    N.save(kb, note, DRAFT, {})
    p = vault_file(kb, note["id"])
    p.write_text(p.read_text("utf-8").replace("代码见截图：", "代码见截图：\n\n我的理解：桶装满才倒出来。"), encoding="utf-8")
    out = N.save(kb, note, DRAFT.replace("文件拷贝", "文件拷贝（复习版）"), {"style": "复习版"})
    assert not out.exported and out.proposal_path
    assert "我的理解：桶装满才倒出来。" in p.read_text("utf-8")
    assert out.missing_user_lines == ["我的理解：桶装满才倒出来。"]
    hist = N.history(kb, note["id"])
    assert [h["author"] for h in hist] == ["ai", "user", "ai"]
    # saving again does not record the same hand edit twice
    N.save(kb, note, DRAFT.replace("文件拷贝", "文件拷贝（复习版）"), {})
    assert [h["author"] for h in N.history(kb, note["id"])].count("user") == 1


def test_new_version_that_keeps_user_text_is_applied(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    N.save(kb, note, DRAFT, {})
    p = vault_file(kb, note["id"])
    p.write_text(p.read_text("utf-8").replace("代码见截图：", "代码见截图：\n\n我的理解：桶装满才倒出来。"), encoding="utf-8")
    keep = DRAFT.replace("代码见截图：", "代码见截图：\n\n我的理解：桶装满才倒出来。").replace("# 文件拷贝", "# 文件拷贝（复习版）")
    out = N.save(kb, note, keep, {})
    assert out.exported
    assert "复习版" in p.read_text("utf-8") and "我的理解" in p.read_text("utf-8")


def test_deleted_content_does_not_come_back_silently(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    N.save(kb, note, DRAFT, {})
    p = vault_file(kb, note["id"])
    p.write_text(p.read_text("utf-8").replace("> 最后一桶可能不满。\n", ""), encoding="utf-8")
    out = N.save(kb, note, DRAFT + "\n补充一句。\n", {})
    assert not out.exported
    assert "最后一桶可能不满" not in p.read_text("utf-8")


def test_merge_keeps_both(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    N.save(kb, note, DRAFT + "\n## 结尾\n\n结束。\n", {})
    p = vault_file(kb, note["id"])
    p.write_text(p.read_text("utf-8").replace("代码见截图：", "代码见截图：\n\n我的理解：桶。"), encoding="utf-8")
    out = N.save(kb, note, (DRAFT + "\n## 结尾\n\n结束。\n").replace("结束。", "结束，下次讲字符流。"), {}, mode="merge")
    assert out.exported, out.message
    t = p.read_text("utf-8")
    assert "我的理解：桶。" in t and "下次讲字符流" in t
    assert N.history(kb, note["id"])[-1]["author"] == "merge"


def test_rollback_and_reexport(kb, lec, isolated_env, monkeypatch):
    note = N.get_or_create(kb, "lecture", lec)
    N.save(kb, note, DRAFT, {})
    N.save(kb, note, DRAFT.replace("文件拷贝", "第二版"), {})
    out = N.rollback(kb, N.get_or_create(kb, "lecture", lec), 1)
    assert out.version == 3
    assert "# 文件拷贝" in vault_file(kb, note["id"]).read_text("utf-8")
    # notes folder moved (e.g. new Mac): everything can be written again
    monkeypatch.setenv("LW_NOTES_DIR", str(isolated_env / "vault2"))
    res = N.reexport_all(kb)
    assert res["written"] == 1
    assert (isolated_env / "vault2/Java语言程序设计/attachments" / lec / "F001.webp").exists()


def test_non_lecture_note_and_material_link(kb, lec, tmp_path):
    from lectureweaver import materials as M

    f = tmp_path / "guide.md"
    f.write_text("# 实验指导书\n\n实现文件拷贝程序，并统计耗时。\n", encoding="utf-8")
    m, new, _ = M.import_file(kb, f, title="实验指导书")
    note = N.get_or_create(kb, "review", course_id="2026-2027-1:1234567", key="第7周复习")
    out = N.save(kb, note, f"# 第7周复习\n\n实验要求见 {{m {m['id']} p.1}}。\n", {})
    t = (N.notes_root() / out.vault_path).read_text("utf-8")
    assert "《实验指导书》 p.1" in t and "file://" in t
    assert out.vault_path.startswith("Java语言程序设计/复习/")


def test_coverage_gaps_point_at_uncited_teaching():
    segs = [{"rec": "r1", "clock": f"10:{m:02d}:00", "text": "讲" * 200} for m in range(10, 40)]
    # cited at the start and the end only: the middle stretch is reported
    gaps = N.coverage_gaps(segs, [N._secs("10:10:00"), N._secs("10:39:00")])
    assert gaps and gaps[0][0] == "10:14" and gaps[0][1] == "10:35"
    assert N.coverage_gaps(segs, [N._secs(f"10:{m:02d}:00") for m in range(10, 40, 5)]) == []


def test_section_without_class_anchor_is_flagged(kb, lec):
    note = N.get_or_create(kb, "lecture", lec)
    draft = (
        "# 拷贝与缓冲\n\n## 1. 文件拷贝\n\n{t 10:10:00} 用 read 的返回值控制写出的字节数，最后一次可能读不满整个缓冲区。\n\n"
        "## 2. 缓冲流\n\n### 读一行\n\n{t 11:05:00} readLine 一次读一行，读到末尾返回 null，循环条件据此判断。\n\n"
        "## 3. NIO 通道\n\n> [!quote] 资料：教材第 11 章\n> FileChannel 可以在两个通道之间直接传输数据，transferTo 一次调用完成拷贝。\n\n"
        "> [!tip] AI 补充\n> 大文件拷贝用 transferTo 往往更快，因为数据不必经过用户态缓冲区。\n\n"
        "## 自测\n\n1. read 返回 -1 表示什么？读到了文件末尾，循环应当结束。\n"
    )
    rr = N.render(kb, note, draft, N.notes_root() / "x.md", N.notes_root())
    w = next(x for x in rr.warnings if "没有任何课堂时间点" in x)
    # a section counts its sub-sections; summaries and quizzes are exempt
    assert "“3. NIO 通道”" in w and "2. 缓冲流" not in w and "自测" not in w


def test_logic_words_listed_for_math_courses_only(kb):
    cid = make_course(kb, cid="2026-2027-1:7654321", name="概率论", code="7654321", subject="math")
    lid = make_lecture(kb, cid, lid="2026-10-20_7654321_p3-4", date="2026-10-20")
    note = N.get_or_create(kb, "lecture", lid)
    draft = (
        "# 协方差\n\n{t 10:10:00} 乘积分解 $E(XY)=E(X)E(Y)$ 必须有独立条件。期望的线性性质对任意随机变量成立。\n\n"
        "> [!tip] AI 补充\n> 不相关就足以保证乘积分解，独立只是充分条件。\n\n"
        "{t 11:05:00} $|\\rho|=1$ 当且仅当 $P(Y=aX+b)=1$。\n"
    )
    rr = N.render(kb, note, draft, N.notes_root() / "x.md", N.notes_root())
    assert not rr.errors
    assert any("必须有独立条件" in c for c in rr.checks) and any("当且仅当" in c for c in rr.checks)
    assert not any("充分条件" in c for c in rr.checks)  # callouts are not classroom text
    assert all("{t" not in c for c in rr.checks)

    other = N.get_or_create(kb, "lecture", make_lecture(kb, make_course(kb)))
    assert N.render(kb, other, draft, N.notes_root() / "y.md", N.notes_root()).checks == []


def test_material_page_beyond_pdf_is_an_error(kb, lec):
    kb.x("INSERT INTO materials(id,sha256,filename,kind,title,path,text_path,units,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
         "m_abcdef123456", "x", "ch4.pdf", "pdf", "第4章", "materials/m_abcdef123456/ch4.pdf", "materials/m_abcdef123456/content.md", 32, 0)
    kb.db.commit()
    note = N.get_or_create(kb, "lecture", lec)
    ok = N.render(kb, note, "# 大数定律\n\n{t 10:10:00} 见 {m m_abcdef123456 p.10}\n", N.notes_root() / "x.md", N.notes_root())
    assert not ok.errors and "#page=10" in ok.text
    bad = N.render(kb, note, "# 大数定律\n\n{t 10:10:00} 见 {m m_abcdef123456 p.363}\n", N.notes_root() / "x.md", N.notes_root())
    assert any("只有 32 页" in e for e in bad.errors)


def test_saved_note_converts_back_to_draft_markup(kb, lec):
    kb.x("INSERT INTO materials(id,sha256,filename,kind,title,path,text_path,units,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
         "m_abcdef123456", "x", "第4章.pdf", "pdf", "第4章", "materials/m_abcdef123456/第4章.pdf", "materials/m_abcdef123456/content.md", 32, 0)
    kb.db.commit()
    note = N.get_or_create(kb, "lecture", lec)
    draft = "# 拷贝\n\n## 1. 读写\n\n{t 10:10:00} read 返回读到的字节数。\n\n{f F001}\n\n> [!quote] 资料：第4章\n> 原文。{m m_abcdef123456 p.13}\n\n{t 11:05:30} 第二段。\n"
    N.save(kb, note, draft, {})
    note = N.get_or_create(kb, "lecture", lec)
    text = N.version_text(kb, note["id"], note["current_version"])
    assert "lw:sources" in text
    assert N.to_draft(kb, note, text) == draft
