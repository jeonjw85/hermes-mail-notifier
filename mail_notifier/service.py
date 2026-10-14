"""Shared scheduled/manual processing flow; one bounded turn per account."""

import sqlite3
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field

from .config import ConfigError, settings_fingerprint
from .hermes_host import HostError, ModelUnavailable
from .imap_client import ImapMailbox, MailboxError
from .pipeline import RunBudget, classify_one, deliver_one, fetch_one
from .store import StorageError, StoreBusy, UIDValidityChanged
from .triage import TriageError


@dataclass
class RunReport:
    processed: int = 0
    sent: int = 0
    ignored: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    already_running: bool = False
    fetched: int = 0
    classified: int = 0
    held: int = 0
    model_calls: int = 0
    duration_seconds: float = 0

    def to_dict(self) -> dict:
        return asdict(self)


def error_code(exc: Exception) -> str:
    if isinstance(exc, (HostError, MailboxError, TriageError, UIDValidityChanged, StorageError)):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return "storage_failed"
    if isinstance(exc, ConfigError):
        return "invalid_settings"
    return "unexpected_error"


class _Run:
    def __init__(self, host, store, settings, report, budget, stack, mailbox_factory, clock):
        self.host, self.store, self.settings, self.report = host, store, settings, report
        self.budget, self.stack, self.factory, self.clock = budget, stack, mailbox_factory, clock
        self.boxes, self.epochs = {}, {}
        self.scanned, self.failed_sources, self.touched = set(), set(), set()
        self.model_down = False
        self.fingerprint = settings_fingerprint(settings)
        self.target = None

    def run(self):
        accounts = {a.id: a for a in self.settings.accounts if a.enabled}
        identifiers = list(accounts)
        stages = ["ready", "fetched", "discovered"]
        next_stage = self.store.get_meta("next_stage")
        offset = stages.index(next_stage) if next_stage in stages else 0
        stages = stages[offset:] + stages[:offset]
        rotation = {}
        for stage in stages:
            next_account = self.store.get_meta(f"next_account:{stage}")
            index = identifiers.index(next_account) if next_account in identifiers else 0
            rotation[stage] = identifiers[index:] + identifiers[:index]
        turns = deque((rotation[stage][index], stage) for index in range(len(identifiers)) for stage in stages)
        idle = 0
        while turns and idle < len(turns) and self.budget.remaining() > 0:
            identifier, stage = turns.popleft()
            turns.append((identifier, stage))
            self.store.set_meta("next_stage", stages[(stages.index(stage) + 1) % len(stages)])
            worked = self.step(accounts[identifier], stage)
            if worked:
                next_index = (identifiers.index(identifier) + 1) % len(identifiers)
                self.store.set_meta(f"next_account:{stage}", identifiers[next_index])
            idle = 0 if worked else idle + 1
        self.report.processed = len(self.touched)
        self.report.held = self.store.status()["held"]

    def remember_error(self, account, exc):
        code = error_code(exc)
        self.report.errors[account.id] = code
        self.store.record_error(account.id, code, self.clock())
        return code

    def complete(self, payload):
        self.budget.consume("model")
        self.report.model_calls += 1
        return self.host.complete_structured(payload, timeout=min(45, self.budget.remaining()))

    def send(self, target, notification):
        return self.host.send(target or self.settings.target, notification, timeout=min(30, self.budget.remaining()))

    def step(self, account, stage):
        if stage == "discovered":
            return self.collect(account)
        lane = "model" if stage == "fetched" else "deliver"
        if not self.budget.available(lane) or (stage == "fetched" and self.model_down):
            return False
        due = self.store.due(account.id, self.clock(), 1, status=stage)
        if not due:
            return False
        item = due[0]
        self.store.mark_attempt(item.key, status=stage)
        self.touched.add(item.key)
        try:
            if stage == "fetched":
                if self.target is None:
                    self.target = self.host.resolve_target(self.settings.target)
                result = classify_one(self.store, item, account, self.settings, self.complete, self.clock(), target=self.target)
                self.report.classified += 1
                self.report.ignored += int(not result.important)
            else:
                self.budget.consume("deliver")
                deliver_one(self.store, item, self.send, self.clock())
                self.report.sent += 1
        except (HostError, TriageError) as exc:
            code = self.remember_error(account, exc)
            if isinstance(exc, ModelUnavailable):
                self.model_down = True
            # classify_one already counted malformed output, including the review fallback.
            if not isinstance(exc, TriageError):
                self.store.retry(item.key, code, self.clock())
        return True

    def collect(self, account):
        if (account.id in self.failed_sources or not self.budget.available("fetch")
                or self.store.cached_count() >= self.settings.max_cached_bodies):
            return False
        hold = self.store.get_meta(f"source_hold:{account.id}")
        if hold and hold.get("fingerprint") == self.fingerprint:
            self.report.errors[account.id] = hold["code"]
            return False
        retry = self.store.get_meta(f"source_retry:{account.id}", {})
        if retry.get("retry_at", 0) > self.clock():
            self.report.errors[account.id] = retry["code"]
            return False
        item, worked = None, False
        try:
            if account.id not in self.boxes:
                mailbox = self.stack.enter_context(self.factory(
                    account, self.host.get_password(account), timeout=min(20, self.budget.remaining()),
                    remaining=self.budget.remaining))
                self.boxes[account.id] = mailbox
                mailbox.set_timeout(min(20, self.budget.remaining()))
                epoch, highest = mailbox.snapshot()
                self.epochs[account.id] = epoch
                cursor = self.store.cursor(account.id)
                if cursor is None:
                    self.store.initialize(account, epoch, highest)
                    self.scanned.add(account.id)
                elif cursor[0] != epoch:
                    raise UIDValidityChanged("uidvalidity_changed")
                self.store.record_error(account.id, None, self.clock())
                if account.id not in self.scanned and self.budget.remaining() > 0:
                    capacity = self.store.capacity(self.settings.max_pending)
                    if capacity:
                        mailbox.set_timeout(min(20, self.budget.remaining()))
                        page = mailbox.discover(self.store.scan_cursor(account.id),
                                                min(capacity, self.settings.max_fetches), highest)
                        self.store.enqueue(account, epoch, page.uids, now=self.clock(),
                                           scanned_through=page.scanned_through, max_pending=self.settings.max_pending)
                    self.scanned.add(account.id)
                worked = True
            if self.budget.remaining() <= 0:
                return worked
            due = self.store.due(account.id, self.clock(), 1, status="discovered")
            if not due:
                self.store.set_meta(f"source_retry:{account.id}", {})
                return worked
            item = due[0]
            if item.key[2] != self.epochs[account.id]:
                raise UIDValidityChanged("uidvalidity_changed")
            self.store.mark_attempt(item.key, status="discovered")
            self.touched.add(item.key)
            self.budget.consume("fetch")
            mailbox = self.boxes[account.id]
            mailbox.set_timeout(min(20, self.budget.remaining()))
            self.report.fetched += int(fetch_one(self.store, item, mailbox, self.clock()))
            self.store.set_meta(f"source_retry:{account.id}", {})
        except (MailboxError, HostError, UIDValidityChanged) as exc:
            code = self.remember_error(account, exc)
            self.failed_sources.add(account.id)
            permanent = code in {"imap_authentication_failed", "imap_mailbox_unavailable", "credential_unavailable"}
            if permanent:
                self.store.set_meta(f"source_hold:{account.id}", {"code": code, "fingerprint": self.fingerprint})
            else:
                attempts = retry.get("attempts", 0) + 1
                self.store.set_meta(f"source_retry:{account.id}", {
                    "code": code, "attempts": attempts,
                    "retry_at": self.clock() + min(300 * 2 ** min(attempts - 1, 4), 3600)})
            if item is not None and not isinstance(exc, UIDValidityChanged):
                self.store.retry(item.key, code, self.clock(), permanent=permanent)
            return True
        return item is not None or worked


