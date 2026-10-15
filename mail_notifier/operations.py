"""Profile-scoped user operations shared by terminal and Hermes tools."""

import time
from contextlib import contextmanager
from dataclasses import replace

from .config import ConfigError, Settings
from .hermes_host import HostError
from .imap_client import ImapMailbox
from .policy import INSTRUCTIONS, POLICY_VERSION
from .triage import _matches

POLICY_FIELDS = ("importance_instructions", "important_senders", "ignored_senders",
                 "important_keywords", "always_notify_senders", "language")


@contextmanager
def _store(host, *, write=False):
    store = host.open_store()
    try:
        if write:
            with store.run_lock() as acquired:
                if not acquired:
                    raise HostError("already_running")
                yield store
        else:
            yield store
    finally:
        store.close()


def _account(host, identifier):
    if not isinstance(identifier, str) or not identifier:
        raise HostError("account_not_found")
    account = next((a for a in host.load_settings().accounts if a.id == identifier), None)
    if account is None:
        raise HostError("account_not_found")
    return account


def _limit(value):
    if type(value) is not int or not 1 <= value <= 200:
        raise HostError("invalid_limit")
    return value


def history(host, *, mail_id=None, account_id=None, limit=20):
    _limit(limit)
    if account_id is not None:
        _account(host, account_id)
    with _store(host) as store:
        items = store.history(limit, account_id=account_id, identifier=mail_id)
        if mail_id is not None and not items:
            raise HostError("mail_not_found")
        return {"items": items}


def queue(host, *, action="list", mail_id=None, account_id=None, target=None, limit=20):
    _limit(limit)
    if action not in ("list", "retry", "retarget") or (target is not None and action != "retarget"):
        raise HostError("invalid_queue_action")
    if account_id is not None:
        _account(host, account_id)
    if action != "list" and not mail_id and not account_id:
        raise HostError("queue_scope_required")
    if action == "retarget":
        if not isinstance(target, str):
            raise HostError("invalid_target")
        try:
            Settings(target=target)
        except ConfigError:
            raise HostError("invalid_target") from None
    with _store(host, write=action != "list") as store:
        if action == "retry":
            store.prune(time.time())
        items = store.queue_items(account_id=account_id)
        if mail_id is not None:
            items = [row for row in items if row["mail_id"] == mail_id]
            if not items:
                raise HostError("pending_mail_not_found")
        if action == "list":
            return {"items": items[:limit], "total": len(items)}
        if action == "retarget":
            ready = [row for row in items if row["status"] == "ready" or row["resume_status"] == "ready"]
            if mail_id and not ready:
                raise HostError("notification_not_ready")
            target = host.resolve_target(target)
            for row in ready:
                store.retarget(row["mail_id"], target)
            return {"changed": len(ready), "target": target}
        for row in items:
            store.resume(row["mail_id"], time.time())
        source_accounts = {row["account_id"] for row in items
                           if row["status"] == "discovered" or row["resume_status"] == "discovered"}
        if account_id:
            source_accounts.add(account_id)
        for identifier in source_accounts:
            # Clear connection backoff without widening a single-mail selection.
            store.set_meta(f"source_hold:{identifier}", None)
            store.set_meta(f"source_retry:{identifier}", {})
        return {"changed": len(items)}


def feedback(host, *, action, mail_id=None, feedback_id=None, decision=None, note=""):
    if action not in ("add", "list", "remove"):
        raise HostError("invalid_feedback_action")
    if action == "add" and (not isinstance(mail_id, str) or not mail_id or feedback_id is not None):
        raise HostError("invalid_feedback")
    if action == "remove" and (type(feedback_id) is not int or feedback_id < 1):
        raise HostError("invalid_feedback")
    if action != "add" and (mail_id is not None or decision is not None or note):
        raise HostError("invalid_feedback")
    with _store(host, write=action != "list") as store:
        if action == "list":
            return {"items": store.list_feedback(200)}
        if action == "add":
            return {"item": store.add_feedback(mail_id, decision, note, time.time())}
        if not store.remove_feedback(feedback_id):
            raise HostError("feedback_not_found")
        return {"removed": feedback_id}


def policy(host, *, changes=None):
    with _store(host, write=changes is not None):
        settings = host.load_settings()
        if changes is not None:
            if not isinstance(changes, dict) or not changes or set(changes) - set(POLICY_FIELDS):
                raise HostError("invalid_policy_changes")
            values = dict(changes)
            for name in ("important_senders", "ignored_senders", "important_keywords", "always_notify_senders"):
                if name in values:
                    if not isinstance(values[name], (tuple, list)):
                        raise HostError("invalid_policy_changes")
                    values[name] = tuple(values[name])
            settings = replace(settings, **values)
            host.save_settings(settings)
        conflicts = [sender for sender in settings.always_notify_senders
                     if sender in settings.ignored_senders or _matches(sender, settings.ignored_senders)]
        return {"policy_version": POLICY_VERSION, "default_instructions": INSTRUCTIONS,
                "settings": {name: getattr(settings, name) for name in POLICY_FIELDS},
                "conflicts": conflicts, "conflict_resolution": "ignored_senders wins"}


def set_account_enabled(host, account_id, enabled, *, mailbox_factory=ImapMailbox):
    if type(enabled) is not bool:
        raise HostError("invalid_enabled")
    from .onboarding import OnboardingService
    from .onboarding_auth import SetupOwner
    from .i18n import resolve_ui_language
    _account(host, account_id)
    service = OnboardingService(host, mailbox_factory=mailbox_factory)
    owner = SetupOwner(str(host.home()), "cli")
    draft = service.start(owner)
    # This existing explicit command is the confirmation. Never adopt and
    # publish unrelated inputs left by an interactive setup session.
    expected = replace(draft.original, ui_language=resolve_ui_language(saved=draft.original.ui_language))
    if draft.form or draft.candidate != expected or draft.state != "draft":
        raise HostError("setup_changed_retry")
    draft = service.change(draft.id, owner, draft.revision, action="enable" if enabled else "disable",
                           values={"account_id": account_id})
    if enabled:
        draft = service.validate_password(draft.id, owner, draft.revision, "")
        if draft.state != "awaiting_confirmation":
            code = draft.steps.get("error_code", "credential_unavailable")
            service.cancel(draft.id, owner, draft.revision)
            raise HostError(code)
    result = service.commit(draft.id, owner, draft.revision)
    if result["state"] not in {"completed", "scheduled"}:
        raise HostError(result.get("error", "setup_repair_required"))
    return {"account_id": account_id, "enabled": enabled, "cron_job_id": result.get("cron_job_id", "")}
