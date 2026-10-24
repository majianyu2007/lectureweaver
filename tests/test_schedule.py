from datetime import date

from lectureweaver.schedule import (TermCalendar, bell, clean_course_name, guess_subject, period_times, week_in_bits,
                                    weekday_num, weeks_from_label)


def test_week_of_matches_school():
    # School said week 7 on 2026-10-23 for a term starting Monday 2026-09-07.
    cal = TermCalendar("2026-2027-1", date(2026, 9, 7), 20)
    assert cal.week_of(date(2026, 10, 23)) == 7
    assert cal.week_of(date(2026, 9, 7)) == 1
    assert cal.week_of(date(2026, 9, 6)) is None
    assert cal.date_of(7, 5) == date(2026, 10, 23)


def test_bell_seasons_match_recordings():
    # recordings: 第5节 started 14:29 on 09-07 (summer) and 13:59 on 10-12 (winter)
    assert period_times(date(2026, 9, 7), 5, 6) == ("14:30", "16:10")
    assert period_times(date(2026, 10, 12), 5, 6) == ("14:00", "15:40")
    assert bell(date(2026, 5, 1))[5][0] == "14:30"
    assert bell(date(2026, 10, 1))[5][0] == "14:00"


def test_week_bits_and_labels():
    assert week_in_bits("00010000000000000000", 4)
    assert not week_in_bits("00010000000000000000", 3)
    bits = weeks_from_label("1-3周,6-11周(单),14周")
    on = [i + 1 for i, b in enumerate(bits) if b == "1"]
    assert on == [1, 2, 3, 7, 9, 11, 14]


def test_misc():
    assert weekday_num("一") == 1 and weekday_num("星期日") == 7 and weekday_num(3) == 3
    assert clean_course_name("【本】Java语言程序设计") == "Java语言程序设计"
    assert guess_subject("概率论与数理统计") == "math"
    assert guess_subject("数据结构实验") == "programming_lab"
    assert guess_subject("马克思主义基本原理") == "humanities"
