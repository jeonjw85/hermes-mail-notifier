import fcntl
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from mail_notifier.store import Store


def create_v1(db):
    db.executescript("""
        CREATE TABLE accounts(account_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL, last_error TEXT, last_checked REAL);
        CREATE TABLE work(account_id TEXT NOT NULL, mailbox TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', notification TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            last_error TEXT, finished_at REAL, receipt TEXT,
            PRIMARY KEY(account_id, mailbox, uidvalidity, uid));
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO accounts VALUES('a1','INBOX',7,103,NULL,1000);
        INSERT INTO work(account_id,mailbox,uidvalidity,uid,status,notification,attempts,retry_at)
            VALUES('a1','INBOX',7,101,'ready','saved summary',2,1600);
        INSERT INTO work(account_id,mailbox,uidvalidity,uid,status) VALUES('a1','INBOX',7,102,'pending');
        INSERT INTO work(account_id,mailbox,uidvalidity,uid,status,finished_at) VALUES('a1','INBOX',7,103,'ignored',900);
        INSERT INTO meta VALUES('cron_job_id','"job-1"');
        PRAGMA user_version=1;
    """)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"
        self.lock = self.path.with_suffix(".lock")
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)

    def test_v1_migration_preserves_ready_retry_cursor_and_cron(self):
        create_v1(self.db)
        store = Store(self.db, self.lock)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)
        item = self.db.execute("SELECT * FROM work WHERE uid=101").fetchone()
        self.assertEqual((item["notification"], item["attempts"], item["retry_at"], item["target"]),
                         ("saved summary", 2, 1600, "telegram"))
        self.assertEqual([r[0] for r in self.db.execute("SELECT status FROM work ORDER BY uid")],
                         ["ready", "discovered", "suppressed"])
        self.assertEqual(store.cursor("a1"), (7, 103))
        self.assertEqual(store.get_meta("cron_job_id"), "job-1")
        self.assertEqual(len({r[0] for r in self.db.execute("SELECT mail_id FROM work")}), 3)
        backup = Path(str(self.path) + ".v1.bak")
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_failed_migration_rolls_back_and_reopens_v1(self):
        create_v1(self.db)
        self.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            Store(self.db, self.lock)
        self.db.set_authorizer(None)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT notification FROM work WHERE uid=101").fetchone()[0], "saved summary")
        Store(self.db, self.lock)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_future_schema_is_rejected(self):
        self.db.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError):
            Store(self.db, self.lock)

    def test_reopen_v2_does_not_rewrite_schema(self):
        Store(self.db, self.lock)
        statements = []
        self.db.set_trace_callback(statements.append)
        Store(self.db, self.lock)
        self.assertFalse(any(s.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE")) for s in statements))

    def test_migration_respects_other_process_lock(self):
        create_v1(self.db)
        with self.lock.open("a") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Exception) as caught:
                Store(self.db, self.lock)
            self.assertEqual(getattr(caught.exception, "code", None), "already_running")
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_migration_snapshots_configured_target_once(self):
        create_v1(self.db)
        Store(self.db, self.lock, migration_target="telegram:123")
        Store(self.db, self.lock, migration_target="telegram:456")
        self.assertEqual(self.db.execute("SELECT target FROM work WHERE uid=101").fetchone()[0], "telegram:123")

    def test_v2_upgrade_preserves_ready_snapshot_and_backs_up_v2(self):
        create_v1(self.db)
        self.db.executescript("""
            ALTER TABLE work ADD COLUMN target TEXT;
            ALTER TABLE work ADD COLUMN mail_id TEXT;
            ALTER TABLE work ADD COLUMN payload_json TEXT;
            ALTER TABLE work ADD COLUMN decision_json TEXT;
            ALTER TABLE work ADD COLUMN resume_status TEXT;
            ALTER TABLE work ADD COLUMN created_at REAL DEFAULT 1000;
            ALTER TABLE work ADD COLUMN fetched_at REAL;
            ALTER TABLE work ADD COLUMN queued_at REAL;
            ALTER TABLE work ADD COLUMN fetch_attempts INTEGER DEFAULT 0;
            ALTER TABLE work ADD COLUMN classify_attempts INTEGER DEFAULT 0;
            ALTER TABLE work ADD COLUMN delivery_attempts INTEGER DEFAULT 0;
            ALTER TABLE work ADD COLUMN invalid_attempts INTEGER DEFAULT 0;
            ALTER TABLE accounts ADD COLUMN scan_uid INTEGER DEFAULT 103;
            UPDATE work SET mail_id='mail-' || uid;
            UPDATE work SET target='telegram:111' WHERE status='ready';
            UPDATE work SET status='discovered' WHERE status='pending';
            PRAGMA user_version=2;
        """)
        store = Store(self.db, self.lock, migration_target="telegram:222")
        self.assertEqual(store.due("a1", 2000, 1, status="ready")[0].target, "telegram:111")
        self.assertEqual(store.list_feedback(), [])
        with closing(sqlite3.connect(str(self.path) + ".v2.bak")) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
