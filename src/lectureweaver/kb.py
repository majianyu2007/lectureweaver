"""The knowledge base: one directory with an SQLite file plus plain files.

``kb.sqlite`` holds structure and state (courses, timetable, recordings,
lectures, processing stages, materials, note versions, learning records,
consent). Content lives as ordinary files next to it so it stays readable
without this tool: transcripts (JSON), screenshots (WebP), extracted
material text (Markdown), note versions (Markdown).

All paths stored in the database are relative to the KB root, so the whole
directory can be backed up, moved to another Mac and opened again.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS terms (
  code TEXT PRIMARY KEY,               -- 2026-2027-1
  name TEXT, start_date TEXT, end_date TEXT,
  first_monday TEXT, week_count INTEGER,
  periods TEXT,                        -- JSON bell schedule from ehall
  synced_at REAL
);

CREATE TABLE IF NOT EXISTS courses (
  id TEXT PRIMARY KEY,                 -- stable: <term>:<course code or ylb id>
  term TEXT NOT NULL,
  name TEXT NOT NULL,
  code TEXT,                           -- 课程号 KCH
  teacher TEXT,
  class_ids TEXT,                      -- JSON list: ehall JXBID == ylb classIds
  ylb_course_id TEXT,
  category TEXT,
  aliases TEXT DEFAULT '[]',           -- JSON list, user-added names
  profile TEXT DEFAULT '{}',           -- JSON: subject kind + note preferences
  created_at REAL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS timetable (
  id TEXT PRIMARY KEY,                 -- KBID-based
  course_id TEXT REFERENCES courses(id) ON DELETE CASCADE,
  term TEXT, class_id TEXT, weekday INTEGER, start_period INTEGER, end_period INTEGER,
  weeks TEXT,                          -- bit string, index i = week i+1
  weeks_label TEXT, room TEXT, teacher TEXT, raw TEXT
);

CREATE TABLE IF NOT EXISTS lectures (
  id TEXT PRIMARY KEY,                 -- <date>_<course key>_p<a>-<b>
  course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  date TEXT NOT NULL, week INTEGER, weekday INTEGER,
  start_period INTEGER, end_period INTEGER,
  start_time TEXT, end_time TEXT,
  room TEXT, teacher TEXT,
  platform_group TEXT,                 -- courseTableDetailId
  match_status TEXT NOT NULL DEFAULT 'pending',  -- matched|unscheduled|ambiguous|manual|incomplete
  match_note TEXT,
  title TEXT,                          -- user/AI topic title
  dir TEXT NOT NULL,                   -- relative path of the lecture folder
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS lectures_course ON lectures(course_id, date);

CREATE TABLE IF NOT EXISTS recordings (
  id TEXT PRIMARY KEY,                 -- platform video id
  lecture_id TEXT REFERENCES lectures(id) ON DELETE SET NULL,
  course_id TEXT REFERENCES courses(id) ON DELETE CASCADE,
  name TEXT, start_time TEXT, end_time TEXT,
  week INTEGER, weekday INTEGER, segment INTEGER,
  room TEXT, teacher TEXT, task_id TEXT,
  course_table_id TEXT, course_table_detail_id TEXT,
  role TEXT DEFAULT 'primary',         -- primary|alternate
  raw TEXT, detail TEXT, fetched_at REAL
);
CREATE INDEX IF NOT EXISTS recordings_lecture ON recordings(lecture_id);

CREATE TABLE IF NOT EXISTS stages (
  lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,                -- done|partial|failed|skipped|running
  detail TEXT DEFAULT '{}',
  bytes_in INTEGER DEFAULT 0,          -- network bytes from the school
  started_at REAL, finished_at REAL, error TEXT,
  PRIMARY KEY (lecture_id, stage)
);

CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY,                 -- m_<sha12>
  sha256 TEXT UNIQUE, filename TEXT, kind TEXT,
  title TEXT, path TEXT, text_path TEXT, units INTEGER,
  imported_at REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS material_links (
  material_id TEXT REFERENCES materials(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL,           -- course|lecture|concept
  target_id TEXT NOT NULL,
  role TEXT,                           -- textbook|lab_guide|slides|code|reference|homework
  note TEXT,
  PRIMARY KEY (material_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                  -- lecture|review|cards|topic|explain
  course_id TEXT REFERENCES courses(id) ON DELETE SET NULL,
  lecture_id TEXT REFERENCES lectures(id) ON DELETE SET NULL,
  title TEXT, vault_path TEXT,         -- relative to the notes dir
  current_version INTEGER DEFAULT 0,
  exported_version INTEGER,            -- version last written to the vault
  exported_hash TEXT,                  -- sha256 of what we wrote there
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS note_versions (
  note_id TEXT REFERENCES notes(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  author TEXT NOT NULL,                -- ai|user|merge|rollback
  model TEXT, style TEXT, message TEXT,
  path TEXT NOT NULL, sha256 TEXT NOT NULL,
  created_at REAL,
  PRIMARY KEY (note_id, version)
);

CREATE TABLE IF NOT EXISTS concepts (
  id TEXT PRIMARY KEY,
  course_id TEXT REFERENCES courses(id) ON DELETE SET NULL,
  name TEXT NOT NULL, aliases TEXT DEFAULT '[]', summary TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS concept_refs (
  concept_id TEXT REFERENCES concepts(id) ON DELETE CASCADE,
  lecture_id TEXT REFERENCES lectures(id) ON DELETE CASCADE,
  t REAL, note TEXT,
  PRIMARY KEY (concept_id, lecture_id, t)
);
CREATE TABLE IF NOT EXISTS learning_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  concept_id TEXT REFERENCES concepts(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,                  -- explained|asked|answered|self_report|review|correction
  outcome TEXT,                        -- correct|partial|wrong|unsure|n/a
  evidence TEXT,                       -- what the learner actually said/did
  detail TEXT DEFAULT '{}',
  session_id TEXT, harness TEXT
);
CREATE TABLE IF NOT EXISTS mastery (
  concept_id TEXT PRIMARY KEY REFERENCES concepts(id) ON DELETE CASCADE,
  state TEXT NOT NULL,                 -- unseen|introduced|shaky|working|solid
  rationale TEXT, manual INTEGER DEFAULT 0, updated_at REAL
);
CREATE TABLE IF NOT EXISTS misconceptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  concept_id TEXT REFERENCES concepts(id) ON DELETE CASCADE,
  text TEXT NOT NULL, count INTEGER DEFAULT 1,
  first_seen REAL, last_seen REAL, resolved_at REAL
);

CREATE TABLE IF NOT EXISTS consents (
  service TEXT PRIMARY KEY,            -- harness|asr:<provider>|llm:<provider>
  scopes TEXT NOT NULL,                -- JSON list
  granted_at REAL, revoked_at REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  service TEXT, ts REAL, units REAL, unit TEXT, cost REAL, note TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  text, kind UNINDEXED, ref UNINDEXED, lecture_id UNINDEXED, course_id UNINDEXED, t UNINDEXED,
  tokenize = 'trigram'
);
"""


