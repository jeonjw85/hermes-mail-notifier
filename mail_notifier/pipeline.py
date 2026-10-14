"""Bounded, independently retryable operations on durable mail jobs."""

import time
from dataclasses import asdict

from .config import settings_fingerprint
from .message import MailContent
from .render import render_notification
from .triage import TriageError, review_result, triage_message


class RunBudget:
    def __init__(self, settings, monotonic=time.monotonic, *, started=None):
        self.monotonic = monotonic
        self.started = monotonic() if started is None else started
        self.seconds = settings.max_run_seconds
        self.limits = {"fetch": settings.max_fetches, "model": settings.max_model_calls,
                       "deliver": settings.max_deliveries}
        self.used = dict.fromkeys(self.limits, 0)

    def remaining(self) -> float:
        return max(0, self.seconds - (self.monotonic() - self.started))

    def available(self, stage: str) -> bool:
        return self.used[stage] < self.limits[stage] and self.remaining() > 0

    def consume(self, stage: str) -> None:
        self.used[stage] += 1


def fetch_one(store, item, mailbox, now):
    mail = mailbox.fetch(item.key[-1])
    if mail is None:
        store.finish(item.key, "missing", now)
        return False
    store.cache_message(item.key, mail, now)
    return True


def classify_one(store, item, account, settings, complete, now, *, target=None):
    mail = MailContent(**item.payload)
    try:
        result = triage_message(mail, settings, complete, account_label=account.label,
                                feedback=store.feedback_for(mail.sender_address))
    except TriageError as exc:
        store.retry(item.key, exc.code, now)
        if store.get_work(item.mail_id).invalid_attempts < 5:
            raise
        result = review_result(mail, "AI 판단 응답을 5회 검증하지 못했습니다. 원문 확인이 필요합니다.")
    notification = render_notification(account, mail, result, mail_id=item.mail_id) if result.important else None
    store.save_decision(item.key, asdict(result), notification=notification, target=target or settings.target,
                        fingerprint=settings_fingerprint(settings), now=now)
    return result


def deliver_one(store, item, send, now):
    receipt = send(item.target, item.notification)
    store.finish(item.key, "sent", now, receipt)
