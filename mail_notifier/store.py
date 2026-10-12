"""Transactional UID checkpoints and a durable notification outbox."""

import fcntl
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Account

WorkKey = tuple[str, str, int, int]
_KEY = "account_id=? AND mailbox=? AND uidvalidity=? AND uid=?"
_UNFINISHED = "status IN ('pending', 'ready')"


class UIDValidityChanged(RuntimeError):
    code = "uidvalidity_changed"


@dataclass(frozen=True)
class WorkItem:
    key: WorkKey
    status: str
    notification: str | None
    attempts: int
    retry_at: float


def _safe_code(code):
    if code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        raise ValueError("error code must be a safe identifier")
    return code


class Store:
    def __init__(self, connection: sqlite3.Connection, lock_path: Path):
        self.db = connection
        self.lock_path = Path(lock_path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("unsupported database version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL,
                last_error TEXT, last_checked REAL
            );
            CREATE TABLE IF NOT EXISTS work (
                account_id TEXT NOT NULL, mailbox TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', notification TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                last_error TEXT, finished_at REAL, receipt TEXT,
                PRIMARY KEY (account_id, mailbox, uidvalidity, uid)
            );
            CREATE INDEX IF NOT EXISTS work_due ON work(account_id, status, retry_at, uid);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=1;
        """)

    @contextmanager
    def run_lock(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def cursor(self, account_id: str) -> tuple[int, int] | None:
        row = self.db.execute("SELECT uidvalidity, last_uid FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return tuple(row) if row else None

    def initialize(self, account: Account, uidvalidity: int, last_uid: int) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO accounts(account_id, mailbox, uidvalidity, last_uid) VALUES(?,?,?,?)",
                            (account.id, account.mailbox, uidvalidity, last_uid))

    def enqueue(self, account: Account, uidvalidity: int, uids: list[int]) -> None:
        with self.db:
            row = self.db.execute("SELECT * FROM accounts WHERE account_id=?", (account.id,)).fetchone()
            if row is None:
                raise ValueError("account baseline required")
            if row["uidvalidity"] != uidvalidity or row["mailbox"] != account.mailbox:
                raise UIDValidityChanged("uidvalidity_changed")
            new = sorted({uid for uid in uids if uid > row["last_uid"]})
            self.db.executemany("INSERT OR IGNORE INTO work(account_id, mailbox, uidvalidity, uid) VALUES(?,?,?,?)",
                                [(account.id, account.mailbox, uidvalidity, uid) for uid in new])
            if new:
                self.db.execute("UPDATE accounts SET last_uid=? WHERE account_id=?", (new[-1], account.id))

    def due(self, account_id: str, now: float, limit: int) -> list[WorkItem]:
        last_attempted = self.get_meta(f"last_work_uid:{account_id}", 0)
        rows = self.db.execute(f"""SELECT * FROM work WHERE account_id=? AND {_UNFINISHED} AND retry_at<=?
                               ORDER BY CASE WHEN uid > ? THEN 0 ELSE 1 END, uid LIMIT ?""",
                               (account_id, now, last_attempted, limit))
        return [WorkItem((row["account_id"], row["mailbox"], row["uidvalidity"], row["uid"]),
                         row["status"], row["notification"], row["attempts"], row["retry_at"]) for row in rows]

    def mark_attempt(self, key: WorkKey) -> None:
        # Persist the per-account round-robin position before the external call,
        # so a low-UID poison message cannot monopolize every later run.
        self.set_meta(f"last_work_uid:{key[0]}", key[-1])

    def prepare(self, key: WorkKey, notification: str) -> None:
        with self.db:
            self.db.execute(f"UPDATE work SET status='ready', notification=?, attempts=0, retry_at=0, last_error=NULL WHERE {_KEY} AND status='pending'",
                            (notification, *key))

    def finish(self, key: WorkKey, status: str, now: float, receipt: dict | None = None) -> None:
        if status not in {"sent", "ignored", "missing", "superseded"}:
            raise ValueError("invalid completed status")
        with self.db:
            self.db.execute(f"UPDATE work SET status=?, notification=NULL, finished_at=?, receipt=?, last_error=NULL WHERE {_KEY} AND {_UNFINISHED}",
                            (status, now, json.dumps(receipt) if receipt else None, *key))

    def retry(self, key: WorkKey, error_code: str, now: float) -> None:
        _safe_code(error_code)
        with self.db:
            row = self.db.execute(f"SELECT status, attempts FROM work WHERE {_KEY} AND {_UNFINISHED}", key).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + 1
            delay = min(300 * 2 ** min(attempts - 1, 4), 3600) if row["status"] == "ready" else 300
            self.db.execute(f"UPDATE work SET attempts=?, retry_at=?, last_error=? WHERE {_KEY}",
                            (attempts, now + delay, error_code, *key))

    def reset(self, account: Account, uidvalidity: int, last_uid: int, now: float) -> int:
        with self.db:
            count = self.db.execute(f"UPDATE work SET status='superseded', notification=NULL, finished_at=? WHERE account_id=? AND {_UNFINISHED}",
                                    (now, account.id)).rowcount
            self.db.execute("""INSERT INTO accounts(account_id, mailbox, uidvalidity, last_uid, last_checked)
                VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET mailbox=excluded.mailbox,
                uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, last_error=NULL, last_checked=excluded.last_checked""",
                            (account.id, account.mailbox, uidvalidity, last_uid, now))
            return count

    def record_error(self, account_id: str, code: str | None, now: float) -> None:
        with self.db:
            self.db.execute("UPDATE accounts SET last_error=?, last_checked=? WHERE account_id=?", (_safe_code(code), now, account_id))

    def status(self) -> dict:
        accounts = []
        totals = {"pending": 0, "ready": 0, "sent": 0}
        for row in self.db.execute("SELECT * FROM accounts ORDER BY account_id"):
            counts = dict(self.db.execute("SELECT status, count(*) FROM work WHERE account_id=? GROUP BY status", (row["account_id"],)))
            account = dict(row)
            outstanding = self.db.execute(
                f"SELECT last_error FROM work WHERE account_id=? AND {_UNFINISHED} AND last_error IS NOT NULL ORDER BY retry_at DESC LIMIT 1",
                (row["account_id"],),
            ).fetchone()
            if account["last_error"] is None and outstanding:
                account["last_error"] = outstanding[0]
            account["pending"] = counts.get("pending", 0) + counts.get("ready", 0)
            account["ready"] = counts.get("ready", 0)
            account["sent"] = counts.get("sent", 0)
            for name in totals:
                totals[name] += account[name]
            accounts.append(account)
        return {**totals, "accounts": accounts}

    def prune(self, now: float) -> int:
        with self.db:
            return self.db.execute(f"DELETE FROM work WHERE NOT ({_UNFINISHED}) AND finished_at<?", (now - 30 * 86400,)).rowcount

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def close(self) -> None:
        self.db.close()
