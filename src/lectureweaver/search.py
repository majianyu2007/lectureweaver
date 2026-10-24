"""Full-text search across transcripts, screenshot OCR, materials and notes.

SQLite FTS5 with the trigram tokenizer gives substring search for Chinese
(no word segmentation needed). Queries shorter than three characters use a
LIKE scan, which is fine at personal-library scale.
"""

from __future__ import annotations

import re

from .kb import KB

KIND_NAMES = {"transcript": "课堂转写", "frame": "截图文字", "material": "资料", "note": "笔记"}


def search(kb: KB, query: str, course_id: str | None = None, kinds: list[str] | None = None, limit: int = 30) -> list[dict]:
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return []
    where, args = [], []
    long_terms = [t for t in terms if len(t) >= 3]
    if long_terms and len(long_terms) == len(terms):
        where.append("search MATCH ?")
        args.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in terms))
    else:
        for t in terms:
            where.append("text LIKE ?")
            args.append(f"%{t}%")
    if course_id:
        where.append("course_id=?")
        args.append(course_id)
    if kinds:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        args += kinds
    rows = kb.q(f"SELECT text, kind, ref, lecture_id, course_id FROM search WHERE {' AND '.join(where)} LIMIT ?", *args, limit * 4)
    out = []
    seen = set()
    for r in rows:
        text = r["text"]
        head = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", text)  # one row = one transcript line / one screenshot
        for frag in _fragments(text, terms):
            m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", frag["line"])
            clock = m.group(1) if m else (head.group(1) if head else None)
            key = (r["kind"], r["ref"], clock, frag["snippet"].strip())
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": r["kind"], "kind_name": KIND_NAMES.get(r["kind"], r["kind"]), "ref": r["ref"], "lecture": r["lecture_id"],
                        "course": r["course_id"], "clock": clock, "snippet": frag["snippet"], "unit": frag.get("unit")})
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # enrich with dates / course names
    for o in out:
        if o["lecture"]:
            l = kb.q1("SELECT l.date, l.week, c.name FROM lectures l JOIN courses c ON c.id=l.course_id WHERE l.id=?", o["lecture"])
            if l:
                o["date"], o["week"], o["course_name"] = l["date"], l["week"], l["name"]
        elif o["kind"] == "material":
            m = kb.q1("SELECT title FROM materials WHERE id=?", o["ref"])
            o["material_title"] = m["title"] if m else o["ref"]
    out.sort(key=lambda o: (o.get("date") or "9999", o.get("clock") or ""))
    return out


def _fragments(text: str, terms: list[str], width: int = 60) -> list[dict]:
    """Best-matching line(s) of a document with some context."""
    lines = text.splitlines() or [text]
    hits = []
    unit = None
    for ln in lines:
        um = re.match(r"\[(p\.\d+|slide \d+|§\d+|file)\]", ln)
        if um:
            unit = um.group(1)
        if all(t.lower() in ln.lower() for t in terms):
            i = ln.lower().find(terms[0].lower())
            a = max(0, i - width)
            hits.append({"line": ln, "snippet": ("…" if a else "") + ln[a : i + len(terms[0]) + width] + ("…" if i + width < len(ln) else ""), "unit": unit})
    if not hits:
        i = text.lower().find(terms[0].lower())
        a = max(0, i - width)
        hits.append({"line": text[a : i + width], "snippet": text[a : i + width].replace("\n", " "), "unit": unit})
    return hits[:3]
