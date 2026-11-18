"""SQLite storage. One connection per call, WAL mode, atomic claim helpers."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT, email_key TEXT, phone TEXT, phone_key TEXT,
  first_name TEXT, last_name TEXT, company TEXT, job_title TEXT, domain TEXT,
  source TEXT, status TEXT NOT NULL DEFAULT 'new',
  tier TEXT, tier_reasons TEXT DEFAULT '[]', missing TEXT DEFAULT '[]',
  consent INTEGER NOT NULL DEFAULT 0, consent_source TEXT, consent_at REAL,
  tz TEXT, owner TEXT, paused_human INTEGER NOT NULL DEFAULT 0,
  flags TEXT DEFAULT '[]', questions_asked INTEGER NOT NULL DEFAULT 0,
  touches INTEGER NOT NULL DEFAULT 0, crm_contact_id TEXT,
  tier_override TEXT, override_reason TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  first_response_at REAL, last_inbound_at REAL, last_outbound_at REAL
);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email_key);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_key);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, external_id TEXT NOT NULL,
  kind TEXT NOT NULL, received_at REAL NOT NULL, UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, direction TEXT NOT NULL,
  channel TEXT, body TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
  sent_at REAL, external_id TEXT, send_key TEXT, author TEXT, meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_msg_lead ON messages(lead_id);
CREATE TABLE IF NOT EXISTS fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, name TEXT NOT NULL,
  value TEXT NOT NULL, evidence TEXT, confidence REAL, source TEXT NOT NULL,
  message_id INTEGER, created_at REAL NOT NULL, superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fields_lead ON fields(lead_id);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, kind TEXT NOT NULL,
  step_key TEXT NOT NULL, subject TEXT, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_review', send_state TEXT, send_key TEXT,
  channel_message_id TEXT, created_at REAL NOT NULL, decided_at REAL, decided_by TEXT, reason TEXT,
  not_before REAL, UNIQUE(lead_id, step_key)
);
CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, step INTEGER NOT NULL,
  due_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', cancel_reason TEXT,
  UNIQUE(lead_id, step)
);
CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL UNIQUE, card TEXT NOT NULL,
  owner TEXT, status TEXT NOT NULL DEFAULT 'open', kind TEXT NOT NULL DEFAULT 'sales',
  created_at REAL NOT NULL, sla_due REAL NOT NULL, ack_at REAL, escalated_at REAL, crm_task_id TEXT
);
CREATE TABLE IF NOT EXISTS suppression (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL,
  reason TEXT, created_at REAL NOT NULL, UNIQUE(kind, key)
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, lead_id INTEGER, actor TEXT NOT NULL,
  event TEXT NOT NULL, detail TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_lead ON audit(lead_id);
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL
);
"""


def connect(path: str = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


class DB:
    """Thin wrapper holding one shared connection guarded by a re-entrant lock."""

    def __init__(self, path: str = None):
        self.path = path or config.DB_PATH
        self.con = connect(self.path)
        self.con.executescript(SCHEMA)
        self.lock = _write_lock

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).lastrowid

    def run_rowcount(self, sql, args=()):
        """Execute a write and return the number of rows it changed. Claim winners are decided by this."""
        with self.lock:
            return self.con.execute(sql, args).rowcount

    @contextmanager
    def tx(self):
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except Exception:
                self.con.execute("ROLLBACK")
                raise
            else:
                self.con.execute("COMMIT")

    # settings helpers
    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
