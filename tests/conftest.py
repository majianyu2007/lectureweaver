import json
import time

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own KB, state dir and notes dir; no Keychain."""
    monkeypatch.setenv("LW_HOME", str(tmp_path / "kb"))
    monkeypatch.setenv("LW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LW_NOTES_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("LW_NO_KEYCHAIN", "1")
    yield tmp_path


@pytest.fixture
def kb(isolated_env):
    from lectureweaver.kb import KB

    k = KB(isolated_env / "kb")
    yield k
    k.close()


def make_course(kb, cid="2026-2027-1:1234567", name="Java语言程序设计", code="1234567", subject="programming"):
    from lectureweaver.kb import dumps

    kb.x(
        "INSERT INTO courses(id,term,name,code,teacher,class_ids,profile,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        cid, "2026-2027-1", name, code, "张老师", dumps(["202620279999990001"]), dumps({"subject": subject}), time.time(), time.time(),
    )
    kb.db.commit()
    return cid


def make_lecture(kb, course_id, lid="2026-10-20_1234567_p3-4", date="2026-10-20", segs=((3, "10:09:00", "10:55:00"), (4, "11:04:00", "11:49:59"))):
    """A processed lecture with transcript + frames on disk (no network)."""
    from PIL import Image

    from lectureweaver.kb import dumps

    ldir = f"lectures/2026-2027-1/x/{lid}"
    kb.x(
        "INSERT INTO lectures(id,course_id,date,week,weekday,start_period,end_period,start_time,end_time,match_status,dir,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        lid, course_id, date, 7, 2, segs[0][0], segs[-1][0], segs[0][1][:5], segs[-1][2][:5], "matched", ldir, time.time(), time.time(),
    )
    transcript, frames = [], []
    d = kb.abs(ldir)
    (d / "frames").mkdir(parents=True)
    for i, (seg, st, et) in enumerate(segs, 1):
        rid = f"{lid}-rec{i}"
        det = {"vgaViewFiles": [{"id": f"f{i}", "startTime": f"{date} {st}", "endTime": f"{date} {et}"}]}
        kb.x(
            "INSERT INTO recordings(id,lecture_id,course_id,name,start_time,end_time,segment,course_table_id,detail) VALUES(?,?,?,?,?,?,?,?,?)",
            rid, lid, course_id, f"第{seg}节", f"{date} {st}", f"{date} {et}", seg, "CT1", dumps(det),
        )
        h, m, _ = (int(x) for x in st.split(":"))
        transcript.append({"rec": rid, "part": i, "t0": 60.0, "t1": 70.0, "clock": f"{h:02d}:{m + 1:02d}:00", "text": f"第{i}段：read 方法返回成功读取的字节个数", "src": "platform"})
        fid = f"F{i:03d}"
        Image.new("RGB", (64, 36), (255, 255, 255)).save(d / "frames" / f"{fid}.webp")
        frames.append({"id": fid, "rec": rid, "part": i, "stream": "vga", "t": 120, "clock": f"{h:02d}:{m + 2:02d}:00", "file": f"frames/{fid}.webp", "ocr": "fos.write(data,0,length);"})
    (d / "transcript.json").write_text(json.dumps({"segments": transcript, "sources": [{"rec": f"{lid}-rec1", "kind": "platform"}]}, ensure_ascii=False), encoding="utf-8")
    (d / "frames.json").write_text(json.dumps({"frames": frames}, ensure_ascii=False), encoding="utf-8")
    (d / "packet.md").write_text("# 学习包\n", "utf-8")
    for st in ("details", "transcript", "visual", "packet"):
        kb.set_stage(lid, st, "done")
    kb.db.commit()
    return lid
