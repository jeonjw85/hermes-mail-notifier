"""Shared scheduled/manual processing flow; one bounded turn per account."""

import sqlite3
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field

from .config import MAX_MESSAGES, MAX_RUN_SECONDS, ConfigError
from .hermes_host import HostError, ModelUnavailable
from .imap_client import ImapMailbox, MailboxError
from .render import render_notification
from .store import UIDValidityChanged
from .triage import TriageError, triage_message


@dataclass
class RunReport:
    processed: int = 0
    sent: int = 0
    ignored: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    already_running: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def error_code(exc: Exception) -> str:
    if isinstance(exc, (HostError, MailboxError, TriageError, UIDValidityChanged)):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return "storage_failed"
    if isinstance(exc, ConfigError):
        return "invalid_settings"
    return "unexpected_error"


def check_mail(host, *, mailbox_factory=ImapMailbox, clock=time.time, monotonic=time.monotonic) -> RunReport:
    report = RunReport()
    started = monotonic()
    expired = lambda: monotonic() - started >= MAX_RUN_SECONDS
    try:
        with ExitStack() as stack:
            store = host.open_store()
            stack.callback(store.close)
            if not stack.enter_context(store.run_lock()):
                report.already_running = True
                return report
            settings = host.load_settings()
            accounts = [account for account in settings.accounts if account.enabled]
            ids = [account.id for account in accounts]
            next_id = store.get_meta("next_account_id")
            offset = ids.index(next_id) if next_id in ids else 0
            turns = deque(accounts[offset:] + accounts[:offset])
            queues, mailboxes = {}, {}
            model_unavailable = False
            while turns and report.processed < MAX_MESSAGES and not expired():
                account = turns.popleft()
                store.set_meta("next_account_id", ids[(ids.index(account.id) + 1) % len(ids)])
                if account.id not in mailboxes:
                    try:
                        mailbox = stack.enter_context(mailbox_factory(account, host.get_password(account)))
                        epoch, highest = mailbox.snapshot()
                        cursor = store.cursor(account.id)
                        if cursor is None:
                            store.initialize(account, epoch, highest)
                            store.record_error(account.id, None, clock())
                            continue
                        if cursor[0] != epoch:
                            raise UIDValidityChanged("uidvalidity_changed")
                        if expired():
                            break
                        store.enqueue(account, epoch, mailbox.new_uids(cursor[1], MAX_MESSAGES))
                        store.record_error(account.id, None, clock())
                        queues[account.id] = deque(store.due(account.id, clock(), MAX_MESSAGES))
                        mailboxes[account.id] = mailbox
                    except (MailboxError, HostError, UIDValidityChanged) as exc:
                        code = error_code(exc)
                        report.errors[account.id] = code
                        store.record_error(account.id, code, clock())
                        continue
                queue = queues[account.id]
                if not queue:
                    continue
                item = queue.popleft()
                if queue:
                    turns.append(account)
                if expired():
                    break
                if model_unavailable and item.status == "pending":
                    continue
                store.mark_attempt(item.key)
                report.processed += 1
                try:
                    notification = item.notification
                    if item.status == "pending":
                        mail = mailboxes[account.id].fetch(item.key[-1])
                        if mail is None:
                            store.finish(item.key, "missing", clock())
                            continue
                        if expired():
                            break
                        result = triage_message(mail, settings, host.complete_structured)
                        if not result.important:
                            store.finish(item.key, "ignored", clock())
                            report.ignored += 1
                            continue
                        notification = render_notification(account, mail, result)
                        store.prepare(item.key, notification)
                    if expired():
                        break
                    receipt = host.send(settings.target, notification)
                    store.finish(item.key, "sent", clock(), receipt)
                    report.sent += 1
                except (MailboxError, HostError, TriageError) as exc:
                    code = error_code(exc)
                    if isinstance(exc, ModelUnavailable):
                        model_unavailable = True
                    report.errors[account.id] = code
                    store.retry(item.key, code, clock())
                    store.record_error(account.id, code, clock())
                    if isinstance(exc, MailboxError):
                        # A dropped connection cannot be reused for this account's other UIDs.
                        queues[account.id].clear()
            store.prune(clock())
    except Exception as exc:
        report.errors["run"] = error_code(exc)
    return report


def get_status(host) -> dict:
    settings = host.load_settings()
    store = host.open_store()
    try:
        status = store.status()
        stored = {entry["account_id"]: entry for entry in status["accounts"]}
        status["accounts"] = [
            {**stored.get(account.id, {"pending": 0, "ready": 0, "sent": 0, "last_error": None}),
             "account_id": account.id, "label": account.label, "provider": account.provider,
             "enabled": account.enabled, "mailbox": account.mailbox}
            for account in settings.accounts
        ]
        status.update(target=settings.target, schedule=settings.schedule, language=settings.language)
        job_id = store.get_meta("cron_job_id")
        status["cron"] = {"job_id": job_id, "state": "not_configured"}
        if job_id:
            try:
                jobs = host.manage_cron("list", include_disabled=True).get("jobs", [])
                job = next((job for job in jobs if job.get("job_id") == job_id), None)
                status["cron"]["state"] = (job.get("state") or ("active" if job.get("enabled", True) else "paused")) if job else "missing"
                if job:
                    status["cron"]["last_run_at"] = job.get("last_run_at")
                    status["cron"]["next_run_at"] = job.get("next_run_at")
            except HostError as exc:
                status["cron"]["error"] = exc.code
        return status
    finally:
        store.close()


def reset_account(host, account_id: str, *, mailbox_factory=ImapMailbox, clock=time.time) -> dict:
    store = host.open_store()
    try:
        with store.run_lock() as acquired:
            if not acquired:
                raise HostError("already_running")
            account = next((a for a in host.load_settings().accounts if a.id == account_id), None)
            if account is None:
                raise HostError("account_not_found")
            with mailbox_factory(account, host.get_password(account)) as mailbox:
                epoch, highest = mailbox.snapshot()
            count = store.reset(account, epoch, highest, clock())
            return {"account_id": account.id, "uidvalidity": epoch, "last_uid": highest, "superseded": count}
    finally:
        store.close()
