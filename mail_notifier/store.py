"""Transactional UID checkpoints and a durable notification outbox."""

import fcntl
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Account
from .message import MailContent
from .migrations import SCHEMA_VERSION, mail_id, migrate
from .policy import DECISIONS, POLICY_VERSION, valid_sender

WorkKey = tuple[str, str, int, int]
_KEY = "account_id=? AND mailbox=? AND uidvalidity=? AND uid=?"
_UNFINISHED = "status IN ('discovered', 'fetched', 'ready', 'held')"
_STAGES = {"discovered": "fetch_attempts", "fetched": "classify_attempts", "ready": "delivery_attempts"}


class StorageError(ValueError):
    def __init__(self, code="storage_failed"):
        self.code = code
        super().__init__(code)


class StoreBusy(StorageError):
    def __init__(self):
        super().__init__("already_running")


class UIDValidityChanged(RuntimeError):
    code = "uidvalidity_changed"


@dataclass(frozen=True)
class WorkItem:
    key: WorkKey
    status: str
    notification: str | None
    attempts: int
    retry_at: float
    mail_id: str = ""
    payload: dict | None = None
    decision: dict | None = None
    target: str | None = None
    resume_status: str | None = None
    created_at: float = 0
    last_error: str | None = None
    invalid_attempts: int = 0


def _work_item(row) -> WorkItem:
    return WorkItem(
        (row["account_id"], row["mailbox"], row["uidvalidity"], row["uid"]),
        row["status"], row["notification"], row["attempts"], row["retry_at"], row["mail_id"],
        json.loads(row["payload_json"]) if row["payload_json"] else None,
        json.loads(row["decision_json"]) if row["decision_json"] else None,
        row["target"], row["resume_status"], row["created_at"], row["last_error"], row["invalid_attempts"],
    )


def _safe_code(code):
    if code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        raise ValueError("error code must be a safe identifier")
    return code


