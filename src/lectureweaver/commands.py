"""Subcommand definitions. Each ``spec_*`` adds one command group."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .cli import UserError, emit

SPECS = []


def spec(fn):
    SPECS.append(fn)
    return fn


def _add(sub, name: str, fn, help: str, json_flag: bool = True) -> argparse.ArgumentParser:
    p = sub.add_parser(name, help=help, description=help)
    p.set_defaults(_fn=fn)
    if json_flag:
        p.add_argument("--json", action="store_true", help="输出 JSON")
    return p


# =====================================================================  auth
@spec
def spec_auth(sub):
    g = sub.add_parser("auth", help="统一身份认证登录与会话").add_subparsers(metavar="<子命令>")
    p = _add(g, "login", cmd_auth_login, "登录统一身份认证并建立云课堂会话（密码只存入系统凭据库）")
    p.add_argument("--username", "-u", help="学号")
    p.add_argument("--gui", action="store_true", help="用系统对话框输入密码/验证码（AI 代为启动时使用；macOS/Windows/Linux 桌面均可）")
    p.add_argument("--no-save-password", action="store_true", help="不保存密码（之后会话过期需重新登录）")
    _add(g, "status", cmd_auth_status, "查看会话状态（不显示任何凭据）")
    p = _add(g, "logout", cmd_auth_logout, "退出登录并清除会话 Cookie")
    p.add_argument("--forget-password", action="store_true", help="同时删除保存的密码")


def cmd_auth_login(args):
    from .config import MachineConfig
    from .prompt import default_prompter
    from .session import Session

    cfg = MachineConfig.load()
    prompter = default_prompter(gui=True if args.gui else None)
    username = args.username or cfg.username or prompter.ask("学号")
    if not username:
        raise UserError("需要学号")
    s = Session(cfg)
    try:
        token = s.interactive_login(username, None, prompter, save_password=not args.no_save_password)
        user = s.ylb().user_resource()
        name = (user or {}).get("userName", "")
        s.ylb_token.user = {"userName": name, "userNo": (user or {}).get("userNo", "")}
        s.save()
    finally:
        s.close()
    emit(args, {"ok": True, "user": name, "userId": token.user_id}, f"登录成功：{name}（云课堂 userId {token.user_id}）")


def cmd_auth_status(args):
    from .secrets import backend_name
    from .session import Session

    s = Session()
    st = s.status()
    ok = False
    detail = ""
    try:
        if s.ylb_token:
            u = s.ylb().user_resource()
            ok = bool(u)
            detail = (u or {}).get("userName", "")
    except Exception as e:  # noqa: BLE001 - status must never crash
        detail = str(e)
    finally:
        s.close()
    st["ylb_session_ok"] = ok
    st["detail"] = detail
    lines = [
        f"学号：{st['username'] or '未配置'}",
        f"已保存的密码：{'有' if st['password_in_keychain'] else '无'}（{backend_name()}）",
        f"统一认证 CASTGC：{'有' if st['castgc_cookie'] else '无'}；可信设备标记：{'有' if st['trusted_device_cookie'] else '无'}",
        f"云课堂会话：{'可用' if ok else '不可用'} {detail}",
    ]
    emit(args, st, "\n".join(lines))


def cmd_auth_logout(args):
    from .session import Session

    s = Session()
    s.logout(forget_password=args.forget_password)
    s.close()
    emit(args, {"ok": True}, "已退出登录")


# =====================================================================  dev
@spec
def spec_dev(sub):
    g = sub.add_parser("dev", help="开发/诊断工具").add_subparsers(metavar="<子命令>")
    p = _add(g, "probe", cmd_dev_probe, "抓取云课堂接口样本到本地目录（去除令牌）")
    p.add_argument("--out", default=".lw-scratch/probe")
    p.add_argument("--courses", type=int, default=2, help="最多探测几门课")


def _dump(out: Path, name: str, data) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


def cmd_dev_probe(args):
    from .session import Session

    out = Path(args.out)
    s = Session()
    y = s.ylb()
    summary = {}
    try:
        now = y.now_term()
        _dump(out, "now_term", now)
        rc = y.recording_courses(now["schoolYear"], now["term"])
        _dump(out, "recording_courses", rc)
        n = 0
        for c in rc or []:
            recs = y.all_recordings(c.get("classIds"), now["schoolYear"], now["term"])
            _dump(out, f"recs_{c.get('courseId')}", recs)
            summary[c.get("courseName") or c.get("courseId")] = len(recs)
            if recs and n < args.courses:
                n += 1
                r0 = recs[0]
                vid = r0.get("id") or r0.get("videoId") or r0.get("recordVideoInfoId")
                v = y.record_video(vid)
                _dump(out, f"video_{vid}", v)
                if v.get("taskId"):
                    _dump(out, f"speech_{vid}", y.speech(v["taskId"]))
                files = (v.get("vgaViewFiles") or []) + (v.get("teacherViewFiles") or [])
                if files:
                    _dump(out, f"ppt_{vid}", y.ppt_pages(files[0]["id"]))
                if r0.get("courseTableId"):
                    try:
                        _dump(out, f"months_{r0['courseTableId']}", y.course_recordings(r0["courseTableId"]))
                    except Exception as e:  # noqa: BLE001
                        _dump(out, f"months_{r0['courseTableId']}", {"error": str(e)})
                for kind in ("summarize", "abstracts", "mindmap"):
                    try:
                        _dump(out, f"ai_{kind}_{vid}", y.platform_ai(vid, kind))
                    except Exception as e:  # noqa: BLE001
                        _dump(out, f"ai_{kind}_{vid}", {"error": str(e)})
            time.sleep(0.3)
    finally:
        s.close()
    emit(args, summary)


# =====================================================================  setup / sync / browse
def _kb():
    from .kb import KB

    return KB.open()


def _add_filters(p):
    p.add_argument("--course", "-c", help="课程名/别名/课程号（模糊匹配）")
    p.add_argument("--week", "-w", help="教学周，如 7 或 5-7")
    p.add_argument("--date", "-d", help="日期，如 2026-10-19 或 10-19")
    p.add_argument("--since", help="起始日期（含）")
    p.add_argument("--until", help="结束日期（含）")
    p.add_argument("--lecture", "-l", action="append", help="课时 ID（可多次）")
    p.add_argument("--unprocessed", action="store_true", help="只选尚未整理完成的课时")
    p.add_argument("--status", help="按匹配状态筛选：matched/unscheduled/ambiguous/incomplete/manual")
    p.add_argument("--term", help="学期，如 2026-2027-1（默认最新）")
    p.add_argument("--limit", type=int)


def _filters(args):
    from .select import Filters

    return Filters(
        course=args.course, week=args.week, date=args.date, since=args.since, until=args.until,
        lecture=args.lecture, unprocessed=args.unprocessed, status=args.status, term=args.term, limit=args.limit,
    )


@spec
def spec_setup(sub):
    p = _add(sub, "init", cmd_init, "初始化/修改知识库位置与笔记输出目录")
    p.add_argument("--kb", help="知识库目录（默认 ~/LectureWeaver）")
    p.add_argument("--notes", help="笔记输出目录（Obsidian Vault 内的文件夹或任意 Markdown 目录）")
    p.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径（不在 PATH 里时）")
    p.add_argument("--whisper-bin", help="whisper.cpp 的 whisper-cli 路径（不在 PATH 里时）")
    p.add_argument("--whisper-model", help="whisper 模型文件（默认在 ~/.cache/whisper/ 下查找）")
    p = _add(sub, "sync", cmd_sync, "同步课表与课堂实录元数据（不下载视频）")
    p.add_argument("--term", help="学期，如 2025-2026-2（默认当前学期）")
    p.add_argument("--no-timetable", action="store_true", help="不访问教务课表")
    p = _add(sub, "courses", cmd_courses, "列出课程")
    p.add_argument("--term")
    p = _add(sub, "lectures", cmd_lectures, "列出课时（可筛选）及其处理状态")
    _add_filters(p)


def cmd_init(args):
    from .config import MachineConfig
    from .kb import KB

    cfg = MachineConfig.load()
    if args.kb:
        cfg.kb_dir = str(Path(args.kb).expanduser().resolve())
    if args.notes:
        cfg.notes_dir = str(Path(args.notes).expanduser().resolve())
        Path(cfg.notes_dir).mkdir(parents=True, exist_ok=True)
    if args.ffmpeg:
        ff = Path(args.ffmpeg).expanduser().resolve()
        cfg.ffmpeg_bin = str(ff)
        probe = ff.with_name(ff.name.replace("ffmpeg", "ffprobe"))
        if probe.exists():
            cfg.ffprobe_bin = str(probe)
    if args.whisper_bin:
        cfg.whisper_bin = str(Path(args.whisper_bin).expanduser().resolve())
    if args.whisper_model:
        cfg.whisper_model = str(Path(args.whisper_model).expanduser().resolve())
    cfg.save()
    kb = KB(cfg.kb_path)
    kb.close()
    data = {"kb_dir": cfg.kb_dir, "notes_dir": cfg.notes_dir or None, "state_dir": str(cfg.path().parent)}
    emit(args, data, f"知识库：{cfg.kb_dir}\n笔记目录：{cfg.notes_dir or '（未设置，用 lw init --notes <目录> 设置）'}\n本机状态与会话：{cfg.path().parent}")


def cmd_sync(args):
    from .session import Session
    from .sync import sync

    kb = _kb()
    s = Session()
    try:
        rep = sync(kb, s, term=args.term, with_timetable=not args.no_timetable)
    finally:
        s.close()
        kb.close()
    lines = [
        f"学期 {rep.term}：课程 {rep.courses} 门，课表时段 {rep.timetable_rows} 条",
        f"课堂录像 {rep.recordings} 条（新增 {rep.new_recordings}），课时 {rep.lectures} 个（新增 {rep.new_lectures}）",
    ]
    if rep.flagged:
        lines.append(f"需要留意的课时 {len(rep.flagged)} 个（用 `lw lectures --status ambiguous` 等查看原因）")
    for w in rep.warnings:
        lines.append(f"提示：{w}")
    emit(args, rep.__dict__, "\n".join(lines))


def cmd_courses(args):
    from .kb import loads
    from .select import current_term

    kb = _kb()
    term = args.term or current_term(kb)
    rows = kb.q(
        """SELECT c.*, (SELECT COUNT(*) FROM lectures l WHERE l.course_id=c.id) AS n_lect,
           (SELECT COUNT(*) FROM materials m JOIN material_links ml ON ml.material_id=m.id WHERE ml.target_type='course' AND ml.target_id=c.id) AS n_mat
           FROM courses c WHERE term=? ORDER BY n_lect DESC, name""",
        term,
    )
    data = [
        {"id": r["id"], "name": r["name"], "code": r["code"], "teacher": r["teacher"], "lectures": r["n_lect"],
         "materials": r["n_mat"], "subject": loads(r["profile"], {}).get("subject"), "aliases": loads(r["aliases"], [])}
        for r in rows
    ]
    text = "\n".join(
        f"{d['name']:<16} 课号 {d['code'] or '-':<8} {d['teacher'] or '':<6} 课时 {d['lectures']:>3}  资料 {d['materials']}  [{d['subject']}]"
        + (f"  别名：{'、'.join(d['aliases'])}" if d["aliases"] else "")
        for d in data
    )
    kb.close()
    emit(args, data, text or "还没有课程，先运行 lw sync")


def lecture_state(kb, lid: str) -> dict:
    return {r["stage"]: r["status"] for r in kb.q("SELECT stage,status FROM stages WHERE lecture_id=?", lid)}


def cmd_lectures(args):
    from .select import SelectError, select_lectures
    from .sync import lecture_label

    kb = _kb()
    try:
        rows = select_lectures(kb, _filters(args))
    except SelectError as e:
        raise UserError(str(e))
    data = []
    lines = []
    for r in rows:
        st = lecture_state(kb, r["id"])
        note = kb.q1("SELECT id,current_version FROM notes WHERE lecture_id=? AND kind='lecture'", r["id"])
        d = {
            "id": r["id"], "course": r["course_name"], "label": lecture_label(r), "match": r["match_status"],
            "match_note": r["match_note"], "stages": st, "note_version": note["current_version"] if note else None,
        }
        data.append(d)
        flag = "" if r["match_status"] == "matched" else f" ⚠{r['match_status']}"
        prog = " ".join(f"{k}:{v}" for k, v in st.items()) or "未处理"
        lines.append(f"{r['id']:<34} {r['course_name']:<12} {lecture_label(r)}{flag} | {prog}" + (f" | 笔记 v{note['current_version']}" if note else ""))
        if r["match_status"] != "matched" and r["match_note"]:
            lines.append(f"    └ {r['match_note']}")
    kb.close()
    emit(args, data, "\n".join(lines) if lines else "没有符合条件的课时")


# =====================================================================  processing
@spec
def spec_process(sub):
    p = _add(sub, "process", cmd_process, "整理选定课时：转写、截图、OCR、学习包（可中断续跑，已完成的不重复）")
    _add_filters(p)
    p.add_argument("--force", action="append", default=[], help="强制重做某阶段（details/transcript/visual/packet/all）")
    p.add_argument("--stages", help="只跑这些阶段，逗号分隔")
    p.add_argument("--asr", default="auto", help="无平台转写时：auto/local（本地 whisper）、none（不识别）、或已授权的云服务名")
    p.add_argument("--dry-run", action="store_true", help="只列出将处理的课时")
    g = sub.add_parser("lecture", help="单个课时：详情、学习包、手动关联").add_subparsers(metavar="<子命令>")
    p = _add(g, "show", cmd_lecture_show, "查看课时详情、处理进度和素材")
    p.add_argument("lecture")
    p = _add(g, "packet", cmd_lecture_packet, "输出学习包路径（供 AI 整理笔记/讲解；首次使用需授权）")
    p.add_argument("lecture")
    p.add_argument("--print", action="store_true", help="直接打印学习包内容")
    p = _add(g, "assign", cmd_lecture_assign, "把一条录像手动归入某课时（处理匹配歧义）")
    p.add_argument("recording", help="录像 ID（lw lecture show 可见）")
    p.add_argument("lecture")
    p = _add(g, "frame", lambda a: cmd_frame(a), "查看某张截图的完整 OCR 与图片路径")
    p.add_argument("lecture")
    p.add_argument("frame_id")
    p = _add(g, "confirm", cmd_lecture_confirm, "确认课时与课表的对应关系（标记为 manual，之后同步不再改动）")
    p.add_argument("lecture")
    p.add_argument("--note", default="用户已确认")
    p.add_argument("--title", help="为课时设置主题标题")
    p = _add(sub, "status", cmd_status, "总体进度：已完成/不完整/需要补充的课时")
    p.add_argument("--course", "-c")
    p.add_argument("--term")
    _add(sub, "clean", cmd_clean, "清理残留的临时媒体文件")


def cmd_process(args):
    from .pipeline import STAGES, Pipeline
    from .select import SelectError, select_lectures
    from .sync import lecture_label

    kb = _kb()
    try:
        rows = select_lectures(kb, _filters(args))
    except SelectError as e:
        raise UserError(str(e))
    if not rows:
        emit(args, {"processed": []}, "没有符合条件的课时（先运行 lw sync，或放宽筛选条件）")
        return 0
    if args.dry_run:
        emit(args, [r["id"] for r in rows], "\n".join(f"{r['id']}  {r['course_name']} {lecture_label(r)}" for r in rows))
        return 0
    stages = args.stages.split(",") if args.stages else STAGES
    from .kb import try_lock

    lock = try_lock(kb.root / ".process.lock")  # held until this process exits
    if lock is None:
        raise UserError("另一个 lw process 正在处理这个知识库，请等它结束（或确认它已退出）后再运行")
    pipe = Pipeline(kb, log=lambda m: print(m, flush=True), force=set(args.force), asr=args.asr)
    results = {}
    total_in = 0
    try:
        for i, r in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] {r['course_name']} {lecture_label(r)}（{r['id']}）", flush=True)
            res = pipe.run(r, stages)
            results[r["id"]] = res
            for st, msg in res.items():
                print(f"  {st}: {msg}", flush=True)
            total_in += sum(x["bytes_in"] or 0 for x in kb.q("SELECT bytes_in FROM stages WHERE lecture_id=?", r["id"]))
    finally:
        if pipe.session:
            pipe.session.close()
    from .media import human_bytes

    emit(args, results, f"完成 {len(rows)} 个课时；这些课时各处理阶段（按最近一次运行计）共从学校服务器传输 {human_bytes(total_in)}，未保存任何视频。")


def cmd_lecture_show(args):
    from .kb import loads
    from .sync import lecture_label

    kb = _kb()
    lec = kb.q1("SELECT l.*, c.name AS course_name FROM lectures l JOIN courses c ON c.id=l.course_id WHERE l.id=?", args.lecture)
    if not lec:
        raise UserError(f"找不到课时 {args.lecture}")
    recs = kb.q("SELECT id,name,start_time,end_time,segment,role,task_id FROM recordings WHERE lecture_id=? ORDER BY start_time", lec["id"])
    stages = {r["stage"]: {"status": r["status"], "detail": loads(r["detail"], {}), "bytes_in": r["bytes_in"], "error": r["error"]} for r in kb.q("SELECT * FROM stages WHERE lecture_id=?", lec["id"])}
    notes = [dict(r) for r in kb.q("SELECT id,kind,title,current_version,vault_path FROM notes WHERE lecture_id=?", lec["id"])]
    mats = [dict(r) for r in kb.q("SELECT m.id,m.title,ml.role FROM materials m JOIN material_links ml ON ml.material_id=m.id WHERE ml.target_type='lecture' AND ml.target_id=?", lec["id"])]
    data = {"id": lec["id"], "course": lec["course_name"], "label": lecture_label(lec), "teacher": lec["teacher"], "room": lec["room"],
            "match": lec["match_status"], "match_note": lec["match_note"], "dir": str(kb.abs(lec["dir"])), "recordings": [dict(r) for r in recs],
            "stages": stages, "notes": notes, "materials": mats}
    from .media import human_bytes

    lines = [f"{lec['course_name']} {lecture_label(lec)}  [{lec['id']}]", f"教师 {lec['teacher']}  教室 {lec['room']}  匹配：{lec['match_status']} {lec['match_note'] or ''}",
             f"目录：{kb.abs(lec['dir'])}", "录像："]
    lines += [f"  {r['id']}  {r['name']}  {r['role']}" for r in recs]
    lines.append("进度：")
    for k, v in stages.items():
        d = v["detail"]
        extra = ""
        if k == "transcript":
            extra = "；".join(f"第{s.get('part')}段 {s.get('kind')} {s.get('model', '')}".strip() for s in d.get("sources", []))
        if k == "visual":
            extra = f"截图 {d.get('frames')} 张，占用 {human_bytes(d.get('stored_bytes') or 0)}"
        lines.append(f"  {k}: {v['status']}  传输 {human_bytes(v['bytes_in'] or 0)}  {extra} {v['error'] or ''}")
    for n in notes:
        lines.append(f"笔记：{n['title']} v{n['current_version']} → {n['vault_path']}")
    for m in mats:
        lines.append(f"关联资料：{m['title']}（{m['role']}）")
    kb.close()
    emit(args, data, "\n".join(lines))


def cmd_lecture_packet(args):
    from .consent import ConsentRequired, check

    kb = _kb()
    lec = kb.q1("SELECT * FROM lectures WHERE id=?", args.lecture)
    if not lec:
        raise UserError(f"找不到课时 {args.lecture}")
    st = kb.stage(lec["id"], "packet")
    if not st or st["status"] != "done":
        raise UserError(f"课时还没整理完，先运行 lw process -l {lec['id']}")
    try:
        check(kb, "harness", ["transcripts", "screenshots", "materials"])
    except ConsentRequired as e:
        raise UserError(str(e))
    path = kb.abs(lec["dir"]) / "packet.md"
    # refresh: neighbours/materials/notes may have changed since processing
    from .packet import build_packet

    build_packet(kb, lec)
    kb.close()
    if args.print:
        print(path.read_text("utf-8"))
        return 0
    emit(args, {"packet": str(path)}, str(path))


def cmd_lecture_assign(args):
    kb = _kb()
    rec = kb.q1("SELECT * FROM recordings WHERE id=?", args.recording)
    lec = kb.q1("SELECT * FROM lectures WHERE id=?", args.lecture)
    if not rec or not lec:
        raise UserError("录像或课时不存在")
    old = rec["lecture_id"]
    kb.x("UPDATE recordings SET lecture_id=?, role='primary' WHERE id=?", lec["id"], rec["id"])
    kb.x("UPDATE lectures SET match_status='manual', match_note=COALESCE(match_note,'')||?, updated_at=? WHERE id=?",
         f"；手动并入录像 {rec['id'][-6:]}", time.time(), lec["id"])
    kb.x("DELETE FROM stages WHERE lecture_id=? AND stage IN ('details','transcript','visual','packet')", lec["id"])
    if old and not kb.q1("SELECT 1 FROM recordings WHERE lecture_id=?", old):
        kb.x("UPDATE lectures SET match_status='manual', match_note='录像已手动并入 ' || ? WHERE id=?", lec["id"], old)
    kb.db.commit()
    kb.close()
    emit(args, {"ok": True}, f"已把录像并入 {lec['id']}；该课时需要重新处理（lw process -l {lec['id']}）")


def cmd_lecture_confirm(args):
    kb = _kb()
    if not kb.q1("SELECT 1 FROM lectures WHERE id=?", args.lecture):
        raise UserError(f"找不到课时 {args.lecture}")
    kb.x("UPDATE lectures SET match_status='manual', match_note=?, title=COALESCE(?,title), updated_at=? WHERE id=?", args.note, args.title, time.time(), args.lecture)
    kb.db.commit()
    kb.close()
    emit(args, {"ok": True}, "已确认")


def cmd_status(args):
    from .select import current_term, find_courses

    kb = _kb()
    term = args.term or current_term(kb)
    courses = find_courses(kb, args.course, term) if args.course else kb.q("SELECT * FROM courses WHERE term=? ORDER BY name", term)
    out = []
    lines = []
    for c in courses:
        lects = kb.q("SELECT id, match_status FROM lectures WHERE course_id=? ORDER BY date", c["id"])
        if not lects:
            continue
        done = partial = failed = none = noted = 0
        attention = []
        for l in lects:
            st = {r["stage"]: r["status"] for r in kb.q("SELECT stage,status FROM stages WHERE lecture_id=?", l["id"])}
            has_note = kb.q1("SELECT 1 FROM notes WHERE lecture_id=? AND kind='lecture'", l["id"])
            noted += bool(has_note)
            if not st:
                none += 1
            elif "failed" in st.values():
                failed += 1
                attention.append(f"{l['id']} 处理失败")
            elif "partial" in st.values():
                partial += 1
                attention.append(f"{l['id']} 素材不完整")
            elif all(st.get(s) in ("done", "skipped") for s in ("transcript", "visual", "packet")):
                done += 1
                if not has_note:
                    attention.append(f"{l['id']} 已整理素材，尚未生成笔记")
            else:
                none += 1
            if l["match_status"] in ("ambiguous", "unscheduled", "incomplete"):
                attention.append(f"{l['id']} 课表匹配：{l['match_status']}")
        out.append({"course": c["name"], "lectures": len(lects), "processed": done, "partial": partial, "failed": failed, "pending": none, "notes": noted, "attention": attention})
        lines.append(f"{c['name']}: 课时 {len(lects)}，素材完成 {done}，不完整 {partial}，失败 {failed}，未处理 {none}，已有笔记 {noted}")
        lines += [f"   · {a}" for a in attention[:8]] + ([f"   · …另有 {len(attention) - 8} 条"] if len(attention) > 8 else [])
    kb.close()
    emit(args, out, "\n".join(lines) or "还没有数据，先运行 lw sync")


def cmd_clean(args):
    from .media import human_bytes
    from .pipeline import cleanup_tmp

    n = cleanup_tmp()
    emit(args, {"freed": n}, f"已清理临时文件 {human_bytes(n)}")


# =====================================================================  notes
@spec
def spec_notes(sub):
    g = sub.add_parser("note", help="笔记：保存/导出/版本/冲突处理").add_subparsers(metavar="<子命令>")
    p = _add(g, "save", cmd_note_save, "校验并保存 AI 写好的笔记草稿，自动写入笔记目录（不覆盖你的手动修改）")
    p.add_argument("--file", "-f", required=True, help="草稿 Markdown 文件（含 {t}/{f}/{m} 标记）")
    p.add_argument("--lecture", "-l", help="课时 ID（课时笔记）")
    p.add_argument("--kind", default="lecture", choices=["lecture", "review", "cards", "topic", "explain"])
    p.add_argument("--course", "-c", help="非课时笔记所属课程")
    p.add_argument("--key", help="非课时笔记的标识（如 第7周复习）")
    p.add_argument("--title", help="标题（默认取草稿第一个一级标题）")
    p.add_argument("--model", help="生成笔记的模型/工具，例如 claude-opus-5-5@claude-code")
    p.add_argument("--style", help="笔记风格说明，例如 复习版/详细版")
    p.add_argument("--merge", action="store_true", help="你改过笔记时尝试三方合并")
    p.add_argument("--replace", action="store_true", help="明确覆盖你的修改（旧内容保留在历史中）")
    p.add_argument("--allow-drop", action="store_true", help="明确允许新版本不包含你补充的内容")
    p.add_argument("--check", action="store_true", help="只校验不保存")
    p = _add(g, "list", cmd_note_list, "列出笔记")
    p.add_argument("--course", "-c")
    p = _add(g, "show", cmd_note_show, "显示笔记当前内容（含你在笔记软件中的修改）")
    p.add_argument("note", help="笔记 ID 或课时 ID")
    p = _add(g, "history", cmd_note_history, "版本历史")
    p.add_argument("note")
    p = _add(g, "diff", cmd_note_diff, "比较两个版本（默认最新两版）")
    p.add_argument("note")
    p.add_argument("--a", type=int)
    p.add_argument("--b", type=int)
    p = _add(g, "rollback", cmd_note_rollback, "回滚到某个版本（生成新版本，不删除历史）")
    p.add_argument("note")
    p.add_argument("--to", type=int, required=True)
    p = _add(g, "accept", cmd_note_accept, "采用最新 AI 版本替换你修改过的笔记（你的版本保留在历史中）")
    p.add_argument("note")
    _add(g, "sync", cmd_note_sync, "扫描笔记目录，记录你在笔记软件中做的修改")
    _add(g, "export", cmd_note_export, "把所有笔记重新写到当前笔记目录（换电脑/换目录后使用）")


def _note_by(kb, ident: str):
    r = kb.q1("SELECT * FROM notes WHERE id=?", ident) or kb.q1("SELECT * FROM notes WHERE lecture_id=? AND kind='lecture'", ident)
    if not r:
        raise UserError(f"找不到笔记 {ident}")
    return dict(r)


def cmd_note_save(args):
    from . import notes as N

    kb = _kb()
    draft = Path(args.file).read_text("utf-8")
    try:
        note = N.get_or_create(kb, args.kind, args.lecture, _course_id(kb, args.course) if args.course else None, args.key)
        if args.check:
            rr = N.render(kb, note, draft, N.notes_root() / (note.get("vault_path") or "x.md"), N.notes_root())
            emit(args, {"errors": rr.errors, "warnings": rr.warnings, "stats": rr.stats},
                 "\n".join(["校验通过" if not rr.errors else "有错误："] + [f"- {e}" for e in rr.errors] + [f"- 提醒：{w}" for w in rr.warnings]) + f"\n统计：{rr.stats}")
            return 1 if rr.errors else 0
        mode = "replace" if args.replace else ("merge" if args.merge else "safe")
        out = N.save(kb, note, draft, {"model": args.model, "style": args.style}, mode=mode, title=args.title, allow_drop=args.allow_drop)
    except N.NoteError as e:
        raise UserError(str(e))
    finally:
        kb.close()
    lines = [f"{out.message}（{out.note_id} v{out.version}）"]
    if out.vault_path:
        lines.append(f"笔记：{N.notes_root() / out.vault_path}")
    if out.proposal_path:
        lines.append(f"新版本提案：{N.notes_root() / out.proposal_path}")
    if out.missing_user_lines:
        lines.append("新版本里找不到你补充的这些内容：")
        lines += [f"  - {x[:80]}" for x in out.missing_user_lines[:10]]
    lines += [f"提醒：{w}" for w in out.warnings]
    emit(args, out.__dict__, "\n".join(lines))


def _course_id(kb, q):
    from .select import find_courses

    cs = find_courses(kb, q)
    if len(cs) != 1:
        raise UserError(f"课程“{q}”匹配到 {len(cs)} 门，请写得更具体")
    return cs[0]["id"]


def cmd_note_list(args):
    kb = _kb()
    where, a = "", []
    if args.course:
        where, a = "WHERE n.course_id=?", [_course_id(kb, args.course)]
    rows = [dict(r) for r in kb.q(f"SELECT n.id,n.kind,n.title,n.current_version,n.exported_version,n.vault_path,c.name AS course FROM notes n LEFT JOIN courses c ON c.id=n.course_id {where} ORDER BY n.vault_path", *a)]
    kb.close()
    emit(args, rows, "\n".join(f"{r['id']:<40} v{r['current_version']} {r['course'] or ''} {r['vault_path'] or '(未导出)'}" for r in rows) or "还没有笔记")


def cmd_note_show(args):
    from . import notes as N

    kb = _kb()
    n = _note_by(kb, args.note)
    N.sync_in(kb, n)
    n = _note_by(kb, args.note)
    text = N.version_text(kb, n["id"], n["current_version"])
    kb.close()
    emit(args, {"id": n["id"], "version": n["current_version"], "text": text}, text)


def cmd_note_history(args):
    from datetime import datetime

    from . import notes as N

    kb = _kb()
    n = _note_by(kb, args.note)
    N.sync_in(kb, n)
    h = N.history(kb, n["id"])
    exp = _note_by(kb, args.note)["exported_version"]
    kb.close()
    names = {"ai": "AI", "user": "你的修改", "merge": "合并", "rollback": "回滚"}
    emit(args, h, "\n".join(
        f"v{x['version']:<3} {datetime.fromtimestamp(x['created_at']):%Y-%m-%d %H:%M}  {names.get(x['author'], x['author'])}  {x['model'] or ''} {x['style'] or ''} {x['message'] or ''}{'  ← 笔记目录中的版本' if x['version'] == exp else ''}"
        for x in h))


def cmd_note_diff(args):
    from . import notes as N

    kb = _kb()
    d = N.diff(kb, _note_by(kb, args.note), args.a, args.b)
    kb.close()
    emit(args, {"diff": d}, d or "没有差异")


def cmd_note_rollback(args):
    from . import notes as N

    kb = _kb()
    out = N.rollback(kb, _note_by(kb, args.note), args.to)
    kb.close()
    emit(args, out.__dict__, out.message)


def cmd_note_accept(args):
    from . import notes as N

    kb = _kb()
    out = N.accept_proposal(kb, _note_by(kb, args.note))
    kb.close()
    emit(args, out.__dict__, out.message)


def cmd_note_sync(args):
    from . import notes as N

    kb = _kb()
    res = {}
    for n in kb.q("SELECT * FROM notes WHERE vault_path IS NOT NULL"):
        res[n["id"]] = N.sync_in(kb, dict(n))
    kb.close()
    edited = [k for k, v in res.items() if v == "edited"]
    missing = [k for k, v in res.items() if v == "missing"]
    emit(args, res, f"检查 {len(res)} 篇；你修改过的 {len(edited)} 篇已记入版本历史；在笔记目录中找不到的 {len(missing)} 篇" + ("".join(f"\n  - {m}" for m in missing)))


def cmd_note_export(args):
    from . import notes as N

    kb = _kb()
    try:
        r = N.reexport_all(kb)
    except N.NoteError as e:
        raise UserError(str(e))
    kb.close()
    emit(args, r, f"写出 {r['written']} 篇；{r['kept_user_files']} 篇因笔记目录里已有不同内容而保留原文件（新版本放在 .lw-proposed.md）")


# =====================================================================  materials
@spec
def spec_materials(sub):
    g = sub.add_parser("material", help="课程资料：导入、关联、阅读").add_subparsers(metavar="<子命令>")
    p = _add(g, "add", cmd_material_add, "导入资料（PDF/PPTX/DOCX/Markdown/代码…）并关联课程")
    p.add_argument("files", nargs="+")
    p.add_argument("--course", "-c", help="关联到课程")
    p.add_argument("--lecture", "-l", action="append", help="关联到课时（可多次）")
    p.add_argument("--role", help="textbook/lab_guide/slides/code/reference/homework/exam/notes")
    p.add_argument("--title")
    p.add_argument("--note")
    p = _add(g, "link", cmd_material_link, "把资料关联到课程/课时")
    p.add_argument("material")
    p.add_argument("--course", "-c")
    p.add_argument("--lecture", "-l", action="append")
    p.add_argument("--role")
    p.add_argument("--note")
    p.add_argument("--unlink", action="store_true")
    p = _add(g, "suggest", cmd_material_suggest, "根据内容推荐可能对应的课时（需确认后再 link）")
    p.add_argument("material")
    p.add_argument("--course", "-c", required=True)
    p = _add(g, "list", cmd_material_list, "列出资料")
    p.add_argument("--course", "-c")
    p = _add(g, "show", cmd_material_show, "显示资料提取的文字")
    p.add_argument("material")
    p.add_argument("--units", help="页/幻灯片范围，如 1-3,7")


def cmd_material_add(args):
    from . import materials as M

    kb = _kb()
    cid = _course_id(kb, args.course) if args.course else None
    out = []
    try:
        for f in args.files:
            m, new, warn = M.import_file(kb, Path(f), title=args.title if len(args.files) == 1 else None, note=args.note)
            if cid:
                M.link(kb, m["id"], "course", cid, args.role, args.note)
            for lid in args.lecture or []:
                M.link(kb, m["id"], "lecture", lid, args.role, args.note)
            out.append({"id": m["id"], "title": m["title"], "new": new, "units": m["units"], "warning": warn})
    except M.MaterialError as e:
        raise UserError(str(e))
    finally:
        kb.close()
    emit(args, out, "\n".join(f"{'导入' if o['new'] else '已存在'} {o['id']} 《{o['title']}》 {o['units']} 页/段" + (f"（{o['warning']}）" if o["warning"] else "") for o in out))


def cmd_material_link(args):
    from . import materials as M

    kb = _kb()
    try:
        if args.course:
            cid = _course_id(kb, args.course)
            (M.unlink(kb, args.material, "course", cid) if args.unlink else M.link(kb, args.material, "course", cid, args.role, args.note))
        for lid in args.lecture or []:
            (M.unlink(kb, args.material, "lecture", lid) if args.unlink else M.link(kb, args.material, "lecture", lid, args.role, args.note))
    except M.MaterialError as e:
        raise UserError(str(e))
    finally:
        kb.close()
    emit(args, {"ok": True}, "已更新关联")


def cmd_material_suggest(args):
    from . import materials as M

    kb = _kb()
    res = M.suggest_lectures(kb, args.material, _course_id(kb, args.course))
    kb.close()
    emit(args, res, "\n".join(f"{r['lecture']}  第{r['week']}周  相似度 {r['score']}  共同词：{'、'.join(r['shared_terms'])}" for r in res) or "没有已处理的课时可比较（先 lw process）")


def cmd_material_list(args):
    kb = _kb()
    where, a = "", []
    if args.course:
        where, a = "WHERE m.id IN (SELECT material_id FROM material_links WHERE target_type='course' AND target_id=?)", [_course_id(kb, args.course)]
    rows = []
    for m in kb.q(f"SELECT * FROM materials m {where} ORDER BY imported_at DESC", *a):
        links = [f"{l['target_type']}:{l['target_id']}({l['role'] or '-'})" for l in kb.q("SELECT * FROM material_links WHERE material_id=?", m["id"])]
        rows.append({"id": m["id"], "title": m["title"], "file": m["filename"], "kind": m["kind"], "units": m["units"], "links": links})
    kb.close()
    emit(args, rows, "\n".join(f"{r['id']}  《{r['title']}》 {r['kind']} {r['units']}页/段  {' '.join(r['links'])}" for r in rows) or "还没有资料")


def cmd_material_show(args):
    from . import materials as M

    kb = _kb()
    try:
        text = M.material_text(kb, args.material, args.units)
    except M.MaterialError as e:
        raise UserError(str(e))
    kb.close()
    emit(args, {"text": text}, text)


# =====================================================================  search
@spec
def spec_search(sub):
    p = _add(sub, "search", cmd_search, "检索概念/例题/老师的讲解出自哪节课（转写、截图文字、资料、笔记）")
    p.add_argument("query")
    p.add_argument("--course", "-c")
    p.add_argument("--kind", action="append", choices=["transcript", "frame", "material", "note"])
    p.add_argument("--limit", type=int, default=30)
    p = _add(sub, "frame", cmd_frame, "查看某张截图的完整 OCR 与图片路径")
    p.add_argument("lecture")
    p.add_argument("frame_id")


def cmd_search(args):
    from .search import search

    kb = _kb()
    res = search(kb, args.query, _course_id(kb, args.course) if args.course else None, args.kind, args.limit)
    kb.close()
    lines = []
    for r in res:
        where = f"{r.get('course_name', '')} {r.get('date', '')} 第{r.get('week')}周 {r['lecture']}" if r["lecture"] else f"《{r.get('material_title', r['ref'])}》{r.get('unit') or ''}"
        lines.append(f"[{r['kind_name']}] {where} {r['clock'] or ''}\n    {r['snippet']}")
    emit(args, res, "\n".join(lines) or "没有找到")


def cmd_frame(args):
    import json as _j

    kb = _kb()
    lec = kb.q1("SELECT * FROM lectures WHERE id=?", args.lecture)
    if not lec:
        raise UserError("课时不存在")
    fj = _j.loads((kb.abs(lec["dir"]) / "frames.json").read_text("utf-8"))
    f = next((x for x in fj["frames"] if x["id"] == args.frame_id), None)
    if not f:
        raise UserError("截图不存在")
    path = kb.abs(lec["dir"]) / f["file"]
    kb.close()
    emit(args, {**f, "path": str(path)}, f"{f['id']} {f['clock']} {f['stream']}\n图片：{path}\n\n{f.get('ocr', '')}")


# =====================================================================  learning
@spec
def spec_learn(sub):
    g = sub.add_parser("learn", help="学习状态：知识点、作答证据、薄弱点").add_subparsers(metavar="<子命令>")
    p = _add(g, "concept", cmd_learn_concept, "登记/更新知识点，并可关联到课时")
    p.add_argument("name")
    p.add_argument("--course", "-c")
    p.add_argument("--summary")
    p.add_argument("--alias", action="append")
    p.add_argument("--lecture", "-l", help="出处课时")
    p.add_argument("--at", help="出处时间 HH:MM:SS")
    p = _add(g, "log", cmd_learn_log, "记录一条学习证据（讲解/作答/自述/复习）")
    p.add_argument("name", help="知识点名称")
    p.add_argument("--course", "-c")
    p.add_argument("--kind", required=True, choices=["explained", "asked", "answered", "self_report", "review", "correction", "exposure"])
    p.add_argument("--outcome", choices=["correct", "partial", "wrong", "unsure", "n/a"])
    p.add_argument("--evidence", help="学习者实际的回答/表述（简要原话）")
    p.add_argument("--question", help="所提的问题")
    p.add_argument("--type", help="题型：recall/apply/transfer/review")
    p.add_argument("--misconception", help="暴露出的具体误解")
    p.add_argument("--session")
    p.add_argument("--harness", help="当前 AI 工具名，如 claude-code / codex")
    p = _add(g, "status", cmd_learn_status, "知识点掌握情况（带依据）")
    p.add_argument("--course", "-c")
    p.add_argument("name", nargs="?")
    p = _add(g, "next", cmd_learn_next, "建议接下来学/复查什么")
    p.add_argument("--course", "-c")
    p = _add(g, "set", cmd_learn_set, "手动修正掌握状态（需说明理由；之后的新证据会重新判断）")
    p.add_argument("name")
    p.add_argument("state", choices=["unseen", "introduced", "shaky", "working", "solid"])
    p.add_argument("--course", "-c")
    p.add_argument("--reason", required=True)
    p = _add(g, "resolve", cmd_learn_resolve, "标记某条误解已纠正")
    p.add_argument("misconception_id", type=int)
    p.add_argument("--evidence", required=True)
    p = _add(g, "session", cmd_learn_session, "开始/结束一次学习会话（记录在哪个 AI 工具中学习）")
    p.add_argument("action", choices=["start", "end"])
    p.add_argument("--course", "-c")
    p.add_argument("--focus")
    p.add_argument("--harness")
    p.add_argument("--summary")


def _concept(kb, name, course):
    from .learning import ensure_concept, find_concept

    cid = _course_id(kb, course) if course else None
    c = find_concept(kb, name, cid)
    return c or ensure_concept(kb, name, cid)


def cmd_learn_concept(args):
    from .learning import ensure_concept, link_lecture

    kb = _kb()
    cid = _course_id(kb, args.course) if args.course else None
    if args.lecture and not cid:
        l = kb.q1("SELECT course_id FROM lectures WHERE id=?", args.lecture)
        cid = l["course_id"] if l else None
    c = ensure_concept(kb, args.name, cid, args.summary, args.alias)
    if args.lecture:
        link_lecture(kb, c["id"], args.lecture, args.at)
    kb.close()
    emit(args, dict(c), f"知识点 {c['name']}（{c['id']}）")


def cmd_learn_log(args):
    from .learning import LearnError, log_event

    kb = _kb()
    c = _concept(kb, args.name, args.course)
    session = args.session or kb.get_setting("active_session", {}).get("id")
    try:
        d = log_event(kb, c["id"], args.kind, args.outcome, args.evidence, {"question": args.question, "type": args.type},
                      session, args.harness or kb.get_setting("active_session", {}).get("harness"), args.misconception)
    except LearnError as e:
        raise UserError(str(e))
    kb.close()
    emit(args, d, f"{c['name']}：{d['state_name']}（{d['rationale']}）")


def cmd_learn_status(args):
    from .learning import history, overview

    kb = _kb()
    cid = _course_id(kb, args.course) if args.course else None
    if args.name:
        c = _concept(kb, args.name, args.course)
        items = [x for x in overview(kb, c["course_id"]) if x["id"] == c["id"]]
        mis = [dict(r) for r in kb.q("SELECT id,text,count,resolved_at FROM misconceptions WHERE concept_id=?", c["id"])]
        h = history(kb, c["id"])
        kb.close()
        it = items[0]
        lines = [f"{it['name']}：{it['state_name']} —— {it['rationale']}", f"出处课时：{', '.join(it['lectures']) or '未关联'}", "误解："]
        lines += [f"  #{m['id']} {m['text']}（{m['count']} 次）{'已纠正' if m['resolved_at'] else '未解决'}" for m in mis] or ["  无"]
        lines.append("最近记录：")
        lines += [f"  {e['time']} {e['kind']} {e['outcome'] or ''} {e['evidence'] or ''}" for e in h[:10]]
        emit(args, {"concept": it, "misconceptions": mis, "history": h}, "\n".join(lines))
        return
    items = overview(kb, cid)
    kb.close()
    order = {"shaky": 0, "introduced": 1, "working": 2, "solid": 3, "unseen": 4}
    items.sort(key=lambda x: (order[x["state"]], x["name"]))
    emit(args, items, "\n".join(f"{x['state_name']:<10} {x['name']:<16} {x['rationale']}" for x in items) or "还没有知识点记录")


def cmd_learn_next(args):
    from .learning import next_steps

    kb = _kb()
    res = next_steps(kb, _course_id(kb, args.course) if args.course else None)
    kb.close()
    emit(args, res, "\n".join(f"{i}. {x['name']}：{x['reason']}" for i, x in enumerate(res, 1)) or "暂无待复查的知识点")


def cmd_learn_set(args):
    from .learning import derive, set_manual

    kb = _kb()
    c = _concept(kb, args.name, args.course)
    set_manual(kb, c["id"], args.state, args.reason)
    d = derive(kb, c["id"])
    kb.close()
    emit(args, d, f"{c['name']}：{d['state_name']}（{d['rationale']}）")


def cmd_learn_resolve(args):
    from .learning import LearnError, resolve_misconception

    kb = _kb()
    try:
        resolve_misconception(kb, args.misconception_id, args.evidence)
    except LearnError as e:
        raise UserError(str(e))
    kb.close()
    emit(args, {"ok": True}, "已标记纠正")


def cmd_learn_session(args):
    import uuid

    kb = _kb()
    if args.action == "start":
        s = {"id": uuid.uuid4().hex[:12], "started": time.time(), "course": args.course, "focus": args.focus, "harness": args.harness}
        kb.set_setting("active_session", s)
        msg = f"学习会话 {s['id']} 开始"
    else:
        s = kb.get_setting("active_session", {}) or {}
        hist = kb.get_setting("session_log", []) or []
        if s:
            s.update({"ended": time.time(), "summary": args.summary})
            hist.append(s)
            kb.set_setting("session_log", hist[-200:])
        kb.set_setting("active_session", {})
        msg = "学习会话已结束" + (f"：{args.summary}" if args.summary else "")
    kb.close()
    emit(args, s, msg)


# =====================================================================  consent / providers
@spec
def spec_consent(sub):
    g = sub.add_parser("consent", help="云端资料发送授权").add_subparsers(metavar="<子命令>")
    _add(g, "status", cmd_consent_status, "查看已授权的服务与范围")
    p = _add(g, "grant", cmd_consent_grant, "授权服务在指定范围内使用资料（用户确认后再运行）")
    p.add_argument("service", help="harness / asr:<名称> / llm:<名称>")
    p.add_argument("--scopes", required=True, help="逗号分隔：transcripts,screenshots,materials,notes,learning,audio")
    p.add_argument("--note")
    p = _add(g, "revoke", cmd_consent_revoke, "撤销授权")
    p.add_argument("service")
    g = sub.add_parser("provider", help="可选的云端识别/模型服务配置").add_subparsers(metavar="<子命令>")
    p = _add(g, "add", cmd_provider_add, "添加 OpenAI 兼容的语音识别服务（不会自动开通或付费）")
    p.add_argument("name")
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--monthly-budget-minutes", type=float, help="每 30 天最多识别多少分钟（防止意外大额费用）")
    p = _add(g, "set-key", cmd_provider_key, "把 API Key 存入系统凭据库（从终端/对话框输入，不进入笔记或日志）")
    p.add_argument("name")
    p.add_argument("--gui", action="store_true")
    _add(g, "list", cmd_provider_list, "列出服务（不显示密钥）")


def cmd_consent_status(args):
    from .consent import SCOPES, status

    kb = _kb()
    st = status(kb)
    kb.close()
    emit(args, st, "\n".join(f"{s['service']}: {'已撤销' if s['revoked'] else '已授权'} 范围 {', '.join(SCOPES.get(x, x) for x in s['scopes'])}" for s in st) or "尚未授权任何云端服务")


def cmd_consent_grant(args):
    from .consent import grant

    kb = _kb()
    try:
        grant(kb, args.service, [s.strip() for s in args.scopes.split(",") if s.strip()], args.note or "")
    except ValueError as e:
        raise UserError(str(e))
    kb.close()
    emit(args, {"ok": True}, f"已记录授权：{args.service}（可随时 lw consent revoke {args.service}）")


def cmd_consent_revoke(args):
    from .consent import revoke

    kb = _kb()
    ok = revoke(kb, args.service)
    kb.close()
    emit(args, {"ok": ok}, "已撤销" if ok else "没有有效的授权")


def cmd_provider_add(args):
    kb = _kb()
    prov = kb.get_setting("providers", {}) or {}
    prov[args.name] = {"base_url": args.base_url, "model": args.model, "kind": "asr-openai-compatible", "monthly_budget_minutes": args.monthly_budget_minutes}
    kb.set_setting("providers", prov)
    kb.close()
    emit(args, prov[args.name], f"已添加 {args.name}。还需要：lw provider set-key {args.name}；并在用户同意后 lw consent grant asr:{args.name} --scopes audio")


def cmd_provider_key(args):
    from .prompt import default_prompter
    from .secrets import set_secret

    key = default_prompter(gui=True if args.gui else None).ask(f"{args.name} 的 API Key", secret=True)
    if not key:
        raise UserError("未输入")
    set_secret(f"apikey:{args.name}", key)
    from .secrets import backend_name

    emit(args, {"ok": True}, f"已存入{backend_name()}")


def cmd_provider_list(args):
    from .secrets import get_secret

    kb = _kb()
    prov = kb.get_setting("providers", {}) or {}
    kb.close()
    out = {k: {**v, "has_key": bool(get_secret(f"apikey:{k}"))} for k, v in prov.items()}
    emit(args, out, "\n".join(f"{k}: {v['base_url']} {v['model']} 密钥{'已配置' if v['has_key'] else '未配置'} 预算 {v.get('monthly_budget_minutes') or '未设'} 分钟/30天" for k, v in out.items()) or "未配置（默认只用本地识别）")


# =====================================================================  backup / kb
@spec
def spec_backup(sub):
    g = sub.add_parser("backup", help="备份/校验/恢复整个知识库（不含任何凭据）").add_subparsers(metavar="<子命令>")
    p = _add(g, "create", cmd_backup_create, "创建备份（会先记录你在笔记目录中的修改）")
    p.add_argument("--out", "-o", default=str(Path.home() / "Desktop"), help="备份文件或目录（默认桌面）")
    p = _add(g, "verify", cmd_backup_verify, "校验备份文件")
    p.add_argument("archive")
    p = _add(g, "restore", cmd_backup_restore, "从备份恢复（先完整校验；原有知识库改名保留）")
    p.add_argument("archive")
    p.add_argument("--kb", help="恢复到的知识库目录（默认当前配置）")
    p.add_argument("--force", action="store_true")
    _add(sub, "check", cmd_kb_check, "检查当前知识库的完整性与对应关系")
    g = sub.add_parser("course", help="课程设置：别名、类型、笔记偏好").add_subparsers(metavar="<子命令>")
    p = _add(g, "set", cmd_course_set, "设置课程别名/类型/笔记偏好")
    p.add_argument("course")
    p.add_argument("--alias", action="append", help="别名（如 数据库、DB）")
    p.add_argument("--subject", choices=["programming", "programming_lab", "math", "humanities", "language", "lab", "general", "pe"])
    p.add_argument("--note-prefs", help="对这门课笔记的偏好（自然语言，会写进学习包）")


def cmd_backup_create(args):
    from .backup import create
    from .media import human_bytes

    kb = _kb()
    res = create(kb, Path(args.out))
    kb.close()
    emit(args, res, f"备份完成：{res['path']}（{human_bytes(res['size'])}，{res['files']} 个文件）\n不含：密码/API Key/Cookie/本机路径。恢复：lw backup restore <文件>")


def cmd_backup_verify(args):
    from .backup import verify_archive

    res = verify_archive(Path(args.archive))
    emit(args, res, ("校验通过" if res["ok"] else "校验失败：\n- " + "\n- ".join(res["problems"])) + f"\n记录数：{res['counts']}")
    return 0 if res["ok"] else 1


def cmd_backup_restore(args):
    from .backup import BackupError, restore
    from .config import MachineConfig

    cfg = MachineConfig.load()
    target = Path(args.kb).expanduser() if args.kb else cfg.kb_path
    try:
        res = restore(Path(args.archive), target, force=args.force)
    except BackupError as e:
        raise UserError(str(e))
    if args.kb:
        cfg.kb_dir = str(target)
        cfg.save()
    lines = [f"已恢复到 {res['target']}" + (f"（原知识库已改名为 {res['moved_aside']}）" if res["moved_aside"] else ""),
             f"记录数：{res['counts']}", "下一步：lw init --notes <笔记目录>；lw auth login；lw note export（把笔记写到新电脑的笔记目录）"]
    if not res["ok"]:
        lines.append("注意：" + "；".join(res["problems"]))
    emit(args, res, "\n".join(lines))


def cmd_kb_check(args):
    from .backup import verify_dir

    kb = _kb()
    kb.db.commit()
    res = verify_dir(kb.root)
    kb.close()
    emit(args, res, ("知识库完整" if res["ok"] else "发现问题：\n- " + "\n- ".join(res["problems"])) + f"\n记录数：{res['counts']}")
    return 0 if res["ok"] else 1


def cmd_course_set(args):
    from .kb import dumps, loads

    kb = _kb()
    cid = _course_id(kb, args.course)
    r = kb.q1("SELECT * FROM courses WHERE id=?", cid)
    aliases = sorted(set(loads(r["aliases"], [])) | set(args.alias or []))
    prof = loads(r["profile"], {})
    if args.subject:
        prof["subject"] = args.subject
    if args.note_prefs:
        prof["note_prefs"] = args.note_prefs
    kb.x("UPDATE courses SET aliases=?, profile=? WHERE id=?", dumps(aliases), dumps(prof), cid)
    kb.db.commit()
    kb.close()
    emit(args, {"aliases": aliases, "profile": prof}, f"{r['name']}：别名 {'、'.join(aliases) or '无'}；类型 {prof.get('subject')}；偏好 {prof.get('note_prefs') or '无'}")


# =====================================================================  doctor / skill
@spec
def spec_doctor(sub):
    _add(sub, "doctor", cmd_doctor, "检查运行环境、登录、知识库、笔记目录与授权（AI 每次开始工作前先运行）")
    g = sub.add_parser("skill", help="安装 lecture-weaver Skill 到 AI 工具").add_subparsers(metavar="<子命令>")
    p = _add(g, "install", cmd_skill_install, "把 Skill 安装到 ~/.claude/skills 与 ~/.codex/skills（优先链接，Windows 无权限时复制）")
    p.add_argument("--target", action="append", choices=["claude", "codex"], help="默认两者都装")


def cmd_doctor(args):
    import shutil as sh

    from .asr import whisper_available
    from .config import MachineConfig
    from .secrets import get_secret
    from .session import password_account

    cfg = MachineConfig.load()
    checks = []

    def add(name, ok, detail, fix=""):
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    import sys as _sys

    from .media import ffmpeg_install_hint

    ff_fix = ffmpeg_install_hint()
    add("ffmpeg", bool(sh.which(cfg.ffmpeg_bin)), sh.which(cfg.ffmpeg_bin) or "未安装", ff_fix)
    ok, info = whisper_available()
    add("本地语音识别（可选）", ok, info, "安装 whisper.cpp（README 中有各系统的方法），模型放到 ~/.cache/whisper/ggml-large-v3-turbo-q5_0.bin")
    from . import ocr as O

    try:
        if O.backend() == "vision":
            O.ocr_binary()
        add("本地 OCR", O.backend() != "none", O.backend_description(), O.install_hint())
    except Exception as e:  # noqa: BLE001
        add("本地 OCR", False, str(e), O.install_hint())
    add("知识库", True, str(cfg.kb_path))
    add("笔记目录", bool(cfg.notes_dir), cfg.notes_dir or "未设置", "lw init --notes <Obsidian Vault 中的文件夹>")
    from .secrets import backend_name

    add("学号与密码", bool(cfg.username and get_secret(password_account(cfg.username))), f"{cfg.username or '未登录'}（密码存放：{backend_name()}）", "lw auth login --gui")
    from .secrets import SessionStore

    has_session = bool(SessionStore().load().get("ylb"))
    add("云课堂会话", has_session, "已保存（实际可用性见 lw auth status）" if has_session else "无", "lw auth login --gui")
    kb = _kb()
    from .consent import allowed

    add("AI 工具读取资料的授权", allowed(kb, "harness", ["transcripts", "screenshots"]), "已授权" if allowed(kb, "harness", ["transcripts", "screenshots"]) else "未授权",
        "先向用户说明并征得同意，再 lw consent grant harness --scopes transcripts,screenshots,materials,notes,learning")
    n_lect = kb.q1("SELECT COUNT(*) AS n FROM lectures")["n"]
    add("课时数据", n_lect > 0, f"{n_lect} 个课时", "lw sync")
    kb.close()
    lines = [f"{'✓' if c['ok'] else '✗'} {c['name']}：{c['detail']}" + ("" if c["ok"] or not c["fix"] else f"  → {c['fix']}") for c in checks]
    emit(args, checks, "\n".join(lines))


def cmd_skill_install(args):
    src = Path(__file__).resolve().parents[2] / "skill" / "lecture-weaver"
    if not src.exists():
        raise UserError(f"找不到 Skill 目录 {src}")
    targets = args.target or ["claude", "codex"]
    done = []
    for t in targets:
        base = Path.home() / (".claude" if t == "claude" else ".codex") / "skills"
        base.mkdir(parents=True, exist_ok=True)
        dst = base / "lecture-weaver"
        if dst.is_symlink() or dst.exists():
            if dst.is_symlink() and dst.resolve() == src:
                done.append(f"{dst}（链接）")
                continue
            if (dst / ".lw-copy").exists():  # our own earlier copy: refresh it
                import shutil as _sh

                _sh.rmtree(dst)
            else:
                raise UserError(f"{dst} 已存在且不是本项目安装的，请手动处理")
        try:
            dst.symlink_to(src, target_is_directory=True)
            done.append(f"{dst}（链接）")
        except OSError:
            # Windows without admin/Developer Mode cannot create symlinks.
            import shutil as _sh

            _sh.copytree(src, dst)
            (dst / ".lw-copy").write_text("copied by lw skill install; re-run after updating the project\n", encoding="utf-8")
            done.append(f"{dst}（复制；更新项目后请重新运行 lw skill install）")
    emit(args, done, "已安装：\n" + "\n".join(done))
