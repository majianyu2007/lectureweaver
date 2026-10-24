"""Backup, verification, restore and integrity checks of the knowledge base.

A backup is a ``.tar.gz`` with:
* ``kb.sqlite``  - a consistent snapshot taken with SQLite's backup API;
* ``lectures/``, ``materials/``, ``notes/`` - transcripts, screenshots,
  material files, every note version;
* ``manifest.json`` - format version, table counts, and size + SHA-256 of
  every file.

Never included: passwords, API keys, cookies/session tokens (they live in
the Keychain and the machine state dir), the machine's paths (notes dir,
model files) and scratch media. Before snapshotting, edits made in the notes
folder are recorded as note versions so nothing written by hand is lost.

Restore verifies everything *before* touching the target; an existing
knowledge base is moved aside, not deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from . import __version__
from .kb import KB

FORMAT = 1
CONTENT_DIRS = ("lectures", "materials", "notes")
TABLES = ["courses", "timetable", "lectures", "recordings", "stages", "materials", "material_links", "notes", "note_versions",
          "concepts", "concept_refs", "learning_events", "mastery", "misconceptions", "consents", "settings", "terms"]


class BackupError(Exception):
    pass


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def create(kb: KB, out: Path, sync_vault: bool = True) -> dict:
    if sync_vault:
        try:
            from .notes import NoteError, sync_in

            for n in kb.q("SELECT * FROM notes WHERE vault_path IS NOT NULL"):
                try:
                    sync_in(kb, dict(n))
                except NoteError:
                    break
        except ImportError:
            pass
    out = Path(out).expanduser()
    if out.is_dir() or not out.name.endswith((".tar.gz", ".tgz")):
        out = out / f"LectureWeaver-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "kb.sqlite"
        dst = sqlite3.connect(snap)
        kb.db.commit()
        kb.db.backup(dst)
        dst.close()
        files = {"kb.sqlite": {"size": snap.stat().st_size, "sha256": _sha(snap)}}
        for d in CONTENT_DIRS:
            base = kb.root / d
            if not base.exists():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file() and not p.name.endswith((".lw-tmp", ".DS_Store")):
                    rel = p.relative_to(kb.root).as_posix()
                    files[rel] = {"size": p.stat().st_size, "sha256": _sha(p)}
        counts = {t: kb.q1(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in TABLES}
        manifest = {"format": FORMAT, "app_version": __version__, "created_at": time.time(), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "schema_version": kb.get_meta("schema_version"), "counts": counts, "files": files,
                    "excluded": ["密码/API Key（钥匙串）", "Cookie/会话令牌", "本机路径设置", "临时媒体文件"]}
        mpath = Path(td) / "manifest.json"
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
        tmp_out = out.with_suffix(out.suffix + ".part")
        with tarfile.open(tmp_out, "w:gz", compresslevel=6) as tar:
            tar.add(mpath, "manifest.json")
            tar.add(snap, "kb.sqlite")
            for rel in files:
                if rel != "kb.sqlite":
                    tar.add(kb.root / rel, rel)
        os.replace(tmp_out, out)
    return {"path": str(out), "size": out.stat().st_size, "files": len(files), "counts": counts}


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise BackupError(f"备份包含不安全的路径：{m.name}")
        yield m


def verify_dir(root: Path, manifest: dict | None = None) -> dict:
    """Check checksums (if a manifest is given), database integrity and references."""
    problems: list[str] = []
    root = Path(root)
    if manifest:
        for rel, info in manifest["files"].items():
            p = root / rel
            if not p.exists():
                problems.append(f"缺少文件 {rel}")
            elif p.stat().st_size != info["size"] or _sha(p) != info["sha256"]:
                problems.append(f"文件内容不一致 {rel}")
    db = sqlite3.connect(root / "kb.sqlite")
    db.row_factory = sqlite3.Row
    try:
        ic = db.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            problems.append(f"数据库完整性检查失败：{ic}")
        fk = db.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            problems.append(f"外键不一致 {len(fk)} 处")
        if manifest:
            for t, n in manifest["counts"].items():
                got = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                if got != n:
                    problems.append(f"表 {t} 记录数 {got}，应为 {n}")
        for r in db.execute("SELECT note_id, version, path FROM note_versions"):
            if not (root / r["path"]).exists():
                problems.append(f"笔记版本文件缺失 {r['note_id']} v{r['version']}")
        for r in db.execute("SELECT id, path, text_path FROM materials"):
            for col in ("path", "text_path"):
                if r[col] and not (root / r[col]).exists():
                    problems.append(f"资料文件缺失 {r['id']} ({col})")
        for r in db.execute("SELECT s.lecture_id, s.stage, l.dir FROM stages s JOIN lectures l ON l.id=s.lecture_id WHERE s.status='done'"):
            need = {"transcript": "transcript.json", "visual": "frames.json", "packet": "packet.md"}.get(r["stage"])
            if need and not (root / r["dir"] / need).exists():
                problems.append(f"课时 {r['lecture_id']} 标记 {r['stage']} 完成但缺少 {need}")
        for r in db.execute("SELECT dir FROM lectures"):
            fj = root / r["dir"] / "frames.json"
            if fj.exists():
                for f in json.loads(fj.read_text("utf-8")).get("frames", []):
                    if not (root / r["dir"] / f["file"]).exists():
                        problems.append(f"截图缺失 {r['dir']}/{f['file']}")
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        db.close()
    return {"ok": not problems, "problems": problems[:50], "problem_count": len(problems), "counts": counts}


def verify_archive(archive: Path) -> dict:
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(td, members=list(_safe_members(tar)), filter="data")
        manifest = json.loads((Path(td) / "manifest.json").read_text("utf-8"))
        res = verify_dir(Path(td), manifest)
        res["manifest"] = {k: manifest[k] for k in ("format", "app_version", "created", "counts", "excluded")}
        return res


def restore(archive: Path, target: Path, force: bool = False) -> dict:
    archive = Path(archive).expanduser()
    target = Path(target).expanduser()
    staging = Path(tempfile.mkdtemp(prefix="lw-restore-", dir=target.parent if target.parent.exists() else None))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, members=list(_safe_members(tar)), filter="data")
        manifest = json.loads((staging / "manifest.json").read_text("utf-8"))
        if manifest.get("format", 0) > FORMAT:
            raise BackupError("备份来自更新版本的 LectureWeaver，请先升级")
        res = verify_dir(staging, manifest)
        if not res["ok"]:
            raise BackupError("备份校验失败，未做任何改动：\n- " + "\n- ".join(res["problems"]))
        (staging / "manifest.json").unlink()
        moved_aside = None
        if target.exists() and any(target.iterdir()):
            if not force:
                raise BackupError(f"目标目录 {target} 已有知识库；确认要替换请加 --force（原目录会改名保留）")
            moved_aside = target.with_name(target.name + f".before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
            target.rename(moved_aside)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(target))
        # A restored KB must open and migrate cleanly.
        kb = KB(target)
        after = verify_dir(target)
        kb.close()
        return {"target": str(target), "moved_aside": str(moved_aside) if moved_aside else None, "counts": after["counts"], "ok": after["ok"], "problems": after["problems"]}
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
