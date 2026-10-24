"""Consent for sending material to cloud services.

Rule: the first time a service is used, the user sees what kinds of data go
where and why, and decides. A grant is remembered (in the knowledge base, so
it migrates with it) and reused within its scopes until revoked. A new
service or a wider scope needs a new decision. Consent to send data is not
consent to spend money: paid services also have a usage budget.

Services:
* ``harness``      - the AI tool you are using (Claude Code, Codex, …) reads
                     packets: transcripts, screenshots, material excerpts,
                     notes, learning records. These go to that tool's model
                     provider under your account with it.
* ``asr:<name>``   - a cloud speech-recognition endpoint: lecture audio.
* ``llm:<name>``   - an extra model API configured here (not required).
Local processing (whisper.cpp, Apple Vision OCR, ffmpeg) needs no consent.
"""

from __future__ import annotations

import json
import time

from .kb import KB, loads

SCOPES = {
    "transcripts": "课堂转写文本",
    "screenshots": "课堂截图（录屏/教师机位画面，可能含同学背影）",
    "materials": "你导入的课程资料内容",
    "notes": "已生成的笔记",
    "learning": "学习记录（答题情况、薄弱点）",
    "audio": "课堂音频",
}

DISCLOSURE = {
    "harness": "当前使用的 AI 工具（如 Claude Code / Codex）会读取学习包来整理笔记和讲解，内容会发送给该工具的模型服务商，用途仅为本次整理与教学。",
    "asr": "课堂音频会上传到你配置的云端语音识别服务，用于生成转写；可能按时长计费。",
    "llm": "选定内容会发送到你配置的模型 API。",
}


class ConsentRequired(Exception):
    def __init__(self, service: str, scopes: list[str]):
        self.service = service
        self.scopes = scopes
        kind = service.split(":")[0]
        what = "、".join(SCOPES.get(s, s) for s in scopes)
        super().__init__(
            f"需要你的授权：{DISCLOSURE.get(kind, '')} 涉及资料：{what}。"
            f"同意后运行 `lw consent grant {service} --scopes {','.join(scopes)}`（之后在此范围内不再逐次询问，可随时 `lw consent revoke {service}`）。"
        )


def status(kb: KB) -> list[dict]:
    out = []
    for r in kb.q("SELECT * FROM consents ORDER BY service"):
        out.append({
            "service": r["service"], "scopes": loads(r["scopes"], []), "granted_at": r["granted_at"],
            "revoked": bool(r["revoked_at"]), "note": r["note"],
        })
    return out


def grant(kb: KB, service: str, scopes: list[str], note: str = "") -> None:
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"未知范围：{', '.join(bad)}；可选：{', '.join(SCOPES)}")
    kb.x(
        """INSERT INTO consents(service,scopes,granted_at,revoked_at,note) VALUES(?,?,?,NULL,?)
           ON CONFLICT(service) DO UPDATE SET scopes=excluded.scopes, granted_at=excluded.granted_at, revoked_at=NULL, note=excluded.note""",
        service, json.dumps(sorted(set(scopes))), time.time(), note,
    )
    kb.db.commit()


def revoke(kb: KB, service: str) -> bool:
    cur = kb.x("UPDATE consents SET revoked_at=? WHERE service=? AND revoked_at IS NULL", time.time(), service)
    kb.db.commit()
    return cur.rowcount > 0


def allowed(kb: KB, service: str, scopes: list[str]) -> bool:
    r = kb.q1("SELECT * FROM consents WHERE service=? AND revoked_at IS NULL", service)
    return bool(r) and set(scopes) <= set(loads(r["scopes"], []))


def check(kb: KB, service: str, scopes: list[str]) -> None:
    if not allowed(kb, service, scopes):
        missing = scopes
        r = kb.q1("SELECT * FROM consents WHERE service=? AND revoked_at IS NULL", service)
        if r:
            have = set(loads(r["scopes"], []))
            missing = sorted(have | set(scopes))
        raise ConsentRequired(service, missing)


def require(kb: KB, service: str, scope: str) -> dict:
    """Check consent and return the provider config for cloud services."""
    check(kb, service, [scope])
    name = service.split(":", 1)[1]
    providers = kb.get_setting("providers", {}) or {}
    if name not in providers:
        raise ValueError(f"未配置服务 {name}（lw provider add）")
    budget = providers[name].get("monthly_budget_minutes")
    if budget is not None:
        month_start = time.time() - 30 * 86400
        used = kb.q1("SELECT COALESCE(SUM(units),0) AS u FROM usage WHERE service=? AND ts>=?", service, month_start)["u"]
        if used >= budget:
            raise ValueError(f"{name} 近 30 天已用 {used:.0f} 分钟，达到你设定的上限 {budget}；如需继续请调整预算")
    return {"name": name, **providers[name]}
