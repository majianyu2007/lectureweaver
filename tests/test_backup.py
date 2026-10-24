import tarfile

import pytest

from lectureweaver import backup as B
from lectureweaver import notes as N
from lectureweaver import learning as Lg
from lectureweaver.kb import KB
from conftest import make_course, make_lecture


def populate(kb):
    cid = make_course(kb)
    lid = make_lecture(kb, cid)
    note = N.get_or_create(kb, "lecture", lid)
    N.save(kb, note, "# 笔记\n\n内容 {t 10:10:00}\n\n{f F001}\n", {})
    c = Lg.ensure_concept(kb, "文件拷贝", cid)
    Lg.log_event(kb, c["id"], "answered", "correct", "写对了循环条件")
    return lid, note


def test_roundtrip_keeps_everything(kb, isolated_env):
    lid, note = populate(kb)
    # a hand edit made in the vault must be captured by the backup
    p = N.notes_root() / kb.q1("SELECT vault_path FROM notes")["vault_path"]
    p.write_text(p.read_text("utf-8") + "\n我的补充\n", encoding="utf-8")
    res = B.create(kb, isolated_env / "bk")
    v = B.verify_archive(res["path"])
    assert v["ok"], v["problems"]
    out = B.restore(res["path"], isolated_env / "kb2")
    assert out["ok"], out["problems"]
    kb2 = KB(isolated_env / "kb2")
    assert kb2.q1("SELECT COUNT(*) AS n FROM learning_events")["n"] == 1
    assert kb2.q1("SELECT COUNT(*) AS n FROM note_versions WHERE author='user'")["n"] == 1
    assert kb2.q1("SELECT status FROM stages WHERE lecture_id=? AND stage='visual'", lid)["status"] == "done"
    assert (kb2.root / kb2.q1("SELECT dir FROM lectures")["dir"] / "frames/F001.webp").exists()
    assert B.verify_dir(kb2.root)["ok"]
    kb2.close()


def test_no_secrets_in_backup(kb, isolated_env):
    from lectureweaver.secrets import SessionStore, set_secret

    populate(kb)
    set_secret("nwafu:2025000000", "hunter2-password")
    SessionStore().save({"cookies": [{"name": "CASTGC", "value": "TGT-secret"}]})
    res = B.create(kb, isolated_env / "bk")
    with tarfile.open(res["path"]) as t:
        names = t.getnames()
        blob = b"".join(t.extractfile(m).read() for m in t.getmembers() if m.isfile())
    assert not any("secret" in n or "session" in n for n in names)
    assert b"hunter2-password" not in blob and b"TGT-secret" not in blob


def test_tampered_backup_is_rejected_and_target_untouched(kb, isolated_env):
    populate(kb)
    res = B.create(kb, isolated_env / "bk")
    bad = isolated_env / "bad.tar.gz"
    with tarfile.open(res["path"]) as src, tarfile.open(bad, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name.endswith("transcript.json"):
                data = data.replace("read".encode(), b"READ")
            if data is not None:
                import io

                m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
            else:
                dst.addfile(m)
    target = isolated_env / "kb3"
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(B.BackupError, match="内容不一致"):
        B.restore(bad, target, force=True)
    assert (target / "keep.txt").exists()


def test_restore_refuses_to_overwrite_without_force(kb, isolated_env):
    populate(kb)
    res = B.create(kb, isolated_env / "bk")
    with pytest.raises(B.BackupError, match="--force"):
        B.restore(res["path"], kb.root)
