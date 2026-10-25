from lectureweaver import materials as M
from lectureweaver.search import search
from conftest import make_course, make_lecture


def test_import_docx_pptx_code(kb, tmp_path):
    import docx
    from pptx import Presentation

    d = docx.Document()
    d.add_heading("实验三 文件操作", 1)
    d.add_paragraph("要求：用 FileInputStream 实现文件拷贝，并比较缓冲区大小对耗时的影响。")
    d.save(tmp_path / "lab.docx")
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "BufferedReader"
    s.placeholders[1].text = "readLine() 一次读一行"
    prs.save(tmp_path / "ch10.pptx")
    (tmp_path / "Copy.java").write_text("class Copy { void run() { fos.write(data, 0, length); } }", encoding="utf-8")
    out = [M.import_file(kb, tmp_path / f) for f in ("lab.docx", "ch10.pptx", "Copy.java")]
    assert [o[0]["kind"] for o in out] == ["document", "slides", "code"]
    again, new, _ = M.import_file(kb, tmp_path / "lab.docx")
    assert not new and again["id"] == out[0][0]["id"]
    assert "slide 1" in M.material_text(kb, out[1][0]["id"], "1")


def test_link_suggest_and_search(kb, tmp_path):
    cid = make_course(kb)
    lid = make_lecture(kb, cid)
    make_lecture(kb, cid, lid="2026-10-13_1234567_p3-4", date="2026-10-13")
    kb.index_replace("transcript", lid, [("[10:10:00] read 方法返回成功读取的字节个数", lid, cid, None)])
    f = tmp_path / "guide.md"
    f.write_text("实验：read 方法 成功读取 字节个数", encoding="utf-8")
    m, _, _ = M.import_file(kb, f)
    M.link(kb, m["id"], "course", cid, "lab_guide")
    sug = M.suggest_lectures(kb, m["id"], cid)
    assert sug and sug[0]["lecture"] in (lid, "2026-10-13_1234567_p3-4")
    hits = search(kb, "字节个数")
    kinds = {h["kind"] for h in hits}
    assert {"transcript", "material"} <= kinds
    t = next(h for h in hits if h["kind"] == "transcript")
    assert t["clock"] == "10:10:00" and t["date"] == "2026-10-20"
    assert search(kb, "读取")  # 2-character query uses LIKE