class KB:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "kb.sqlite"
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # Several `lw process` workers may write at once: WAL lets readers and
        # one writer proceed together, busy_timeout makes writers queue.
        try:
            self.db.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # e.g. a file system without shared memory; rollback journal still works
        self.db.execute("PRAGMA busy_timeout = 30000")
        self.db.executescript(SCHEMA)
        self._migrate()
        for d in ("lectures", "materials", "notes"):
            (self.root / d).mkdir(exist_ok=True)

    @classmethod
    def open(cls) -> "KB":
        from .config import MachineConfig

        return cls(MachineConfig.load().kb_path)

    def _migrate(self) -> None:
        v = self.get_meta("schema_version")
        if v is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created_at", str(time.time()))
        # Future migrations: if int(v) < 2: ...

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ basics
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, *args) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, *args) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    def get_meta(self, key: str) -> str | None:
        r = self.q1("SELECT value FROM meta WHERE key=?", key)
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)
        self.db.commit()

    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.q1("SELECT value FROM settings WHERE key=?", key)
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key,
            json.dumps(value, ensure_ascii=False),
        )
        self.db.commit()

    def abs(self, rel: str | Path) -> Path:
        return self.root / rel

    def rel(self, p: Path) -> str:
        # POSIX separators so a KB written on Windows opens on macOS/Linux too
        return Path(p).resolve().relative_to(self.root.resolve()).as_posix()

    # ------------------------------------------------------------ stages
    def stage(self, lecture_id: str, stage: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM stages WHERE lecture_id=? AND stage=?", lecture_id, stage)

    def set_stage(
        self,
        lecture_id: str,
        stage: str,
        status: str,
        detail: dict | None = None,
        bytes_in: int = 0,
        error: str | None = None,
        started_at: float | None = None,
    ) -> None:
        now = time.time()
        self.x(
            """INSERT INTO stages(lecture_id,stage,status,detail,bytes_in,started_at,finished_at,error)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(lecture_id,stage) DO UPDATE SET status=excluded.status, detail=excluded.detail,
                 bytes_in=excluded.bytes_in, started_at=COALESCE(excluded.started_at, stages.started_at),
                 finished_at=excluded.finished_at, error=excluded.error""",
            lecture_id,
            stage,
            status,
            json.dumps(detail or {}, ensure_ascii=False),
            bytes_in,
            started_at or now,
            None if status == "running" else now,
            error,
        )
        self.db.commit()

    # ------------------------------------------------------------ search index
    def index_replace(self, kind: str, ref: str, rows: list[tuple[str, str | None, str | None, float | None]]) -> None:
        """Replace all index rows of (kind, ref). rows: (text, lecture_id, course_id, t)."""
        self.x("DELETE FROM search WHERE kind=? AND ref=?", kind, ref)
        self.db.executemany(
            "INSERT INTO search(text,kind,ref,lecture_id,course_id,t) VALUES(?,?,?,?,?,?)",
            [(text, kind, ref, lid, cid, t) for text, lid, cid, t in rows if text and text.strip()],
        )
        self.db.commit()


def try_lock(path: Path, wait: bool = False):
    """Exclusive lock on ``path``; returns the open file (keep it, closing it
    releases the lock). Non-blocking by default: None if another process holds
    it. ``wait=True`` blocks until the lock is free. Works on macOS, Linux and
    Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if not wait:
                f.close()
                return None
            time.sleep(2)


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default
