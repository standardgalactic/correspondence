"""SQLite batch store with an append-only, hash-chained ledger."""
import json
import sqlite3
import zlib
from pathlib import Path

from .util import canon, sha256_hex

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS slot_state(
  batch TEXT NOT NULL, slot TEXT NOT NULL, idx INTEGER NOT NULL,
  state TEXT NOT NULL, reason TEXT,
  PRIMARY KEY(batch, slot));
CREATE INDEX IF NOT EXISTS slot_state_idx ON slot_state(batch, idx);
CREATE INDEX IF NOT EXISTS slot_state_state ON slot_state(batch, state, idx);
CREATE TABLE IF NOT EXISTS stage_a(
  batch TEXT NOT NULL, slot TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
  plan BLOB, cands TEXT, purpose TEXT, plan_sig TEXT, fallback TEXT,
  PRIMARY KEY(batch, slot));
CREATE TABLE IF NOT EXISTS draft(
  batch TEXT NOT NULL, slot TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  fingerprint TEXT NOT NULL, struct_sig TEXT NOT NULL, plan_sig TEXT NOT NULL,
  rule_path TEXT NOT NULL, purpose TEXT NOT NULL, project TEXT NOT NULL,
  PRIMARY KEY(batch, slot));
CREATE TABLE IF NOT EXISTS fingerprint(
  batch TEXT NOT NULL, fp TEXT NOT NULL, slot TEXT NOT NULL,
  PRIMARY KEY(batch, fp));
CREATE TABLE IF NOT EXISTS ledger(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, slot TEXT,
  record TEXT, payload TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""

GENESIS = "0" * 64


def pack(obj):
    return zlib.compress(canon(obj).encode("utf-8"), 6)


def unpack(blob):
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Store:
    def __init__(self, run_dir):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "batch.sqlite"
        self.con = sqlite3.connect(self.path)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(SCHEMA)
        self.con.commit()
        row = self.con.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        self.last = row[0] if row else GENESIS

    def close(self):
        self.con.close()

    def meta(self, key, default=None):
        row = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def ledger(self, kind, slot, record, payload):
        body = canon({"kind": kind, "slot": slot, "record": record, "payload": payload})
        h = sha256_hex(self.last + body)
        self.con.execute("INSERT INTO ledger(kind,slot,record,payload,prev,hash) VALUES(?,?,?,?,?,?)",
                         (kind, slot, record, canon(payload), self.last, h))
        self.last = h
        return h

    def verify_ledger(self):
        prev = GENESIS
        n = 0
        for seq, kind, slot, record, payload, p, h in self.con.execute(
                "SELECT seq,kind,slot,record,payload,prev,hash FROM ledger ORDER BY seq"):
            body = canon({"kind": kind, "slot": slot, "record": record, "payload": json.loads(payload)})
            if p != prev or sha256_hex(prev + body) != h:
                return False, seq
            prev = h
            n += 1
        return True, n