def check_mail(host, *, mailbox_factory=ImapMailbox, clock=time.time, monotonic=time.monotonic) -> RunReport:
    report, started, wall_started = RunReport(), monotonic(), clock()
    try:
        with ExitStack() as stack:
            store = host.open_store()
            stack.callback(store.close)
            if not stack.enter_context(store.run_lock()):
                report.already_running = True
                return report
            settings = host.load_settings()
            budget = RunBudget(settings, monotonic, started=started)
            store.prune(clock())
            with ExitStack() as connections:
                run = _Run(host, store, settings, report, budget, connections, mailbox_factory, clock)
                try:
                    run.run()
                except Exception as exc:
                    report.errors["run"] = error_code(exc)
                    report.processed = len(run.touched)
            report.duration_seconds = max(0, monotonic() - started)
            store.record_run(report.to_dict(), wall_started, clock())
    except StoreBusy:
        report.already_running = True
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
        status["limits"] = {name: getattr(settings, name) for name in (
            "max_pending", "max_cached_bodies", "max_fetches", "max_model_calls", "max_deliveries", "max_run_seconds")}
        status["backlog"] = status["pending"] >= settings.max_pending or status["cached_bodies"] >= settings.max_cached_bodies
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
            previous = next((row for row in store.status()["accounts"] if row["account_id"] == account.id), {})
            stages = {name: previous.get(name, 0) for name in ("discovered", "fetched", "ready", "held")}
            count = store.reset(account, epoch, highest, clock())
            return {"account_id": account.id, "uidvalidity": epoch, "last_uid": highest,
                    "superseded": count, "superseded_by_stage": stages}
    finally:
        store.close()