class Store:
    def __init__(self, connection: sqlite3.Connection, lock_path: Path, *, migration_target: str | Callable[[], str] = "telegram"):
        self.db = connection
        self.lock_path = Path(lock_path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise ValueError("unsupported database version")
        if version != SCHEMA_VERSION:
            with self.run_lock() as acquired:
                if not acquired:
                    raise StoreBusy()
                target = migration_target
                if callable(target):
                    needs_target = version == 1 and self.db.execute("SELECT 1 FROM work WHERE status='ready' LIMIT 1").fetchone()
                    target = target() if needs_target else "telegram"
                migrate(self.db, target=target, now=time.time())

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

    def scan_cursor(self, account_id: str) -> int:
        row = self.db.execute("SELECT scan_uid FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return row[0] if row else 0

    def capacity(self, maximum: int) -> int:
        count = self.db.execute(f"SELECT count(*) FROM work WHERE {_UNFINISHED}").fetchone()[0]
        return max(0, maximum - count)

    def cached_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM work WHERE payload_json IS NOT NULL").fetchone()[0]

    def initialize(self, account: Account, uidvalidity: int, last_uid: int) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO accounts(account_id, mailbox, uidvalidity, last_uid, scan_uid) VALUES(?,?,?,?,?)",
                            (account.id, account.mailbox, uidvalidity, last_uid, last_uid))

    def enqueue(self, account: Account, uidvalidity: int, uids: list[int], *, now=0,
                scanned_through=None, max_pending=2000) -> int:
        with self.db:
            row = self.db.execute("SELECT * FROM accounts WHERE account_id=?", (account.id,)).fetchone()
            if row is None:
                raise ValueError("account baseline required")
            if row["uidvalidity"] != uidvalidity or row["mailbox"] != account.mailbox:
                raise UIDValidityChanged("uidvalidity_changed")
            candidates = sorted({uid for uid in uids if uid > row["last_uid"]})
            new = candidates[:self.capacity(max_pending)]
            self.db.executemany("""INSERT OR IGNORE INTO work(
                account_id, mailbox, uidvalidity, uid, mail_id, status, created_at)
                VALUES(?,?,?,?,?,'discovered',?)""",
                [(account.id, account.mailbox, uidvalidity, uid,
                  mail_id((account.id, account.mailbox, uidvalidity, uid)), now) for uid in new])
            if new:
                self.db.execute("UPDATE accounts SET last_uid=? WHERE account_id=?", (new[-1], account.id))
            progress = new[-1] if new else row["scan_uid"]
            if len(new) == len(candidates) and scanned_through is not None:
                progress = max(progress, scanned_through)
            self.db.execute("UPDATE accounts SET scan_uid=max(scan_uid,?) WHERE account_id=?", (progress, account.id))
            return len(new)

    def due(self, account_id: str, now: float, limit: int, *, status=None) -> list[WorkItem]:
        if status is not None and status not in _STAGES:
            raise ValueError("invalid queue stage")
        last_attempted = self.get_meta(f"last_work_uid:{account_id}:{status or 'all'}", 0)
        condition = "status=?" if status else "status IN ('discovered','fetched','ready')"
        params = (account_id, status, now, last_attempted, limit) if status else (account_id, now, last_attempted, limit)
        priority = ""
        if status == "fetched":
            prefer_new = int(bool(self.get_meta(f"prefer_new:{account_id}", True)))
            priority = "CASE WHEN (attempts=0)=? THEN 0 ELSE 1 END, "
            params = (account_id, status, now, prefer_new, last_attempted, limit)
        rows = self.db.execute(f"""SELECT * FROM work WHERE account_id=? AND {condition} AND retry_at<=?
                               ORDER BY {priority}CASE WHEN uid > ? THEN 0 ELSE 1 END, uid LIMIT ?""",
                               params)
        return [_work_item(row) for row in rows]

    def mark_attempt(self, key: WorkKey, *, status=None) -> None:
        # Persist the per-account round-robin position before the external call,
        # so a low-UID poison message cannot monopolize every later run.
        self.set_meta(f"last_work_uid:{key[0]}:{status or 'all'}", key[-1])
        if status == "fetched":
            row = self.db.execute(f"SELECT attempts FROM work WHERE {_KEY}", key).fetchone()
            if row:
                self.set_meta(f"prefer_new:{key[0]}", row[0] != 0)

    def get_work(self, identifier: str) -> WorkItem | None:
        row = self.db.execute("SELECT * FROM work WHERE mail_id=?", (identifier,)).fetchone()
        return _work_item(row) if row else None

    def cache_message(self, key: WorkKey, mail: MailContent, now: float) -> None:
        with self.db:
            self.db.execute(f"""UPDATE work SET status='fetched', payload_json=?, fetched_at=?,
                attempts=classify_attempts, retry_at=0, last_error=NULL WHERE {_KEY} AND status='discovered'""",
                (json.dumps(asdict(mail), ensure_ascii=False), now, *key))

    def save_decision(self, key: WorkKey, decision: dict, *, notification: str | None,
                      target: str, fingerprint: str, now: float) -> None:
        suppressed = decision["decision"] == "suppress"
        if not suppressed and not notification:
            raise ValueError("notification required")
        record = {**decision, "policy_version": POLICY_VERSION, "settings_fingerprint": fingerprint,
                  "decided_at": now}
        with self.db:
            row = self.db.execute(f"SELECT payload_json FROM work WHERE {_KEY} AND status='fetched'", key).fetchone()
            if row is None:
                raise StorageError("invalid_work_transition")
            payload = json.loads(row[0])
            record["mail"] = {name: payload.get(name, "") for name in ("sender_address", "sender_name", "subject", "date", "message_id")}
            self.db.execute(f"""UPDATE work SET status=?, decision_json=?, payload_json=NULL,
                notification=?, target=?, queued_at=?, finished_at=?, attempts=0, retry_at=0, last_error=NULL
                WHERE {_KEY} AND status='fetched'""",
                ("suppressed" if suppressed else "ready", json.dumps(record, ensure_ascii=False),
                 notification, target, None if suppressed else now, now if suppressed else None, *key))

    def prepare(self, key: WorkKey, notification: str) -> None:
        with self.db:
            self.db.execute(f"UPDATE work SET status='ready', notification=?, attempts=0, retry_at=0, last_error=NULL WHERE {_KEY} AND status='discovered'",
                            (notification, *key))

    def finish(self, key: WorkKey, status: str, now: float, receipt: dict | None = None) -> None:
        status = "suppressed" if status == "ignored" else status
        if status not in {"sent", "suppressed", "missing", "superseded"}:
            raise ValueError("invalid completed status")
        with self.db:
            self.db.execute(f"UPDATE work SET status=?, notification=NULL, payload_json=NULL, finished_at=?, receipt=?, last_error=NULL WHERE {_KEY} AND {_UNFINISHED}",
                            (status, now, json.dumps(receipt) if receipt else None, *key))

    def retry(self, key: WorkKey, error_code: str, now: float, *, permanent=False) -> None:
        _safe_code(error_code)
        with self.db:
            row = self.db.execute(f"SELECT status, attempts FROM work WHERE {_KEY} AND {_UNFINISHED}", key).fetchone()
            if row is None or row["status"] == "held":
                return
            attempts = row["attempts"] + 1
            delay = min(300 * 2 ** min(attempts - 1, 4), 3600)
            held = permanent or (row["status"] == "ready" and attempts >= 10)
            counter = _STAGES[row["status"]]
            self.db.execute(f"""UPDATE work SET attempts=?, {counter}={counter}+1,
                invalid_attempts=invalid_attempts+?, retry_at=?,
                last_error=?, status=?, resume_status=? WHERE {_KEY}""",
                (attempts, int(error_code == "invalid_model_output"), now + delay, error_code, "held" if held else row["status"],
                 row["status"] if held else None, *key))

    def resume(self, identifier: str, now: float) -> bool:
        with self.db:
            row = self.db.execute("SELECT status,resume_status FROM work WHERE mail_id=?", (identifier,)).fetchone()
            if row is None or row["status"] not in (*_STAGES, "held"):
                return False
            status = row["resume_status"] if row["status"] == "held" else row["status"]
            counter = _STAGES[status]
            self.db.execute(f"""UPDATE work SET status=?, resume_status=NULL, attempts=0, {counter}=0,
                retry_at=?, invalid_attempts=0, last_error=NULL WHERE mail_id=?""", (status, now, identifier))
            return True

    def reset(self, account: Account, uidvalidity: int, last_uid: int, now: float) -> int:
        with self.db:
            count = self.db.execute(f"UPDATE work SET status='superseded', notification=NULL, payload_json=NULL, finished_at=? WHERE account_id=? AND {_UNFINISHED}",
                                     (now, account.id)).rowcount
            self.db.execute("""INSERT INTO accounts(account_id, mailbox, uidvalidity, last_uid, scan_uid, last_checked)
                VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET mailbox=excluded.mailbox,
                uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, scan_uid=excluded.scan_uid,
                last_error=NULL, last_checked=excluded.last_checked""",
                            (account.id, account.mailbox, uidvalidity, last_uid, last_uid, now))
            self.db.execute("DELETE FROM meta WHERE key=?", (f"source_hold:{account.id}",))
            self.db.execute("DELETE FROM meta WHERE key=?", (f"source_retry:{account.id}",))
            return count

    def resume_source(self, account_id: str, now: float) -> None:
        with self.db:
            self.db.execute("DELETE FROM meta WHERE key=?", (f"source_hold:{account_id}",))
            self.db.execute("DELETE FROM meta WHERE key=?", (f"source_retry:{account_id}",))
            self.db.execute("""UPDATE work SET status='discovered', resume_status=NULL, attempts=0,
                fetch_attempts=0, retry_at=?, last_error=NULL
                WHERE account_id=? AND status='held' AND resume_status='discovered'""", (now, account_id))

    def record_error(self, account_id: str, code: str | None, now: float) -> None:
        with self.db:
            self.db.execute("UPDATE accounts SET last_error=?, last_checked=? WHERE account_id=?", (_safe_code(code), now, account_id))

    def status(self) -> dict:
        accounts = []
        totals = dict.fromkeys(("pending", "discovered", "fetched", "ready", "held", "sent", "suppressed"), 0)
        for row in self.db.execute("SELECT * FROM accounts ORDER BY account_id"):
            counts = dict(self.db.execute("SELECT status, count(*) FROM work WHERE account_id=? GROUP BY status", (row["account_id"],)))
            account = dict(row)
            account["source_hold"] = self.get_meta(f"source_hold:{row['account_id']}")
            account["source_retry"] = self.get_meta(f"source_retry:{row['account_id']}", {})
            outstanding = self.db.execute(
                f"SELECT last_error FROM work WHERE account_id=? AND {_UNFINISHED} AND last_error IS NOT NULL ORDER BY retry_at DESC LIMIT 1",
                (row["account_id"],),
            ).fetchone()
            if account["last_error"] is None and outstanding:
                account["last_error"] = outstanding[0]
            account["pending"] = sum(counts.get(name, 0) for name in ("discovered", "fetched", "ready", "held"))
            account.update({name: counts.get(name, 0) for name in totals if name != "pending"})
            account["oldest_pending_at"] = self.db.execute(
                f"SELECT min(created_at) FROM work WHERE account_id=? AND {_UNFINISHED}", (row["account_id"],)).fetchone()[0]
            for name in totals:
                totals[name] += account[name]
            accounts.append(account)
        last_run = self.db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        run = {"started_at": last_run["started_at"], "finished_at": last_run["finished_at"],
               "report": json.loads(last_run["report_json"])} if last_run else None
        usage = dict(self.db.execute("""SELECT
            sum(json_extract(decision_json,'$.usage.total_tokens')) AS total_tokens,
            sum(json_extract(decision_json,'$.usage.cost_usd')) AS known_cost_usd,
            count(*)-count(json_extract(decision_json,'$.usage.cost_usd')) AS unknown_cost_records
            FROM work WHERE decision_json IS NOT NULL AND (json_extract(decision_json,'$.origin') IS NOT 'user_rule'
                OR json_extract(decision_json,'$.usage.total_tokens') IS NOT NULL)""").fetchone())
        usage["cost_usd"] = usage["known_cost_usd"] if usage["unknown_cost_records"] == 0 else None
        return {**totals, "accounts": accounts, "last_run": run, "schema_version": SCHEMA_VERSION,
                "policy_version": POLICY_VERSION, "cached_bodies": self.cached_count(), "usage": usage}

    def history(self, limit=20, *, account_id=None, identifier=None) -> list[dict]:
        filters, params = [], []
        for column, value in (("account_id", account_id), ("mail_id", identifier)):
            if value is not None:
                filters.append(f"{column}=?")
                params.append(value)
        condition = "WHERE " + " AND ".join(filters) if filters else ""
        rows = self.db.execute(f"""SELECT mail_id,account_id,status,decision_json,finished_at,receipt,last_error
            FROM work {condition} ORDER BY created_at DESC, uid DESC LIMIT ?""", [*params, max(1, min(limit, 1000))])
        return [{"mail_id": row["mail_id"], "account_id": row["account_id"], "status": row["status"],
                 "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
                 "finished_at": row["finished_at"], "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
                 "last_error": row["last_error"]} for row in rows]

    def queue_items(self, *, account_id=None) -> list[dict]:
        condition, params = (" AND account_id=?", (account_id,)) if account_id else ("", ())
        return [dict(row) for row in self.db.execute(f"""SELECT mail_id,account_id,status,resume_status,
            target,attempts,retry_at,last_error,created_at FROM work WHERE {_UNFINISHED}{condition}
            ORDER BY created_at,uid""", params)]

    def retarget(self, identifier: str, target: str) -> bool:
        with self.db:
            return self.db.execute("""UPDATE work SET target=? WHERE mail_id=? AND
                (status='ready' OR (status='held' AND resume_status='ready'))""", (target, identifier)).rowcount > 0

    def record_run(self, report: dict, started_at: float, finished_at: float) -> None:
        with self.db:
            self.db.execute("INSERT INTO runs(started_at,finished_at,report_json) VALUES(?,?,?)",
                            (started_at, finished_at, json.dumps(report)))

    def add_feedback(self, identifier: str, decision: str, note: str, now: float) -> dict:
        if decision not in DECISIONS or not isinstance(note, str) or len(note) > 1000 or "\x00" in note:
            raise StorageError("invalid_feedback")
        item = self.get_work(identifier)
        if item is None or not item.decision or "mail" not in item.decision:
            raise StorageError("decision_not_found")
        mail = item.decision["mail"]
        with self.db:
            self.db.execute("""INSERT INTO feedback(mail_id,sender_address,subject,summary,decision,note,created_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(mail_id) DO UPDATE SET
                decision=excluded.decision,note=excluded.note,created_at=excluded.created_at""",
                (identifier, mail["sender_address"].casefold(), mail["subject"][:420],
                 item.decision.get("summary", "")[:1200], decision, note, now))
            self.db.execute("DELETE FROM feedback WHERE created_at<?", (now - 180 * 86400,))
            self.db.execute("DELETE FROM feedback WHERE id NOT IN (SELECT id FROM feedback ORDER BY created_at DESC,id DESC LIMIT 200)")
            return dict(self.db.execute("SELECT * FROM feedback WHERE mail_id=?", (identifier,)).fetchone())

    def list_feedback(self, limit=20) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM feedback ORDER BY created_at DESC,id DESC LIMIT ?", (max(1, min(limit, 200)),))]

    def feedback_for(self, sender: str) -> list[dict]:
        if not valid_sender(sender):
            return []
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM feedback WHERE sender_address=? ORDER BY created_at DESC,id DESC LIMIT 5", (sender.casefold(),))]

    def remove_feedback(self, feedback_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM feedback WHERE id=?", (feedback_id,)).rowcount > 0

    def prune(self, now: float) -> int:
        with self.db:
            self.db.execute("""UPDATE work SET payload_json=NULL,
                status=CASE WHEN status='held' THEN 'held' ELSE 'discovered' END,
                resume_status=CASE WHEN status='held' THEN 'discovered' ELSE NULL END
                WHERE payload_json IS NOT NULL AND fetched_at<? AND
                (status='fetched' OR (status='held' AND resume_status='fetched'))""", (now - 7 * 86400,))
            self.db.execute("DELETE FROM runs WHERE finished_at<?", (now - 30 * 86400,))
            self.db.execute("DELETE FROM feedback WHERE created_at<?", (now - 180 * 86400,))
            return self.db.execute(f"DELETE FROM work WHERE NOT ({_UNFINISHED}) AND finished_at<?", (now - 30 * 86400,)).rowcount

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def close(self) -> None:
        self.db.close()
