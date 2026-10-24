
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
    assert "[10:10:00](https://ylb.nwafu.edu.cn/" in text and "latestWatchTime=60" in text
    assert "](attachments/" in text and "F001.webp" in text
    assert (N.notes_root() / "Java语言程序设计/attachments" / lec / "F001.webp").exists()
    assert "## 资料说明" in text and "lw_id: N-" in text


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
    note = N.get_or_create(kb, "review", course_id="2026-2027-1:2094300", key="第7周复习")
    out = N.save(kb, note, f"# 第7周复习\n\n实验要求见 {{m {m['id']} p.1}}。\n", {})
    t = (N.notes_root() / out.vault_path).read_text("utf-8")
    assert "《实验指导书》 p.1" in t and "file://" in t
    assert out.vault_path.startswith("Java语言程序设计/复习/")
