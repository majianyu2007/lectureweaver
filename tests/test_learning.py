import time

from lectureweaver import learning as Lg
from conftest import make_course


def test_explaining_is_not_mastery(kb):
    c = Lg.ensure_concept(kb, "关系代数除法", make_course(kb))
    d = Lg.log_event(kb, c["id"], "explained", evidence="AI 讲解了定义")
    assert d["state"] == "introduced"


def test_progression_and_misconception(kb, monkeypatch):
    c = Lg.ensure_concept(kb, "除法", None)
    d = Lg.log_event(kb, c["id"], "answered", "wrong", "把除法当成差集", misconception="把 R÷S 理解成 R−S")
    assert d["state"] == "shaky" and d["open_misconceptions"]
    d = Lg.log_event(kb, c["id"], "answered", "correct", "正确求出选了全部课程的学生")
    assert d["state"] == "shaky"  # misconception still open
    mid = kb.q1("SELECT id FROM misconceptions")["id"]
    Lg.resolve_misconception(kb, mid, "能说明除法与差的区别")
    assert Lg.derive(kb, c["id"])["state"] == "working"
    # correct again on another day with an application question -> solid
    t = time.time() + 2 * 86400
    kb.x("INSERT INTO learning_events(concept_id,ts,kind,outcome,evidence,detail) VALUES(?,?,?,?,?,?)", c["id"], t, "answered", "correct", "应用题做对", '{"type": "apply"}')
    kb.db.commit()
    assert Lg.derive(kb, c["id"])["state"] == "solid"


def test_self_report_and_manual_override(kb):
    c = Lg.ensure_concept(kb, "缓冲流", None)
    Lg.log_event(kb, c["id"], "answered", "correct", "说出 readLine 的作用")
    d = Lg.log_event(kb, c["id"], "self_report", "unsure", "还是不懂为什么要 flush")
    assert d["state"] == "shaky"
    Lg.set_manual(kb, c["id"], "working", "用户说已经弄懂了")
    assert Lg.derive(kb, c["id"])["state"] == "working"
    time.sleep(0.01)
    Lg.log_event(kb, c["id"], "answered", "wrong", "flush 和 close 说反了")
    assert Lg.derive(kb, c["id"])["state"] == "shaky"  # new evidence beats the override


def test_requires_evidence(kb):
    import pytest

    c = Lg.ensure_concept(kb, "x", None)
    with pytest.raises(Lg.LearnError):
        Lg.log_event(kb, c["id"], "answered", "correct", None)


def test_next_steps_order(kb):
    a = Lg.ensure_concept(kb, "A", None)
    b = Lg.ensure_concept(kb, "B", None)
    Lg.log_event(kb, a["id"], "explained")
    Lg.log_event(kb, b["id"], "answered", "wrong", "错", misconception="混淆")
    names = [x["name"] for x in Lg.next_steps(kb)]
    assert names == ["B", "A"]
