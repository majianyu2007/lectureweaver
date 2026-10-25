"""Turn user-level filters into a set of lectures.

The AI harness translates natural language ("第七周的 Java"、"数据库还没整理
过的"、"10月12日到18日") into these flags; this module only has to be
predictable and explain what it matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .kb import KB, loads
from .schedule import parse_date

DONE_STAGES = ("transcript", "visual", "packet")


class SelectError(Exception):
    pass


def find_courses(kb: KB, query: str | None, term: str | None = None) -> list:
    rows = kb.q("SELECT * FROM courses" + (" WHERE term=?" if term else "") + " ORDER BY term DESC, name", *([term] if term else []))
    if not query:
        return rows
    q = query.strip().lower()
    exact, partial = [], []
    for r in rows:
        names = [r["name"], r["code"] or "", r["id"]] + loads(r["aliases"], [])
        low = [n.lower() for n in names if n]
        if q in low:
            exact.append(r)
        elif any(q in n for n in low) or _loose(q, r["name"].lower()):
            partial.append(r)
    return exact or partial


def _loose(q: str, name: str) -> bool:
    """'数据库' matches '数据库原理与应用'; 'java' matches 'Java语言程序设计'; chars in order."""
    it = iter(name)
    return len(q) >= 2 and all(ch in it for ch in q)


@dataclass
class Filters:
    course: str | None = None
    week: str | None = None  # '7' | '5-7'
    date: str | None = None  # '2026-10-19'
    since: str | None = None
    until: str | None = None
    lecture: list[str] | None = None
    unprocessed: bool = False
    ready: bool = False  # materials processed, no lecture note yet
    status: str | None = None
    term: str | None = None
    limit: int | None = None


def current_term(kb: KB) -> str | None:
    r = kb.q1("SELECT term FROM courses ORDER BY term DESC LIMIT 1")
    return r["term"] if r else None


def select_lectures(kb: KB, f: Filters) -> list:
    where, args = ["1=1"], []
    if f.lecture:
        marks = ",".join("?" * len(f.lecture))
        rows = kb.q(f"SELECT l.*, c.name AS course_name FROM lectures l JOIN courses c ON c.id=l.course_id WHERE l.id IN ({marks})", *f.lecture)
        missing = set(f.lecture) - {r["id"] for r in rows}
        if missing:
            raise SelectError(f"找不到课时：{', '.join(sorted(missing))}")
        return rows
    term = f.term or current_term(kb)
    if term:
        where.append("c.term=?")
        args.append(term)
    if f.course:
        cs = find_courses(kb, f.course, term)
        if not cs:
            raise SelectError(f"没有匹配“{f.course}”的课程。可用 `lw courses` 查看课程名，或用 `lw course alias` 添加别名")
        where.append(f"l.course_id IN ({','.join('?' * len(cs))})")
        args += [c["id"] for c in cs]
    if f.week:
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", f.week)
        if not m:
            raise SelectError("周次格式应为 7 或 5-7")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        where.append("l.week BETWEEN ? AND ?")
        args += [a, b]
    if f.date:
        where.append("l.date=?")
        args.append(_norm_date(f.date))
    if f.since:
        where.append("l.date>=?")
        args.append(_norm_date(f.since))
    if f.until:
        where.append("l.date<=?")
        args.append(_norm_date(f.until))
    if f.status:
        where.append("l.match_status=?")
        args.append(f.status)
    # A lecture whose recordings were all merged elsewhere has nothing to process.
    where.append("EXISTS (SELECT 1 FROM recordings r WHERE r.lecture_id=l.id)")
    rows = kb.q(
        f"SELECT l.*, c.name AS course_name FROM lectures l JOIN courses c ON c.id=l.course_id WHERE {' AND '.join(where)} ORDER BY l.date, l.start_period",
        *args,
    )
    if f.unprocessed:
        rows = [r for r in rows if not is_processed(kb, r["id"])]
    if f.ready:
        rows = [r for r in rows if is_processed(kb, r["id"]) and not has_note(kb, r["id"])]
    if f.limit:
        rows = rows[: f.limit]
    return rows


def has_note(kb: KB, lecture_id: str) -> bool:
    return kb.q1("SELECT 1 FROM notes WHERE lecture_id=? AND kind='lecture' AND current_version>0", lecture_id) is not None


def is_processed(kb: KB, lecture_id: str) -> bool:
    st = {r["stage"]: r["status"] for r in kb.q("SELECT stage,status FROM stages WHERE lecture_id=?", lecture_id)}
    return all(st.get(s) in ("done", "skipped") for s in DONE_STAGES)


def _norm_date(s: str) -> str:
    s = s.strip()
    if s in ("今天", "today"):
        return date.today().isoformat()
    if s in ("昨天", "yesterday"):
        return (date.today() - timedelta(days=1)).isoformat()
    m = re.fullmatch(r"(\d{1,2})[-/.月](\d{1,2})日?", s)
    if m:
        return date(date.today().year, int(m.group(1)), int(m.group(2))).isoformat()
    return parse_date(s.replace("/", "-").replace(".", "-")).isoformat()
