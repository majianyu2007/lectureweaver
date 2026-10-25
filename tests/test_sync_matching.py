
from lectureweaver.kb import dumps
from lectureweaver.schedule import TermCalendar
from lectureweaver.sync import build_lectures
from datetime import date

from conftest import make_course

CAL = TermCalendar("2026-2027-1", date(2026, 9, 7), 20)


def add_tt(kb, cid, weekday, a, b, weeks="1" * 20, room="A101", raw=None):
    kb.x("INSERT INTO timetable(id,course_id,term,class_id,weekday,start_period,end_period,weeks,weeks_label,room,teacher,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
         f"tt{weekday}{a}{room}", cid, "2026-2027-1", "202620279999990001", weekday, a, b, weeks, "", room, "张老师", dumps(raw or {}))


def add_rec(kb, cid, rid, start, end, seg, group, week="7", room="A101"):
    raw = {"startTime": start, "endTime": end, "week": week}
    kb.x("INSERT INTO recordings(id,course_id,name,start_time,end_time,week,weekday,segment,room,course_table_detail_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         rid, cid, rid, start, end, int(week) if week else None, None, seg, room, group, dumps(raw))


def test_groups_periods_and_matches_timetable(kb):
    cid = make_course(kb)
    add_tt(kb, cid, 2, 3, 4)
    add_rec(kb, cid, "a", "2026-10-20 10:09:00", "2026-10-20 10:55:00", 3, "G1")
    add_rec(kb, cid, "b", "2026-10-20 11:04:00", "2026-10-20 11:50:00", 4, "G1")
    build_lectures(kb, "2026-2027-1", CAL)
    ls = kb.q("SELECT * FROM lectures")
    assert len(ls) == 1
    l = ls[0]
    assert (l["id"], l["week"], l["start_period"], l["end_period"], l["match_status"]) == ("2026-10-20_1234567_p3-4", 7, 3, 4, "matched")
    # re-running keeps the id and does not duplicate
    build_lectures(kb, "2026-2027-1", CAL)
    assert [r["id"] for r in kb.q("SELECT id FROM lectures")] == ["2026-10-20_1234567_p3-4"]


def test_missing_week_is_computed_and_noted(kb):
    cid = make_course(kb)
    add_tt(kb, cid, 2, 3, 4)
    add_rec(kb, cid, "a", "2026-10-20 10:09:00", "2026-10-20 10:55:00", 3, "G1", week=None)
    build_lectures(kb, "2026-2027-1", CAL)
    l = kb.q1("SELECT * FROM lectures")
    assert l["week"] == 7
    assert "按校历推算" in l["match_note"]
    assert "缺少第4节" in l["match_note"]


def test_same_period_twice_is_kept_not_deduplicated(kb):
    cid = make_course(kb)
    add_tt(kb, cid, 2, 3, 4)
    add_rec(kb, cid, "a", "2026-10-20 10:09:00", "2026-10-20 10:55:00", 3, "G1")
    add_rec(kb, cid, "a2", "2026-10-20 10:09:30", "2026-10-20 10:55:00", 3, "G1")
    build_lectures(kb, "2026-2027-1", CAL)
    roles = {r["id"]: r["role"] for r in kb.q("SELECT id, role FROM recordings")}
    assert sorted(roles.values()) == ["alternate", "primary"]
    assert "多条录像" in kb.q1("SELECT match_note FROM lectures")["match_note"]


def test_unscheduled_and_adhoc(kb):
    cid = make_course(kb)
    add_tt(kb, cid, 2, 3, 4)
    # Saturday make-up class: not in timetable
    add_rec(kb, cid, "s", "2026-10-24 08:00:00", "2026-10-24 08:45:00", 1, "G9")
    # 44-second manual clip right after 3-4节 on 10-20
    add_rec(kb, cid, "a", "2026-10-20 10:09:00", "2026-10-20 11:50:00", 3, "G1")
    add_rec(kb, cid, "clip", "2026-10-20 11:51:00", "2026-10-20 11:51:44", None, None)
    build_lectures(kb, "2026-2027-1", CAL)
    st = {r["id"]: (r["match_status"], r["match_note"]) for r in kb.q("SELECT * FROM lectures")}
    sat = next(v for k, v in st.items() if k.startswith("2026-10-24"))
    assert sat[0] == "unscheduled" and "找不到对应时段" in sat[1]
    clip = next(v for k, v in st.items() if "_g" in k)
    assert clip[0] == "unscheduled" and "临时录制" in clip[1] and "2026-10-20_1234567_p3-4" in clip[1]


def test_ambiguous_slots(kb):
    cid = make_course(kb)
    add_tt(kb, cid, 2, 3, 4, room="A")
    add_tt(kb, cid, 2, 3, 4, room="B")
    add_rec(kb, cid, "a", "2026-10-20 10:09:00", "2026-10-20 10:55:00", 3, "G1", room="C")
    build_lectures(kb, "2026-2027-1", CAL)
    assert kb.q1("SELECT match_status FROM lectures")["match_status"] == "ambiguous"


def test_manual_confirmation_survives_resync(kb):
    cid = make_course(kb)
    add_rec(kb, cid, "a", "2026-10-20 10:09:00", "2026-10-20 10:55:00", 3, "G1")
    build_lectures(kb, "2026-2027-1", CAL)
    kb.x("UPDATE lectures SET match_status='manual', match_note='用户确认'")
    kb.db.commit()
    build_lectures(kb, "2026-2027-1", CAL)
    l = kb.q1("SELECT * FROM lectures")
    assert (l["match_status"], l["match_note"]) == ("manual", "用户确认")


def test_rerunning_a_stage_refreshes_later_stages(kb, monkeypatch):
    from lectureweaver.pipeline import Pipeline, StageResult
    from conftest import make_lecture

    make_lecture(kb, make_course(kb))
    ran = []
    pipe = Pipeline(kb, log=lambda m: None, force={"transcript"})
    for st in ("details", "transcript", "visual", "packet"):
        monkeypatch.setattr(pipe, f"stage_{st}", lambda lec, work, st=st: ran.append(st) or StageResult("done"))
    pipe.run(kb.q1("SELECT * FROM lectures"))
    assert ran == ["transcript", "visual", "packet"]
    ran.clear()
    Pipeline.run(pipe.__class__(kb, log=lambda m: None), kb.q1("SELECT * FROM lectures"))
    assert ran == []


def test_assigning_a_clip_removes_the_empty_lecture_for_good(kb):
    import argparse

    from lectureweaver.commands import cmd_lecture_assign
    from lectureweaver.select import Filters, select_lectures

    cid = make_course(kb)
    add_tt(kb, cid, 2, 3, 4)
    add_rec(kb, cid, "a", "2026-10-20 10:09:00", "2026-10-20 11:50:00", 3, "G1")
    add_rec(kb, cid, "clip", "2026-10-20 11:51:00", "2026-10-20 11:51:44", None, None)
    build_lectures(kb, "2026-2027-1", CAL)
    main = "2026-10-20_1234567_p3-4"
    cmd_lecture_assign(argparse.Namespace(json=False, recording="clip", lecture=main))
    kb.db.commit()
    assert [r["id"] for r in kb.q("SELECT id FROM lectures")] == [main]
    build_lectures(kb, "2026-2027-1", CAL)  # re-sync keeps the merge and the lecture's own times
    l = kb.q1("SELECT * FROM lectures WHERE id=?", main)
    assert [r["id"] for r in kb.q("SELECT id FROM lectures")] == [main]
    assert (l["start_period"], l["end_time"]) == (3, "11:50")
    assert {r["lecture_id"] for r in kb.q("SELECT lecture_id FROM recordings")} == {main}
    assert [r["id"] for r in select_lectures(kb, Filters())] == [main]


def test_ready_filter_lists_processed_lectures_without_notes(kb):
    from conftest import make_lecture
    from lectureweaver import notes as N
    from lectureweaver.select import Filters, select_lectures

    cid = make_course(kb)
    a = make_lecture(kb, cid)
    b = make_lecture(kb, cid, lid="2026-10-22_1234567_p1-2", date="2026-10-22")
    assert [r["id"] for r in select_lectures(kb, Filters(ready=True))] == [a, b]
    N.save(kb, N.get_or_create(kb, "lecture", a), "# 拷贝\n\n读完返回 -1 {t 10:10:00}\n", {})
    assert [r["id"] for r in select_lectures(kb, Filters(ready=True))] == [b]
