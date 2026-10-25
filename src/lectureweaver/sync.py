"""Sync courses, timetable and 课堂实录 metadata into the knowledge base.

Only metadata is fetched here (a few hundred KB). Grouping and matching:

* Recordings of one class meeting share ``courseTableDetailId`` on the
  platform (each recording is one 45-min period). They become one lecture.
* Courses are linked across systems by teaching-class id: ylb ``classIds``
  == ehall ``JXBID`` (verified for every course on 2026-10-23); names are only
  a fallback.
* Each lecture is checked against the official timetable (weekday, teaching
  week bit, period overlap). Results: ``matched``; ``unscheduled`` (no slot in
  the timetable, e.g. a make-up class); ``ambiguous`` (several slots fit);
  ``incomplete`` (platform data missing times). Nothing is guessed silently;
  the reason is stored in ``match_note``.
* Existing lectures are never deleted or renamed by a re-sync; a lecture
  keeps its id once created (looked up by platform group first).
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .kb import KB, dumps, loads
from .schedule import (
    TermCalendar,
    calendar_from_ehall,
    clean_course_name,
    guess_subject,
    overlap,
    parse_date,
    period_times,
    week_in_bits,
    weekday_num,
)


@dataclass
class SyncReport:
    term: str = ""
    courses: int = 0
    timetable_rows: int = 0
    recordings: int = 0
    new_recordings: int = 0
    lectures: int = 0
    new_lectures: int = 0
    flagged: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _slug(s: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", s).strip("_")[:60]


def _class_ids(v) -> list[str]:
    out = []
    for x in str(v or "").split(","):
        x = x.strip()
        if not x:
            continue
        m = re.search(r"(\d{18})$", x)  # 'XXS01' + 18-digit JXBID → JXBID suffix
        out.append(m.group(1) if m else x)
    return sorted(set(out))


def sync(kb: KB, session, term: str | None = None, with_timetable: bool = True, log=print) -> SyncReport:
    y = session.ylb()
    rep = SyncReport()
    terms = y.recent_terms(previous=8) or []
    now_term = y.now_term()
    if term:
        t = next((x for x in terms if x.get("id") == term), None)
        if not t:
            raise ValueError(f"云课堂中没有学期 {term}；可用：{', '.join(x.get('id', '') for x in terms)}")
    else:
        t = now_term
    term_code = t["id"]
    rep.term = term_code
    school_year, term_no = t["schoolYear"], t["term"]

    # ---------------------------------------------------------- timetable (ehall)
    cal: TermCalendar | None = None
    ehall_sessions: list[dict] = []
    if with_timetable:
        try:
            raw = session.ehall().timetable_raw(term_code)
            cal = calendar_from_ehall(term_code, raw["calendar"][0])
            ehall_sessions = raw["sessions"]
            kb.set_setting(f"adjustments:{term_code}", raw.get("adjustments") or [])
            kb.x(
                """INSERT INTO terms(code,name,start_date,end_date,first_monday,week_count,periods,synced_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                   start_date=excluded.start_date,end_date=excluded.end_date,first_monday=excluded.first_monday,
                   week_count=excluded.week_count,periods=excluded.periods,synced_at=excluded.synced_at""",
                term_code, t.get("schoolYearTermNickName") or term_code, t.get("startDate"), t.get("endDate"),
                cal.first_monday.isoformat(), cal.week_count, dumps(raw["periods"]), time.time(),
            )
            kb.db.commit()
        except Exception as e:  # noqa: BLE001 - timetable is an enhancement, not a hard requirement
            rep.warnings.append(f"教务课表获取失败，将只用云课堂自带的周次信息：{e}")
    if cal is None:
        row = kb.q1("SELECT * FROM terms WHERE code=?", term_code)
        if row and row["first_monday"]:
            cal = TermCalendar(term_code, parse_date(row["first_monday"]), row["week_count"] or 20)
        elif t.get("startDate"):
            sd = parse_date(t["startDate"])
            from datetime import timedelta

            cal = TermCalendar(term_code, sd - timedelta(days=sd.weekday()), 20)
            rep.warnings.append("学期第一周按云课堂学期开始日期推算，未经教务系统核对")

    # ---------------------------------------------------------- courses
    now = time.time()
    by_class: dict[str, str] = {}
    courses_by_code: dict[str, dict] = {}
    for s in ehall_sessions:
        code = str(s.get("KCH") or "")
        c = courses_by_code.setdefault(
            code,
            {"name": clean_course_name(s.get("KCM", "")), "teacher": s.get("SKJS") or "", "class_ids": set(), "category": s.get("KCXZDM_DISPLAY")},
        )
        if s.get("JXBID"):
            c["class_ids"].add(str(s["JXBID"]))
    for code, c in courses_by_code.items():
        cid = f"{term_code}:{code}"
        _upsert_course(kb, cid, term_code, c["name"], code, c["teacher"], sorted(c["class_ids"]), None, c["category"], now)
        for x in c["class_ids"]:
            by_class[x] = cid

    rec_courses = y.recording_courses(school_year, term_no) or []
    ylb_course_map: dict[str, str] = {}
    for rc in rec_courses:
        ids = _class_ids(rc.get("classIds"))
        cid = next((by_class[i] for i in ids if i in by_class), None)
        name = clean_course_name(rc.get("courseName", ""))
        if cid is None:
            # Name fallback (only when unique), else a ylb-only course.
            cands = [k for k, v in courses_by_code.items() if v["name"] == name]
            if len(cands) == 1:
                cid = f"{term_code}:{cands[0]}"
                rep.warnings.append(f"《{name}》按课程名关联到教务课表（教学班号不一致）")
            else:
                cid = f"{term_code}:ylb-{rc['courseId']}"
                _upsert_course(kb, cid, term_code, name, None, (rc.get("teacherNames") or "").split(",")[0], ids, rc["courseId"], None, now)
        kb.x("UPDATE courses SET ylb_course_id=? WHERE id=?", rc.get("courseId"), cid)
        ylb_course_map[rc["courseId"]] = cid
    kb.db.commit()
    rep.courses = len(kb.q("SELECT id FROM courses WHERE term=?", term_code))

    # ---------------------------------------------------------- timetable rows
    if ehall_sessions:
        kb.x("DELETE FROM timetable WHERE term=?", term_code)
        for s in ehall_sessions:
            cid = f"{term_code}:{s.get('KCH')}"
            kb.x(
                "INSERT OR REPLACE INTO timetable(id,course_id,term,class_id,weekday,start_period,end_period,weeks,weeks_label,room,teacher,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                f"{s.get('KBID') or s.get('WID')}-{s.get('SKXQ')}-{s.get('KSJC')}-{s.get('SKZC')}",
                cid, term_code, str(s.get("JXBID") or ""), int(s.get("SKXQ") or 0), int(s.get("KSJC") or 0), int(s.get("JSJC") or 0),
                str(s.get("SKZC") or ""), s.get("ZCMC") or "", s.get("JASMC") or "", s.get("SKJS") or "", dumps(s),
            )
        kb.db.commit()
        rep.timetable_rows = len(ehall_sessions)

    # ---------------------------------------------------------- recordings
    for rc in rec_courses:
        cid = ylb_course_map[rc["courseId"]]
        recs = y.all_recordings(rc.get("classIds"), school_year, term_no)
        for r in recs:
            exists = kb.q1("SELECT id FROM recordings WHERE id=?", r["id"])
            if not exists:
                rep.new_recordings += 1
            kb.x(
                """INSERT INTO recordings(id,course_id,name,start_time,end_time,week,weekday,segment,room,teacher,task_id,
                     course_table_id,course_table_detail_id,raw,fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET course_id=excluded.course_id,name=excluded.name,start_time=excluded.start_time,
                     end_time=excluded.end_time,week=excluded.week,weekday=excluded.weekday,segment=excluded.segment,room=excluded.room,
                     teacher=excluded.teacher,task_id=excluded.task_id,course_table_id=excluded.course_table_id,
                     course_table_detail_id=excluded.course_table_detail_id,raw=excluded.raw,fetched_at=excluded.fetched_at""",
                r["id"], cid, r.get("videoInfoName"), r.get("startTime"), r.get("endTime"),
                _int(r.get("week")), weekday_num(r.get("weekDay")), _int(r.get("segment")), r.get("classroomName"),
                r.get("teacherNames"), r.get("taskId"), r.get("courseTableId"), r.get("courseTableDetailId"), dumps(r), now,
            )
            rep.recordings += 1
        kb.db.commit()

    # ---------------------------------------------------------- lectures
    before = {r["id"] for r in kb.q("SELECT id FROM lectures")}
    build_lectures(kb, term_code, cal)
    after = kb.q("SELECT id, match_status FROM lectures WHERE course_id IN (SELECT id FROM courses WHERE term=?)", term_code)
    rep.lectures = len(after)
    rep.new_lectures = len({r["id"] for r in after} - before)
    rep.flagged = [r["id"] for r in after if r["match_status"] not in ("matched", "manual")]
    return rep


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _upsert_course(kb: KB, cid, term, name, code, teacher, class_ids, ylb_id, category, now):
    row = kb.q1("SELECT id, profile, class_ids FROM courses WHERE id=?", cid)
    if row:
        merged = sorted(set(loads(row["class_ids"], [])) | set(class_ids))
        kb.x(
            "UPDATE courses SET name=?, code=COALESCE(?,code), teacher=COALESCE(NULLIF(?,''),teacher), class_ids=?, ylb_course_id=COALESCE(?,ylb_course_id), category=COALESCE(?,category), updated_at=? WHERE id=?",
            name, code, teacher, dumps(merged), ylb_id, category, now, cid,
        )
    else:
        kb.x(
            "INSERT INTO courses(id,term,name,code,teacher,class_ids,ylb_course_id,category,profile,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            cid, term, name, code, teacher, dumps(class_ids), ylb_id, category, dumps({"subject": guess_subject(name)}), now, now,
        )


def build_lectures(kb: KB, term_code: str, cal: TermCalendar | None) -> None:
    recs = kb.q(
        "SELECT r.*, c.code AS course_code, c.name AS course_name FROM recordings r JOIN courses c ON c.id=r.course_id WHERE c.term=?",
        term_code,
    )
    groups: dict[tuple, list] = defaultdict(list)
    for r in recs:
        raw = loads(r["raw"], {})
        if r["course_table_detail_id"]:
            key = (r["course_id"], "g", r["course_table_detail_id"])
        else:
            d = (r["start_time"] or raw.get("startTime") or "")[:10] or "unknown"
            key = (r["course_id"], "d", d)
        groups[key].append(r)

    adjustments = kb.get_setting(f"adjustments:{term_code}", []) or []
    tt = defaultdict(list)
    for row in kb.q("SELECT * FROM timetable WHERE term=?", term_code):
        tt[row["course_id"]].append(row)

    now = time.time()
    for (course_id, kind, gkey), rs in groups.items():
        rs = sorted(rs, key=lambda r: (r["start_time"] or "", r["segment"] or 0))
        course = kb.q1("SELECT * FROM courses WHERE id=?", course_id)
        notes: list[str] = []
        status = "matched"
        dates = sorted({(r["start_time"] or "")[:10] for r in rs if r["start_time"]})
        if not dates:
            # e.g. platform rows without time; keep them visible, never guess the date.
            d_str = None
            status = "incomplete"
            notes.append("云课堂未提供录像时间")
        else:
            d_str = dates[0]
            if len(dates) > 1:
                status = "ambiguous"
                notes.append(f"同一平台分组跨多个日期：{', '.join(dates)}")
        segs = sorted({r["segment"] for r in rs if r["segment"]})
        timed = [r for r in rs if r["start_time"]]
        if any(not r["start_time"] for r in rs) and status == "matched":
            status = "incomplete"
            notes.append("部分录像缺少时间信息")

        d = parse_date(d_str) if d_str else None
        week = cal.week_of(d) if (cal and d) else None
        plat_weeks = {r["week"] for r in rs if r["week"]}
        if week is None and len(plat_weeks) == 1:
            week = next(iter(plat_weeks))
            notes.append("周次取自云课堂（未能按校历推算）")
        elif week is not None and plat_weeks and plat_weeks != {week}:
            notes.append(f"云课堂标注第{sorted(plat_weeks)}周，按校历应为第{week}周（以校历为准）")
        if week is not None and not plat_weeks:
            notes.append("云课堂未标注周次，已按校历推算")
        wd = d.isoweekday() if d else None

        # duplicates inside a lecture: same period recorded twice
        seen_seg: dict[int, str] = {}
        roles = {}
        for r in rs:
            sg = r["segment"]
            if sg and sg in seen_seg:
                roles[r["id"]] = "alternate"
                notes.append(f"第{sg}节有多条录像（{seen_seg[sg][-6:]} / {r['id'][-6:]}），均保留，未视为重复")
            else:
                roles[r["id"]] = "primary"
                if sg:
                    seen_seg[sg] = r["id"]

        start_p, end_p = (segs[0], segs[-1]) if segs else (None, None)
        slot = None
        if d and start_p and course_id in tt:
            cands = [
                row for row in tt[course_id]
                if row["weekday"] == wd and (week is None or week_in_bits(row["weeks"], week)) and overlap(row["start_period"], row["end_period"], start_p, end_p)
            ]
            rooms = {r["room"] for r in rs if r["room"]}
            if len(cands) > 1:
                same_room = [c for c in cands if c["room"] in rooms]
                if len(same_room) == 1:
                    cands = same_room
            if len(cands) == 1:
                slot = cands[0]
                if slot["room"] and rooms and slot["room"] not in rooms:
                    notes.append(f"教室不一致：课表 {slot['room']}，录像 {', '.join(sorted(rooms))}")
            elif len(cands) > 1:
                status = "ambiguous" if status == "matched" else status
                notes.append("课表中有多个时段都符合：" + "; ".join(f"周{c['weekday']} {c['start_period']}-{c['end_period']}节 {c['room']}" for c in cands))
            else:
                if status == "matched":
                    status = "unscheduled"
                notes.append("课表中找不到对应时段（可能是调课/补课，或课表未同步）")
        elif d and course_id not in tt:
            if status == "matched":
                status = "unscheduled"
            notes.append("该课程不在教务课表中（只有云课堂数据）")

        if slot is not None:
            start_p, end_p = slot["start_period"], slot["end_period"]
            adj = _adjustment_for(adjustments, course["code"] if course else None, week, wd, start_p, end_p)
            if adj:
                notes.append(adj)
        if d and not segs and status == "matched":
            dur = _duration_s(rs)
            status = "unscheduled"
            notes.append(f"临时录制（非排课录像{'，约 ' + str(dur) + ' 秒' if dur is not None else ''}）")
            near = _nearby_lecture(kb, course_id, d_str, timed)
            if near:
                notes.append(f"时间上紧挨着课时 {near}，可能属于该次课；如确认可运行 lw lecture assign")
        if start_p and segs and (segs[0] != start_p or segs[-1] != end_p):
            missing = [p for p in range(start_p, end_p + 1) if p not in segs]
            if missing:
                notes.append(f"缺少第{','.join(map(str, missing))}节的录像")

        existing = None
        if kind == "g":
            existing = kb.q1("SELECT * FROM lectures WHERE platform_group=?", gkey)
        if existing is None:
            # Keep the id of the lecture these recordings already belong to.
            ids = [r["lecture_id"] for r in rs if r["lecture_id"]]
            if ids:
                existing = kb.q1("SELECT * FROM lectures WHERE id=?", max(set(ids), key=ids.count))
        code = course["code"] or course_id.split(":")[-1][:12]
        if existing:
            lid = existing["id"]
        else:
            ptxt = f"p{start_p}-{end_p}" if start_p else f"g{gkey[-6:]}"
            lid = f"{d_str or 'nodate'}_{code}_{ptxt}"
            n = 2
            while kb.q1("SELECT id FROM lectures WHERE id=?", lid):
                lid = f"{d_str or 'nodate'}_{code}_{ptxt}_{n}"
                n += 1
        times = period_times(d, start_p, end_p) if (d and start_p) else None
        if timed:
            st = min(r["start_time"] for r in timed)[11:16]
            et = max((r["end_time"] or r["start_time"]) for r in timed)[11:16]
        else:
            st, et = (times or (None, None))
        teacher = next((r["teacher"] for r in rs if r["teacher"]), course["teacher"])
        room = next((r["room"] for r in rs if r["room"]), slot["room"] if slot else None)
        ldir = f"lectures/{term_code}/{_slug(code + '-' + course['name'])}/{lid}"
        if existing and existing["match_status"] == "manual" and existing["platform_group"] and existing["platform_group"] != (gkey if kind == "g" else None):
            # Recordings the user merged into another lecture (lw lecture
            # assign): they stay there, and must not overwrite its fields.
            continue
        if existing:
            manual = existing["match_status"] == "manual"
            kb.x(
                """UPDATE lectures SET date=?, week=?, weekday=?, start_period=?, end_period=?, start_time=?, end_time=?,
                   room=?, teacher=?, match_status=?, match_note=?, updated_at=? WHERE id=?""",
                d_str, week, wd, start_p, end_p, st, et, room, teacher,
                existing["match_status"] if manual else status,
                existing["match_note"] if manual else "；".join(dict.fromkeys(notes)),
                now, lid,
            )
        else:
            kb.x(
                """INSERT INTO lectures(id,course_id,date,week,weekday,start_period,end_period,start_time,end_time,room,teacher,
                   platform_group,match_status,match_note,dir,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                lid, course_id, d_str, week, wd, start_p, end_p, st, et, room, teacher,
                gkey if kind == "g" else None, status, "；".join(dict.fromkeys(notes)), ldir, now, now,
            )
        for r in rs:
            kb.x("UPDATE recordings SET lecture_id=?, role=? WHERE id=?", lid, roles.get(r["id"], "primary"), r["id"])
    # Drop lectures left empty by regrouping, but only if nothing refers to them.
    kb.x(
        """DELETE FROM lectures WHERE id NOT IN (SELECT DISTINCT lecture_id FROM recordings WHERE lecture_id IS NOT NULL)
           AND id NOT IN (SELECT lecture_id FROM stages) AND id NOT IN (SELECT lecture_id FROM notes WHERE lecture_id IS NOT NULL)
           AND match_status != 'manual'"""
    )
    kb.db.commit()


def _adjustment_for(adjs: list[dict], code, week, wd, a, b) -> str | None:
    for x in adjs:
        if code and str(x.get("KCH")) != str(code):
            continue
        bits = str(x.get("XSKZC") or "")
        if week and week_in_bits(bits, week) and _int(x.get("XSKXQ")) == wd and overlap(_int(x.get("XKSJC")) or 0, _int(x.get("XJSJC")) or 0, a, b):
            return (
                f"调课：原 {x.get('ZCMC')} 周{x.get('SKXQ')} {x.get('KSJC')}-{x.get('JSJC')}节 {x.get('JASMC') or ''}"
                f" → {x.get('XZCMC')} 周{x.get('XSKXQ')} {x.get('XKSJC')}-{x.get('XJSJC')}节 {x.get('XJASMC') or ''}"
            ).strip()
    return None


def _duration_s(rs) -> int | None:
    from datetime import datetime

    try:
        s = min(r["start_time"] for r in rs if r["start_time"])
        e = max(r["end_time"] for r in rs if r["end_time"])
        return int((datetime.fromisoformat(e) - datetime.fromisoformat(s)).total_seconds())
    except (ValueError, TypeError):
        return None


def _nearby_lecture(kb: KB, course_id: str, d_str: str, timed) -> str | None:
    from datetime import datetime

    if not timed:
        return None
    st = datetime.fromisoformat(min(r["start_time"] for r in timed))
    for l in kb.q("SELECT id,start_time,end_time,date FROM lectures WHERE course_id=? AND date=? AND start_period IS NOT NULL", course_id, d_str):
        try:
            ls = datetime.fromisoformat(f"{l['date']} {l['start_time']}")
            le = datetime.fromisoformat(f"{l['date']} {l['end_time']}")
        except (TypeError, ValueError):
            continue
        if (ls - st).total_seconds() < 20 * 60 and (st - le).total_seconds() < 20 * 60:
            return l["id"]
    return None


def lecture_label(row) -> str:
    wd = "一二三四五六日"[row["weekday"] - 1] if row["weekday"] else "?"
    wk = f"第{row['week']}周" if row["week"] else "周次未知"
    per = f"{row['start_period']}-{row['end_period']}节" if row["start_period"] else ""
    return f"{row['date'] or '日期未知'} {wk} 周{wd} {per}".strip()


def today() -> date:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).date()
