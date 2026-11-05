"""SQLite persistence: disputes, proposals (with a strict state machine) and a hash-chained audit log.

The audit log is append-only by construction (no update/delete API) and tamper-evident: each row's
hash covers the previous row's hash, so editing or removing any row breaks `verify_audit()`.
Never put credentials or tokens in a payload.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS disputes(
  id TEXT PRIMARY KEY, raw TEXT NOT NULL, status TEXT, update_time TEXT, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, dispute_id TEXT NOT NULL, source_update TEXT NOT NULL,
  decision TEXT NOT NULL, draft TEXT, state TEXT NOT NULL, decided_by TEXT, result TEXT,
  created_at TEXT NOT NULL, decided_at TEXT, UNIQUE(dispute_id, source_update));
CREATE TABLE IF NOT EXISTS webhook_events(
  id TEXT PRIMARY KEY, event_type TEXT, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL,
  payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
"""

GENESIS = "0" * 64
# PENDING -> APPROVED | REJECTED | SUPERSEDED ; APPROVED -> EXECUTED | FAILED | UNCERTAIN ; UNCERTAIN -> EXECUTED | FAILED
TRANSITIONS = {
    "PENDING": {"APPROVED", "REJECTED", "SUPERSEDED"},
    "APPROVED": {"EXECUTED", "FAILED", "UNCERTAIN"},
    "UNCERTAIN": {"EXECUTED", "FAILED", "UNCERTAIN"},   # sent, but PayPal's answer never arrived; reconcile, then retry or close
}


class InvalidTransition(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    # ---- audit ---------------------------------------------------------
    def audit(self, actor: str, event: str, payload: dict | None = None) -> int:
        with self._lock:
            row = self._db.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            seq = (row["seq"] + 1) if row else 1
            ts, body = _now(), _canon(payload or {})
            h = hashlib.sha256((prev + _canon([seq, ts, actor, event, body])).encode()).hexdigest()
            self._db.execute("INSERT INTO audit(seq, ts, actor, event, payload, prev_hash, hash) VALUES(?,?,?,?,?,?,?)",
                             (seq, ts, actor, event, body, prev, h))
            self._db.commit()
            return seq

    def audit_rows(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM audit ORDER BY seq")]

    def verify_audit(self) -> tuple[bool, int | None]:
        """Return (ok, first_bad_seq)."""
        prev = GENESIS
        for r in self.audit_rows():
            want = hashlib.sha256((prev + _canon([r["seq"], r["ts"], r["actor"], r["event"], r["payload"]])).encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != want:
                return False, r["seq"]
            prev = r["hash"]
        return True, None

    # ---- disputes ------------------------------------------------------
    def upsert_dispute(self, raw: dict) -> bool:
        """Store the latest raw body. Returns True if it is new or its update_time changed."""
        did, upd = raw["dispute_id"], raw.get("update_time") or ""
        with self._lock:
            cur = self._db.execute("SELECT update_time FROM disputes WHERE id=?", (did,)).fetchone()
            changed = cur is None or cur["update_time"] != upd
            self._db.execute(
                "INSERT INTO disputes(id, raw, status, update_time, seen_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET raw=excluded.raw, status=excluded.status, "
                "update_time=excluded.update_time, seen_at=excluded.seen_at",
                (did, _canon(raw), raw.get("status"), upd, _now()))
            self._db.commit()
            return changed

    def get_dispute(self, did: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT raw FROM disputes WHERE id=?", (did,)).fetchone()
            return json.loads(r["raw"]) if r else None

    # ---- webhook idempotency -------------------------------------------
    def claim_webhook_event(self, event_id: str, event_type: str) -> bool:
        """True the first time an event id is seen, False for a replay or PayPal retry."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO webhook_events(id, event_type, received_at) VALUES(?,?,?)",
                                   (event_id, event_type, _now()))
            self._db.commit()
            return cur.rowcount == 1

    # ---- proposals -----------------------------------------------------
    def create_proposal(self, dispute_id: str, source_update: str, decision: dict, draft: str | None = None) -> int | None:
        """Create a PENDING proposal; supersede older pending ones. None if this exact version already has one."""
        with self._lock:
            if self._db.execute("SELECT 1 FROM proposals WHERE dispute_id=? AND source_update=?",
                                (dispute_id, source_update)).fetchone():
                return None
            self._db.execute("UPDATE proposals SET state='SUPERSEDED', decided_at=? WHERE dispute_id=? AND state='PENDING'",
                             (_now(), dispute_id))
            cur = self._db.execute(
                "INSERT INTO proposals(dispute_id, source_update, decision, draft, state, created_at) VALUES(?,?,?,?, 'PENDING', ?)",
                (dispute_id, source_update, _canon(decision), draft, _now()))
            self._db.commit()
            return cur.lastrowid

    def supersede_pending(self, dispute_id: str) -> list[int]:
        """Retire pending proposals that can no longer be acted on (PayPal stopped offering the action)."""
        with self._lock:
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM proposals WHERE dispute_id=? AND state='PENDING'", (dispute_id,))]
            if ids:
                self._db.execute("UPDATE proposals SET state='SUPERSEDED', decided_at=? WHERE dispute_id=? AND state='PENDING'",
                                 (_now(), dispute_id))
                self._db.commit()
            return ids

    def get_proposal(self, pid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
            return self._row(r)

    def list_proposals(self, state: str | None = None) -> list[dict]:
        with self._lock:
            q, a = ("SELECT * FROM proposals WHERE state=? ORDER BY id DESC", (state,)) if state else \
                   ("SELECT * FROM proposals ORDER BY id DESC", ())
            return [self._row(r) for r in self._db.execute(q, a)]

    def set_advice(
        self, pid: int, analysis: dict, draft: str, recommendation: dict | None = None
    ) -> None:
        """Attach the agent's advisory analysis, draft, and optional AI recommendation.

        Never touches the policy fields of the decision.
        """
        with self._lock:
            r = self._db.execute("SELECT decision FROM proposals WHERE id=? AND state='PENDING'", (pid,)).fetchone()
            if r is None:
                return
            dec = json.loads(r["decision"]); dec["analysis"] = analysis
            if recommendation is not None:
                dec["recommendation"] = recommendation
            self._db.execute("UPDATE proposals SET decision=?, draft=? WHERE id=?", (_canon(dec), draft, pid))
            self._db.commit()

    def set_draft(self, pid: int, draft: str) -> None:
        with self._lock:
            self._db.execute("UPDATE proposals SET draft=? WHERE id=? AND state='PENDING'", (draft, pid))
            self._db.commit()

    def transition(self, pid: int, new: str, by: str | None = None, result: dict | None = None) -> None:
        with self._lock:
            r = self._db.execute("SELECT state FROM proposals WHERE id=?", (pid,)).fetchone()
            if r is None:
                raise KeyError(pid)
            if new not in TRANSITIONS.get(r["state"], set()):
                raise InvalidTransition(f"{r['state']} -> {new} not allowed")
            self._db.execute("UPDATE proposals SET state=?, decided_by=COALESCE(?, decided_by), result=COALESCE(?, result), decided_at=? WHERE id=?",
                             (new, by, _canon(result) if result is not None else None, _now(), pid))
            self._db.commit()

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["decision"] = json.loads(d["decision"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d
