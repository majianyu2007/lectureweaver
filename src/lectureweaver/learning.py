"""Persistent learning state, independent of any AI harness.

What is stored is *evidence*, not a score: each event says what happened
("asked to compute X; answered correctly", "confused 除法 with 差", "AI
explained the definition"). A concept's state is derived from that evidence
with simple rules that can be read and challenged:

  unseen      no evidence at all
  introduced  only explanations or exposure - being told is not knowing
  shaky       the latest attempt was wrong/partial, the learner said they
              don't get it, or an unresolved misconception exists
  working     at least one correct attempt, latest attempt correct
  solid       correct on at least two separate days (one of them an
              application/transfer question or a later review), latest
              attempt correct, no open misconception

A manual override is allowed but keeps its rationale, and new evidence
after the override takes precedence again.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime

from .kb import KB, dumps, loads

STATES = ["unseen", "introduced", "shaky", "working", "solid"]
STATE_NAMES = {"unseen": "未接触", "introduced": "讲过/看过（未检验）", "shaky": "不稳", "working": "基本会", "solid": "掌握"}
EVENT_KINDS = ["explained", "asked", "answered", "self_report", "review", "correction", "exposure"]
OUTCOMES = ["correct", "partial", "wrong", "unsure", "n/a"]


class LearnError(Exception):
    pass


def concept_id(course_id: str | None, name: str) -> str:
    return "c_" + hashlib.sha1(f"{course_id or ''}|{_norm(name)}".encode()).hexdigest()[:10]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def find_concept(kb: KB, name: str, course_id: str | None = None):
    n = _norm(name)
    rows = kb.q("SELECT * FROM concepts" + (" WHERE course_id=?" if course_id else ""), *([course_id] if course_id else []))
    for r in rows:
        if _norm(r["name"]) == n or n in [_norm(a) for a in loads(r["aliases"], [])]:
            return r
    return None


def ensure_concept(kb: KB, name: str, course_id: str | None, summary: str | None = None, aliases: list[str] | None = None):
    r = find_concept(kb, name, course_id)
    if r:
        if summary and not r["summary"]:
            kb.x("UPDATE concepts SET summary=? WHERE id=?", summary, r["id"])
        if aliases:
            al = sorted(set(loads(r["aliases"], [])) | set(aliases))
            kb.x("UPDATE concepts SET aliases=? WHERE id=?", dumps(al), r["id"])
        kb.db.commit()
        return kb.q1("SELECT * FROM concepts WHERE id=?", r["id"])
    cid = concept_id(course_id, name)
    kb.x("INSERT INTO concepts(id,course_id,name,aliases,summary,created_at) VALUES(?,?,?,?,?,?)", cid, course_id, name.strip(), dumps(aliases or []), summary, time.time())
    kb.db.commit()
    return kb.q1("SELECT * FROM concepts WHERE id=?", cid)


def link_lecture(kb: KB, cid: str, lecture_id: str, clock: str | None = None, note: str | None = None) -> None:
    t = None
    if clock:
        h, m, *s = (int(x) for x in clock.split(":"))
        t = h * 3600 + m * 60 + (s[0] if s else 0)
    kb.x("INSERT OR REPLACE INTO concept_refs(concept_id,lecture_id,t,note) VALUES(?,?,?,?)", cid, lecture_id, t if t is not None else -1, note)
    kb.db.commit()


def log_event(kb: KB, cid: str, kind: str, outcome: str | None = None, evidence: str | None = None, detail: dict | None = None,
              session: str | None = None, harness: str | None = None, misconception: str | None = None) -> dict:
    if kind not in EVENT_KINDS:
        raise LearnError(f"事件类型可选：{', '.join(EVENT_KINDS)}")
    if outcome and outcome not in OUTCOMES:
        raise LearnError(f"结果可选：{', '.join(OUTCOMES)}")
    if kind == "answered" and not outcome:
        raise LearnError("answered 事件需要 --outcome（correct/partial/wrong/unsure）")
    if kind in ("answered", "self_report") and not evidence:
        raise LearnError("请用 --evidence 记录学习者实际的回答/表述（这是判断掌握情况的依据）")
    now = time.time()
    kb.x("INSERT INTO learning_events(concept_id,ts,kind,outcome,evidence,detail,session_id,harness) VALUES(?,?,?,?,?,?,?,?)",
         cid, now, kind, outcome, evidence, dumps(detail or {}), session, harness)
    if misconception:
        r = kb.q1("SELECT * FROM misconceptions WHERE concept_id=? AND text=? AND resolved_at IS NULL", cid, misconception)
        if r:
            kb.x("UPDATE misconceptions SET count=count+1, last_seen=? WHERE id=?", now, r["id"])
        else:
            kb.x("INSERT INTO misconceptions(concept_id,text,count,first_seen,last_seen) VALUES(?,?,1,?,?)", cid, misconception, now, now)
    kb.db.commit()
    return derive(kb, cid, store=True)


def resolve_misconception(kb: KB, mid: int, evidence: str) -> None:
    r = kb.q1("SELECT * FROM misconceptions WHERE id=?", mid)
    if not r:
        raise LearnError("没有这条误解记录")
    kb.x("UPDATE misconceptions SET resolved_at=? WHERE id=?", time.time(), mid)
    kb.x("INSERT INTO learning_events(concept_id,ts,kind,outcome,evidence,detail) VALUES(?,?,?,?,?,?)",
         r["concept_id"], time.time(), "correction", "correct", evidence, dumps({"resolved_misconception": r["text"]}))
    kb.db.commit()
    derive(kb, r["concept_id"], store=True)


def derive(kb: KB, cid: str, store: bool = False) -> dict:
    evs = kb.q("SELECT * FROM learning_events WHERE concept_id=? ORDER BY ts", cid)
    open_mis = kb.q("SELECT * FROM misconceptions WHERE concept_id=? AND resolved_at IS NULL", cid)
    manual = kb.q1("SELECT * FROM mastery WHERE concept_id=? AND manual=1", cid)
    attempts = [e for e in evs if e["kind"] in ("answered", "review", "correction") and e["outcome"] in ("correct", "partial", "wrong", "unsure")]
    reports = [e for e in evs if e["kind"] == "self_report"]
    state, why = "unseen", "还没有任何学习记录"
    if evs:
        state, why = "introduced", "只有讲解/浏览记录，还没有通过作答检验"
    if attempts:
        last = attempts[-1]
        correct = [e for e in attempts if e["outcome"] == "correct"]
        days = {datetime.fromtimestamp(e["ts"]).date() for e in correct}
        applied = any(loads(e["detail"], {}).get("type") in ("apply", "transfer", "review") or e["kind"] == "review" for e in correct)
        if last["outcome"] != "correct":
            state, why = "shaky", f"最近一次作答{_o(last['outcome'])}：{(last['evidence'] or '')[:60]}"
        elif len(days) >= 2 and applied and not open_mis:
            state, why = "solid", f"在 {len(days)} 个不同日期答对（含应用/复习题），无未解决的误解"
        else:
            state, why = "working", f"已答对 {len(correct)} 次" + ("（同一天内）" if len(days) < 2 else "") + ("，仍有未解决误解" if open_mis else "") + "，尚需隔天或换题再检验"
    if reports and (not attempts or reports[-1]["ts"] > attempts[-1]["ts"]) and reports[-1]["outcome"] in ("unsure", "wrong"):
        state, why = "shaky", f"学习者自述还不理解：{(reports[-1]['evidence'] or '')[:60]}"
    if open_mis and state in ("working", "solid", "introduced"):
        state = "shaky" if state != "introduced" else state
        why += f"；未解决的误解：{open_mis[-1]['text'][:40]}"
    if manual and (not evs or manual["updated_at"] >= evs[-1]["ts"]):
        state, why = manual["state"], f"手动设定：{manual['rationale']}"
    if store:
        kb.x("INSERT INTO mastery(concept_id,state,rationale,manual,updated_at) VALUES(?,?,?,0,?) ON CONFLICT(concept_id) DO UPDATE SET state=excluded.state, rationale=excluded.rationale, manual=CASE WHEN mastery.manual=1 AND mastery.updated_at>=? THEN 1 ELSE 0 END, updated_at=excluded.updated_at",
             cid, state, why, time.time(), evs[-1]["ts"] if evs else 0)
        kb.db.commit()
    return {"concept": cid, "state": state, "state_name": STATE_NAMES[state], "rationale": why,
            "events": len(evs), "open_misconceptions": [m["text"] for m in open_mis]}


def _o(o: str) -> str:
    return {"wrong": "答错", "partial": "部分正确", "unsure": "不确定", "correct": "正确"}.get(o, o)


def set_manual(kb: KB, cid: str, state: str, rationale: str) -> None:
    if state not in STATES:
        raise LearnError(f"状态可选：{', '.join(STATES)}")
    kb.x("INSERT INTO mastery(concept_id,state,rationale,manual,updated_at) VALUES(?,?,?,1,?) ON CONFLICT(concept_id) DO UPDATE SET state=excluded.state, rationale=excluded.rationale, manual=1, updated_at=excluded.updated_at",
         cid, state, rationale, time.time())
    kb.db.commit()


def overview(kb: KB, course_id: str | None = None) -> list[dict]:
    rows = kb.q("SELECT * FROM concepts" + (" WHERE course_id=?" if course_id else "") + " ORDER BY name", *([course_id] if course_id else []))
    out = []
    for c in rows:
        d = derive(kb, c["id"])
        last = kb.q1("SELECT * FROM learning_events WHERE concept_id=? ORDER BY ts DESC LIMIT 1", c["id"])
        lects = [r["lecture_id"] for r in kb.q("SELECT DISTINCT lecture_id FROM concept_refs WHERE concept_id=?", c["id"])]
        out.append({"id": c["id"], "name": c["name"], "course": c["course_id"], **d, "lectures": lects,
                    "last_activity": datetime.fromtimestamp(last["ts"]).strftime("%Y-%m-%d") if last else None,
                    "last_evidence": last["evidence"] if last else None})
    return out


def next_steps(kb: KB, course_id: str | None = None, limit: int = 8) -> list[dict]:
    """What to work on next: open misconceptions, shaky, untested, stale."""
    items = overview(kb, course_id)
    now = datetime.now()
    pri = []
    for it in items:
        score = None
        reason = ""
        if it["open_misconceptions"]:
            score, reason = 0, "有未解决的误解：" + it["open_misconceptions"][-1]
        elif it["state"] == "shaky":
            score, reason = 1, it["rationale"]
        elif it["state"] == "introduced":
            score, reason = 2, "讲过但从未检验"
        elif it["state"] == "working":
            days = (now - datetime.strptime(it["last_activity"], "%Y-%m-%d")).days if it["last_activity"] else 99
            if days >= 3:
                score, reason = 3, f"{days} 天前答对过，适合换题复查"
        if score is not None:
            pri.append({**it, "priority": score, "reason": reason})
    pri.sort(key=lambda x: (x["priority"], x["last_activity"] or ""))
    return pri[:limit]


def history(kb: KB, cid: str, limit: int = 30) -> list[dict]:
    return [
        {"time": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M"), "kind": r["kind"], "outcome": r["outcome"], "evidence": r["evidence"],
         "detail": loads(r["detail"], {}), "harness": r["harness"]}
        for r in kb.q("SELECT * FROM learning_events WHERE concept_id=? ORDER BY ts DESC LIMIT ?", cid, limit)
    ]


def export_state(kb: KB) -> dict:
    return {
        "concepts": [dict(r) for r in kb.q("SELECT * FROM concepts")],
        "events": [dict(r) for r in kb.q("SELECT * FROM learning_events")],
        "misconceptions": [dict(r) for r in kb.q("SELECT * FROM misconceptions")],
        "mastery": [dict(r) for r in kb.q("SELECT * FROM mastery")],
        "exported_at": time.time(),
    }


__all__ = ["ensure_concept", "find_concept", "log_event", "derive", "overview", "next_steps", "history", "set_manual", "export_state", "json"]
