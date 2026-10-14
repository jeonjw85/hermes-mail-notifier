"""Transactional SQLite schema upgrades with a recoverable pre-upgrade backup."""

import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

SCHEMA_VERSION = 3


def mail_id(key) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))).hex


def _backup(db):
    filename = db.execute("PRAGMA database_list").fetchone()[2]
    if not filename:
        return
    path = Path(filename)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".backup-", dir=path.parent)
    os.close(fd)
    try:
        backup = sqlite3.connect(temporary)
        try:
            db.backup(backup)
        finally:
            backup.close()
        version = db.execute("PRAGMA user_version").fetchone()[0]
        os.replace(temporary, str(path) + f".v{version}.bak")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate(connection: sqlite3.Connection, *, target: str, now: float) -> None:
    db = connection
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version not in (0, 1, 2):
        raise ValueError("unsupported database version")
    if db.in_transaction:
        raise ValueError("migration requires an idle connection")
    if version in (1, 2):
        _backup(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
            last_uid INTEGER NOT NULL, last_error TEXT, last_checked REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS work (
            account_id TEXT NOT NULL, mailbox TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'discovered', notification TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            last_error TEXT, finished_at REAL, receipt TEXT,
            PRIMARY KEY(account_id, mailbox, uidvalidity, uid))""")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        columns = {
            "mail_id": "TEXT", "payload_json": "TEXT", "decision_json": "TEXT", "target": "TEXT",
            "resume_status": "TEXT", "created_at": "REAL NOT NULL DEFAULT 0", "fetched_at": "REAL",
            "queued_at": "REAL", "fetch_attempts": "INTEGER NOT NULL DEFAULT 0",
            "classify_attempts": "INTEGER NOT NULL DEFAULT 0", "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
            "invalid_attempts": "INTEGER NOT NULL DEFAULT 0",
        }
        existing = {row[1] for row in db.execute("PRAGMA table_info(work)")}
        for name, kind in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE work ADD COLUMN {name} {kind}")
        if "scan_uid" not in {row[1] for row in db.execute("PRAGMA table_info(accounts)")}:
            db.execute("ALTER TABLE accounts ADD COLUMN scan_uid INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE accounts SET scan_uid=last_uid")
        for row in db.execute("SELECT account_id, mailbox, uidvalidity, uid FROM work WHERE mail_id IS NULL").fetchall():
            db.execute("""UPDATE work SET mail_id=?, created_at=? WHERE
                account_id=? AND mailbox=? AND uidvalidity=? AND uid=?""", (mail_id(row), now, *row))
        if version < 2:
            db.execute("UPDATE work SET delivery_attempts=attempts, target=?, queued_at=? WHERE status='ready'", (target, now))
        db.execute("UPDATE work SET fetch_attempts=attempts, status='discovered' WHERE status='pending'")
        db.execute("UPDATE work SET status='suppressed' WHERE status='ignored'")
        if version == 1:
            db.execute("UPDATE work SET decision_json=? WHERE status IN ('sent','suppressed','missing','superseded')",
                       (json.dumps({"origin": "legacy"}),))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS work_mail_id ON work(mail_id)")
        db.execute("CREATE INDEX IF NOT EXISTS work_due ON work(account_id, status, retry_at, uid)")
        db.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL NOT NULL, report_json TEXT NOT NULL)""")
        db.execute("""CREATE TABLE feedback (
            id INTEGER PRIMARY KEY, mail_id TEXT NOT NULL UNIQUE, sender_address TEXT NOT NULL,
            subject TEXT NOT NULL, summary TEXT NOT NULL, decision TEXT NOT NULL,
            note TEXT NOT NULL, created_at REAL NOT NULL)""")
        db.execute("PRAGMA user_version=3")
        db.commit()
    except BaseException:
        db.rollback()
        raise
