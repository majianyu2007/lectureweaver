"""Term calendar, bell schedule and timetable occurrences.

Facts carried over from nwafu-bksxk (verified there against the live
system) and re-checked here on 2026-10-23:

* ``cxxljc.do`` gives the term start (``XQKSRQ``) and week count (``ZZC``);
  week N starts on first_monday + 7*(N-1). ``dqzc.do`` agreed (week 7 on
  2026-10-23 for a term starting 2026-09-07).
* NWAFU runs 冬令/夏令 作息 (summer May 1 – Oct 1) while ``jc.do`` returns
  one fixed table; recordings confirm it (第5节 starts 14:29 in September and
  13:59 in October, i.e. the camera starts ~1 min early).
* ``SKZC`` is a bit string: index i = teaching week i+1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

WINTER = {  # 冬令作息 (Oct 1 – May 1)
    1: ("08:00", "08:45"), 2: ("08:55", "09:40"), 3: ("10:10", "10:55"), 4: ("11:05", "11:50"),
    5: ("14:00", "14:45"), 6: ("14:55", "15:40"), 7: ("16:00", "16:45"), 8: ("16:55", "17:40"),
    9: ("19:00", "19:45"), 10: ("19:50", "20:35"), 11: ("20:40", "21:25"),
}
SUMMER = {  # 夏令作息 (May 1 – Oct 1): afternoon/evening 30 min later
    1: ("08:00", "08:45"), 2: ("08:55", "09:40"), 3: ("10:10", "10:55"), 4: ("11:05", "11:50"),
    5: ("14:30", "15:15"), 6: ("15:25", "16:10"), 7: ("16:30", "17:15"), 8: ("17:25", "18:10"),
    9: ("19:30", "20:15"), 10: ("20:20", "21:05"), 11: ("21:10", "21:55"),
}
WEEKDAYS = "一二三四五六日"


def bell(d: date) -> dict[int, tuple[str, str]]:
    return SUMMER if 5 <= d.month < 10 else WINTER


def period_times(d: date, start: int, end: int) -> tuple[str, str] | None:
    b = bell(d)
    if start in b and end in b:
        return b[start][0], b[end][1]
    return None


def parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def weekday_num(s: str | int | None) -> int | None:
    """'一'..'日' / 1..7 → 1..7."""
    if s is None:
        return None
    if isinstance(s, int):
        return s if 1 <= s <= 7 else None
    s = str(s).strip().replace("星期", "").replace("周", "")
    if s.isdigit():
        return int(s)
    if s == "天":
        return 7
    i = WEEKDAYS.find(s)
    return i + 1 if i >= 0 else None


@dataclass
class TermCalendar:
    code: str
    first_monday: date
    week_count: int

    def week_of(self, d: date) -> int | None:
        delta = (d - self.first_monday).days
        if delta < 0:
            return None
        w = delta // 7 + 1
        return w if w <= self.week_count + 4 else None

    def date_of(self, week: int, weekday: int) -> date:
        return self.first_monday + timedelta(days=(week - 1) * 7 + weekday - 1)

    def week_range(self, week: int) -> tuple[date, date]:
        s = self.first_monday + timedelta(days=(week - 1) * 7)
        return s, s + timedelta(days=6)


def calendar_from_ehall(term_code: str, row: dict) -> TermCalendar:
    start = parse_date(str(row["XQKSRQ"]))
    monday = start - timedelta(days=start.weekday())
    return TermCalendar(term_code, monday, int(row.get("ZZC") or 20))


def week_in_bits(bits: str, week: int) -> bool:
    return 1 <= week <= len(bits) and bits[week - 1] == "1"


def weeks_from_label(label: str, max_week: int = 25) -> str:
    """'1-3周,6-11周(单),14周' → bit string. Used when SKZC is missing."""
    on = set()
    for part in re.split(r"[,，]", label or ""):
        m = re.match(r"\s*(\d+)(?:-(\d+))?周?\s*(\((单|双)\))?", part)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2) or a)
        parity = m.group(4)
        for w in range(a, b + 1):
            if parity == "单" and w % 2 == 0:
                continue
            if parity == "双" and w % 2 == 1:
                continue
            on.add(w)
    return "".join("1" if w in on else "0" for w in range(1, max_week + 1))


def overlap(a1: int, a2: int, b1: int, b2: int) -> bool:
    return a1 <= b2 and b1 <= a2


def clean_course_name(name: str) -> str:
    return re.sub(r"^【[^】]*】", "", name or "").strip()


def guess_subject(name: str) -> str:
    """Rough subject kind; only picks the default note emphasis, user can override."""
    n = name or ""
    if re.search(r"实验|实训|实践|上机", n):
        if re.search(r"程序|Java|Python|C\+\+|数据|算法|计算机|软件|网络|编程", n, re.I):
            return "programming_lab"
        return "lab"
    if re.search(r"程序设计|Java|Python|C语言|C\+\+|数据结构|算法|数据库|操作系统|编译|软件|网络|Web|前端", n, re.I):
        return "programming"
    if re.search(r"数学|微积分|代数|几何|概率|统计|离散|数值|运筹|物理|力学|电路", n):
        return "math"
    if re.search(r"英语|日语|俄语|德语|法语|语文|写作", n):
        return "language"
    if re.search(r"马克思|毛泽东|思想|政策|哲学|历史|法|伦理|导论|艺术|文学|形势", n):
        return "humanities"
    if re.search(r"体育", n):
        return "pe"
    return "general"
