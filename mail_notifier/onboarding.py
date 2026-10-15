"""Shared, explicitly confirmed setup with non-secret, durable recovery markers.

External stores are not one transaction. Record intent before credential writes,
and evidence after each step; an interrupted password replacement needs input,
not a guess based on whichever password happens to be readable afterwards.
"""

import copy
import re
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field, replace

from .config import Account, secret_name, settings_fingerprint
from .hermes_host import HostError
from .i18n import resolve_ui_language, text
from .imap_client import ImapMailbox
from .message import MailContent
from .onboarding_auth import SetupOwner, owner_key
from .onboarding_store import SetupDraft
from .service import error_code
from .triage import triage_message

_TERMINAL = {"completed", "cancelled", "expired", "acknowledged"}
_ACCOUNT_FIELDS = {"provider", "username", "label", "mailbox", "enabled"}
_SETTINGS_FIELDS = {"schedule", "target", "language", "ui_language", "important_senders", "ignored_senders",
                    "always_notify_senders", "important_keywords", "importance_instructions"}
_ERRORS = {"imap_authentication_failed", "imap_connection_failed", "imap_timeout", "imap_mailbox_unavailable",
           "imap_invalid_uid_state", "credential_unavailable", "credential_save_failed", "settings_save_failed",
           "credentials_not_reloadable", "credential_reload_check_failed", "destination_unavailable",
           "incompatible_hermes", "hermes_executable_missing", "invalid_schedule", "invalid_settings",
           "model_unavailable", "invalid_model_output", "delivery_failed", "delivery_unverified",
           "cron_operation_failed", "cron_ownership_failed", "invalid_cron_response", "duplicate_owned_cron_jobs", "storage_failed",
           "storage_unavailable", "setup_storage_unavailable", "already_running"}


def _connection(account):
    return account.provider, account.username, account.mailbox


@dataclass(repr=False)
class _Secret:
    revision: int
    connection: tuple
    expires_at: float
    password: str = field(repr=False)


class OnboardingService:
    def __init__(self, host, *, mailbox_factory=ImapMailbox, clock=time.time, check_active=lambda: None):
        self.host, self.mailbox_factory, self.clock = host, mailbox_factory, clock
        self._check_active = check_active
        self._secrets = {}
        self._state_lock = threading.RLock()

    def close(self) -> None:
        """Discard this runtime's pending credentials without changing durable state."""
        with self._state_lock:
            self._secrets.clear()

    def _key(self, draft, account_id):
        return draft.id, owner_key(draft.owner), account_id

    def _forget(self, draft_id):
        with self._state_lock:
            for key in list(self._secrets):
                if key[0] == draft_id:
                    del self._secrets[key]

    def expire(self) -> None:
        with self._state_lock, closing(self.host.open_setup_store()) as store:
            store.prune(now=self.clock())
            for key, secret in list(self._secrets.items()):
                if secret.expires_at <= self.clock():
                    del self._secrets[key]
                    continue
                try:
                    draft = store.get(key[0], SetupOwner(*key[1]))
                except HostError:
                    del self._secrets[key]
                    continue
                if draft.state in _TERMINAL or draft.revision != secret.revision:
                    del self._secrets[key]

    def _read(self, draft_id, owner, revision=None):
        self._check_active()
        if owner_key(owner)[0] != str(self.host.home().resolve()):
            raise HostError("setup_not_found")
        self.expire()
        with closing(self.host.open_setup_store()) as store:
            draft = store.get(draft_id, owner)
        if revision is not None and (type(revision) is not int or revision != draft.revision):
            raise HostError("setup_changed_retry")
        return draft

    def _save(self, draft, **changes):
        # Durable revision publication and cache rebinding are one short local
        # critical section. Never hold this lock over IMAP/model/host writes.
        with self._state_lock:
            self._check_active()
            with closing(self.host.open_setup_store()) as store:
                changed = store.cas(replace(draft, **changes), expected_revision=draft.revision, now=self.clock())
            accounts = {a.id: a for a in changed.candidate.accounts}
            for key, secret in list(self._secrets.items()):
                if key[0] != draft.id:
                    continue
                account = accounts.get(key[2])
                if (changed.state in _TERMINAL or account is None
                        or account.id not in changed.steps.get("credentials", {})
                        or secret.revision != draft.revision or secret.connection != _connection(account)
                        or secret.expires_at <= self.clock()):
                    del self._secrets[key]
                else:
                    secret.revision = changed.revision
            return changed

    def _secret(self, draft, account):
        with self._state_lock:
            secret = self._secrets.get(self._key(draft, account.id))
            if (secret and secret.revision == draft.revision and secret.expires_at > self.clock()
                    and secret.connection == _connection(account)):
                return secret
            return None

    def _required(self, draft):
        original = {a.id: a for a in draft.original.accounts}
        return [a for a in draft.candidate.accounts if a.enabled
                and (a.id not in original or not original[a.id].enabled)]

    def _ready(self, draft):
        markers = draft.steps.get("credentials", {})
        for account in self._required(draft):
            if account.id not in markers:
                return False
        for account in draft.candidate.accounts:
            marker = markers.get(account.id)
            if marker is not None and not marker.get("verified") and not self._secret(draft, account):
                return False
        return True

    def _missing_account(self, draft):
        markers = draft.steps.get("credentials", {})
        required = {a.id for a in self._required(draft)}
        return next((a.id for a in draft.candidate.accounts
                     if ((a.id in required and a.id not in markers)
                         or (a.id in markers and not markers[a.id].get("verified") and not self._secret(draft, a)))), "")

    def start(self, owner: SetupOwner, *, detected_language: str = "") -> SetupDraft:
        self._check_active()
        if owner_key(owner)[0] != str(self.host.home().resolve()):
            raise HostError("setup_not_found")
        self.expire()
        settings = self.host.load_settings()
        saved = self.host.has_saved_settings()
        with closing(self.host.open_setup_store()) as store:
            draft = store.create(owner, settings, original_saved=saved, now=self.clock())
        if draft.revision == 0:
            language = resolve_ui_language(saved=settings.ui_language, detected=detected_language)
            values = {"ui_language": language}
            if not saved:
                values["language"] = language
                if owner.surface == "telegram":
                    values["target"] = "telegram:" + owner.chat_id + (":" + owner.thread_id if owner.thread_id else "")
            draft = self._save(draft, candidate=replace(settings, **values))
        if draft.state != "needs_review" and self._has_writes(draft) and self._settings_conflict(draft, settings):
            draft = self._save(draft, state="needs_review")
        if draft.state in {"awaiting_confirmation", "validating"} and not self._ready(draft):
            draft = self._save(draft, state="needs_secret", form={**draft.form, "account_id": self._missing_account(draft)})
        return draft

    def _has_writes(self, draft):
        return (draft.state in {"committing", "scheduled", "repair_required"}
                or "settings_fingerprint" in draft.steps or bool(draft.steps.get("baselines"))
                or any(m.get("write_started") for m in draft.steps.get("credentials", {}).values()))

    def _settings_conflict(self, draft, actual):
        return ((actual != draft.original and not (self._has_writes(draft) and actual == draft.candidate))
                or ("settings_fingerprint" in draft.steps and actual != draft.candidate))

    def change(self, draft_id: str, owner: SetupOwner, revision: int, *, action: str, values: dict) -> SetupDraft:
        draft = self._read(draft_id, owner, revision)
        if draft.state in _TERMINAL or draft.state == "needs_review" or self._has_writes(draft):
            raise HostError("setup_changed_retry")
        if type(values) is not dict:
            raise HostError("invalid_setup_action")
        if action == "form":
            return self._save(draft, form={**draft.form, **values}, state="draft")
        candidate, steps, form = draft.candidate, copy.deepcopy(draft.steps), draft.form
        steps.pop("error_code", None)
        if action == "settings":
            if not values or set(values) - _SETTINGS_FIELDS:
                raise HostError("invalid_setup_action")
            values = dict(values)
            for name in ("important_senders", "ignored_senders", "always_notify_senders", "important_keywords"):
                if name in values and isinstance(values[name], (list, tuple)):
                    values[name] = tuple(values[name])
            if "schedule" in values:
                self.host.validate_schedule(values["schedule"])
            candidate = replace(candidate, **values)
        elif action in {"add", "edit", "enable", "disable"}:
            allowed = _ACCOUNT_FIELDS | ({"account_id"} if action != "add" else set())
            if set(values) - allowed or (action in {"enable", "disable"} and set(values) != {"account_id"}):
                raise HostError("invalid_setup_action")
            accounts = list(candidate.accounts)
            old = next((a for a in accounts if a.id == values.get("account_id")), None)
            if action != "add" and old is None:
                raise HostError("account_not_found")
            if action in {"enable", "disable"}:
                assert old is not None
                account = replace(old, enabled=action == "enable")
            else:
                fields = {name: value for name, value in values.items() if name in _ACCOUNT_FIELDS}
                if old:
                    account = replace(old, **fields)
                else:
                    identifier = uuid.uuid4().hex
                    username = fields.get("username", "")
                    account = Account(identifier, fields.pop("label", username), fields.pop("provider", ""),
                                      fields.pop("username", ""), secret_name(identifier), **fields)
                if any(a.id != account.id and _connection(a) == _connection(account) for a in accounts):
                    raise HostError("account_already_exists_use_edit")
                if old and _connection(old) != _connection(account):
                    identifier = uuid.uuid4().hex
                    account = replace(account, id=identifier, password_secret=secret_name(identifier))
                    accounts[accounts.index(old)] = replace(old, enabled=False)
                    steps.get("credentials", {}).pop(old.id, None)
            if old and account.id == old.id:
                accounts[accounts.index(old)] = account
            else:
                accounts.append(account)
            if action == "enable" and (not old or not old.enabled):
                steps.get("credentials", {}).pop(account.id, None)
            if action == "disable" or (old and old.enabled and not account.enabled):
                steps.get("credentials", {}).pop(account.id, None)
            candidate = replace(candidate, accounts=tuple(accounts))
            form = {"action": action, "account_id": account.id}
        else:
            raise HostError("invalid_setup_action")
        changed = self._save(draft, candidate=candidate, steps=steps, form=form, state="draft")
        if not self._ready(changed):
            return self._save(changed, state="awaiting_secret", form={**changed.form, "account_id": self._missing_account(changed)})
        return self._save(changed, state="awaiting_confirmation")

    def validate_password(self, draft_id: str, owner: SetupOwner, revision: int, password: str) -> SetupDraft:
        with self._state_lock:
            draft = self._read(draft_id, owner, revision)
            if draft.state in _TERMINAL or draft.state in {"committing", "scheduled", "repair_required", "needs_review"}:
                raise HostError("setup_changed_retry")
            if not isinstance(password, str) or any(c in password for c in "\r\n\x00"):
                raise HostError("invalid_app_password")
            account = next((a for a in draft.candidate.accounts if a.id == draft.form.get("account_id")), None)
            if account is None:
                raise HostError("account_not_found")
            marker = draft.steps.get("credentials", {}).get(account.id, {})
            # An ambiguous replacement must never be validated using the old value.
            if not password and (marker and not marker.get("verified") or account.id not in {a.id for a in draft.original.accounts}):
                return self._save(draft, state="needs_secret")
            self._secrets.pop(self._key(draft, account.id), None)
            steps = copy.deepcopy(draft.steps)
            steps.setdefault("credentials", {})[account.id] = {"write_started": bool(marker.get("write_started")), "verified": False}
            draft = self._save(draft, state="validating", steps=steps)
        try:
            value = password or self.host.get_password(account)
            with self.mailbox_factory(account, value) as mailbox:
                mailbox.snapshot()
        except Exception as exc:
            return self._failure(draft, exc, state="awaiting_secret")
        with self._state_lock:
            # Cancellation/edits during the network call must reject this result.
            draft = self._read(draft.id, owner, draft.revision)
            steps = copy.deepcopy(draft.steps)
            steps.pop("error_code", None)
            steps.setdefault("credentials", {})[account.id] = {"write_started": bool(marker.get("write_started")), "verified": not bool(password)}
            draft = self._save(draft, steps=steps, state="awaiting_confirmation")
            if password:
                self._secrets[self._key(draft, account.id)] = _Secret(draft.revision, _connection(account), self.clock() + 600, password)
            if not self._ready(draft):
                draft = self._save(draft, state="awaiting_secret", form={**draft.form, "account_id": self._missing_account(draft)})
            return draft

    def preview(self, draft_id: str, owner: SetupOwner) -> dict:
        with self._state_lock:
            draft = self._read(draft_id, owner)
            if draft.state == "awaiting_confirmation" and not self._ready(draft):
                draft = self._save(draft, state="needs_secret", form={**draft.form, "account_id": self._missing_account(draft)})
        result = {**self._result(draft), **self._display_settings(draft.candidate)}
        if draft.state in {"needs_review", "scheduled"}:
            current = self.host.load_settings()
            result.update(current_settings=self._display_settings(current), current_settings_fingerprint=settings_fingerprint(current),
                          recorded_steps=copy.deepcopy(draft.steps))
        return result

    def _display_settings(self, settings):
        # Only display fields, never credential references, host objects or secrets.
        return {"accounts": [{"account_id": a.id, "label": a.label, "provider": a.provider,
                "username": a.username, "mailbox": a.mailbox, "enabled": a.enabled} for a in settings.accounts],
                **{name: getattr(settings, name) for name in sorted(_SETTINGS_FIELDS)}}

    def _result(self, draft):
        steps = draft.steps
        completed = []
        if steps.get("credentials") and all(m.get("verified") for m in steps["credentials"].values()):
            completed.append("credentials")
        # Baselines are persisted as intent before queue initialization. A
        # publication marker proves initialization finished, including on repair.
        if steps.get("baselines") and "settings_fingerprint" in steps:
            completed.append("baselines")
        for marker, label in (("settings_fingerprint", "settings"), ("cron", "cron")):
            if marker in steps:
                completed.append(label)
        delivery = steps.get("test_delivery", {})
        if delivery.get("message_id"):
            completed.append("test_delivery")
        result = {"draft_id": draft.id, "revision": draft.revision, "state": draft.state, "completed_steps": completed}
        if "error_code" in steps:
            result["error"] = steps["error_code"]
        if "cron" in steps:
            result["cron_job_id"] = steps["cron"].get("job_id", "")
        if delivery.get("message_id"):
            result["message_id"] = delivery["message_id"]
        if draft.state == "scheduled" and steps.get("cron", {}).get("paused"):
            result["next_action"] = "resume_cron"
        elif draft.state == "needs_review":
            result["next_action"] = "acknowledge_restart"
        elif draft.state in {"needs_secret", "awaiting_secret"}:
            result["next_action"] = "enter_password"
        elif draft.state == "repair_required":
            result["next_action"] = "test_delivery" if "cron" in steps else "cron" if "settings_fingerprint" in steps else "settings"
        return result

    def _failure(self, draft, exc, *, state):
        code = error_code(exc)
        steps = {**draft.steps, "error_code": code if code in _ERRORS else "unexpected_error"}
        form = draft.form
        if state == "needs_secret":
            form = {**form, "account_id": self._missing_account(draft)}
        return self._save(draft, steps=steps, state=state, form=form)

    def cancel(self, draft_id: str, owner: SetupOwner, revision: int) -> SetupDraft:
        draft = self._read(draft_id, owner, revision)
        if self._has_writes(draft):
            raise HostError("setup_already_committing")
        return self._save(draft, state="cancelled")

    def acknowledge(self, draft_id: str, owner: SetupOwner, revision: int, *, current_fingerprint: str) -> SetupDraft:
        """Release an attempt after explicit review, retaining all write evidence.

        This neither completes automation nor rolls anything back. A subsequent
        start uses current settings and needs its own preview/confirmation.
        """
        draft = self._read(draft_id, owner, revision)
        with closing(self.host.open_store()) as store, store.run_lock() as acquired:
            if not acquired:
                raise HostError("already_running")
            draft = self._read(draft.id, owner, revision)
            current = self.host.load_settings()
            if current_fingerprint != settings_fingerprint(current):
                raise HostError("setup_changed_retry")
            paused = (draft.state == "scheduled" and current == draft.candidate
                      and "settings_fingerprint" in draft.steps and draft.steps.get("cron", {}).get("paused")
                      and bool(draft.steps.get("test_delivery", {}).get("message_id")))
            if draft.state != "needs_review" and not paused:
                raise HostError("setup_changed_retry")
            steps = {**draft.steps, "acknowledgement": {"reason": "paused" if paused else "conflict",
                                                       "settings_fingerprint": current_fingerprint}}
            return self._save(draft, state="acknowledged", steps=steps)

    def commit(self, draft_id: str, owner: SetupOwner, revision: int, *, resume_cron: bool = False) -> dict:
        if type(resume_cron) is not bool:
            raise HostError("invalid_setup_action")
        draft = self._read(draft_id, owner, revision)
        if draft.state == "completed":
            return self._result(draft)
        if draft.state in {"committing", "scheduled", "repair_required"}:
            return self.repair(draft_id, owner, revision, step="settings", resume_cron=resume_cron)
        if draft.state in _TERMINAL or draft.state == "needs_review":
            raise HostError("setup_changed_retry")
        if not self._ready(draft):
            return self._result(self._save(draft, state="needs_secret", form={**draft.form, "account_id": self._missing_account(draft)}))
        if draft.state != "awaiting_confirmation":
            raise HostError("setup_confirmation_required")
        if self.host.load_settings() != draft.original:
            return self._result(self._save(draft, state="needs_review"))
        snapshots = {}
        try:
            self.host.check_compatibility()
            self.host.check_environment(draft.candidate)
            self._check_active()
            self.host.resolve_target(draft.candidate.target)
            if any(a.enabled for a in draft.candidate.accounts):
                language = draft.candidate.language
                mail = MailContent("diagnostic@example.invalid", "Diagnostic", text("diagnostic.subject", language), "",
                                   text("diagnostic.body", language), False, False)
                triage_message(mail, replace(draft.candidate, ignored_senders=(), always_notify_senders=()), self.host.complete_structured)
            for account in draft.candidate.accounts:
                if account.enabled and (account in self._required(draft) or account.id in draft.steps.get("credentials", {})):
                    if account.id in draft.steps.get("baselines", {}):
                        continue
                    secret = self._secret(draft, account)
                    with self.mailbox_factory(account, secret.password if secret else self.host.get_password(account)) as mailbox:
                        epoch, uid = mailbox.snapshot()
                    snapshots[account.id] = {"uidvalidity": epoch, "last_uid": uid}
        except Exception as exc:
            return self._result(self._failure(draft, exc, state="awaiting_confirmation"))
        return self._publish(draft, snapshots, resume_cron=resume_cron)

    def repair(self, draft_id: str, owner: SetupOwner, revision: int, *, step: str, resume_cron: bool = False) -> dict:
        if type(resume_cron) is not bool:
            raise HostError("invalid_setup_action")
        draft = self._read(draft_id, owner, revision)
        if step not in {"credentials", "baselines", "settings", "cron", "test_delivery"}:
            raise HostError("invalid_setup_action")
        if draft.state == "completed":
            return self._result(draft)
        if draft.state in _TERMINAL:
            raise HostError("setup_changed_retry")
        if not self._has_writes(draft):
            raise HostError("setup_confirmation_required")
        if draft.state == "needs_review":
            return self._result(draft)
        # Check conflicts before asking for more secrets or doing slow repair
        # probes. Recheck again in _publish after those probes.
        with closing(self.host.open_store()) as store, store.run_lock() as acquired:
            if not acquired:
                return self._result(draft) | {"error": "already_running", "next_action": "retry"}
            draft = self._read(draft.id, owner, revision)
            actual = self.host.load_settings()
            if self._settings_conflict(draft, actual):
                return self._result(self._save(draft, state="needs_review"))
        # No readback heuristic can prove an interrupted password-only write.
        if any(not m.get("verified") for m in draft.steps.get("credentials", {}).values()):
            self._forget(draft.id)
            return self._result(self._save(draft, state="needs_secret", form={**draft.form, "account_id": self._missing_account(draft)}))
        snapshots = {}
        if "settings_fingerprint" not in draft.steps:
            try:
                for account in self._required(draft):
                    if account.id not in draft.steps.get("baselines", {}):
                        with self.mailbox_factory(account, self.host.get_password(account)) as mailbox:
                            epoch, uid = mailbox.snapshot()
                        snapshots[account.id] = {"uidvalidity": epoch, "last_uid": uid}
            except Exception as exc:
                return self._result(self._failure(draft, exc, state="repair_required"))
        return self._publish(draft, snapshots, resume_cron=resume_cron, refresh_cron=step == "cron")

    def _publish(self, draft, snapshots, *, resume_cron, refresh_cron=False):
        with closing(self.host.open_store()) as store:
            with store.run_lock() as acquired:
                if not acquired:
                    return self._result(draft) | {"error": "already_running", "next_action": "retry"}
                draft = self._read(draft.id, draft.owner, draft.revision)
                actual = self.host.load_settings()
                published = "settings_fingerprint" in draft.steps
                recovering = self._has_writes(draft)
                same_candidate = actual == draft.candidate
                if self._settings_conflict(draft, actual):
                    return self._result(self._save(draft, state="needs_review"))
                if not published and not self._ready(draft):
                    return self._result(self._save(draft, state="needs_secret", form={**draft.form, "account_id": self._missing_account(draft)}))
                if not recovering:
                    draft = self._save(draft, state="committing")
                try:
                    steps = copy.deepcopy(draft.steps)
                    for account in draft.candidate.accounts:
                        marker = steps.get("credentials", {}).get(account.id)
                        if marker is None or marker.get("verified"):
                            continue
                        secret = self._secret(draft, account)
                        if secret is None:
                            return self._result(self._save(draft, state="needs_secret", form={**draft.form, "account_id": account.id}))
                        marker["write_started"] = True
                        draft = self._save(draft, steps=steps)
                        secret = self._secret(draft, account)
                        if secret is None:
                            return self._result(self._save(draft, state="needs_secret", form={**draft.form, "account_id": account.id}))
                        self._check_active()
                        self.host.save_password(account, secret.password)
                        if self.host.get_password(account) != secret.password:
                            raise HostError("credential_save_failed")
                        marker["verified"] = True
                        draft = self._save(draft, steps=steps)
                    if not published:
                        # Persist the chosen post-confirmation baseline before its
                        # idempotent queue initialization. Never recompute on repair.
                        steps.setdefault("baselines", {}).update(snapshots)
                        draft = self._save(draft, steps=steps)
                        for account in draft.candidate.accounts:
                            baseline = steps["baselines"].get(account.id)
                            if baseline:
                                store.initialize(account, baseline["uidvalidity"], baseline["last_uid"])
                            if baseline or steps.get("credentials", {}).get(account.id, {}).get("verified"):
                                store.resume_source(account.id, self.clock())
                        if not (recovering and same_candidate):
                            self._check_active()
                            self.host.save_settings(draft.candidate)
                        if self.host.load_settings() != draft.candidate:
                            raise HostError("settings_save_failed")
                        steps["settings_fingerprint"] = settings_fingerprint(draft.candidate)
                        draft = self._save(draft, steps=steps)
                        self._forget(draft.id)
                    if "cron" not in steps or resume_cron or refresh_cron:
                        self._check_active()
                        job_id = self.host.install_schedule(draft.candidate, store)
                        if resume_cron and job_id and any(a.enabled for a in draft.candidate.accounts):
                            self.host.resume_schedule(job_id, store)
                        steps["cron"] = {"paused": bool(store.get_meta("cron_paused"))}
                        if job_id:
                            steps["cron"]["job_id"] = job_id
                        steps.pop("error_code", None)
                        draft = self._save(draft, steps=steps, state="scheduled")
                except Exception as exc:
                    ambiguous = any(m.get("write_started") and not m.get("verified")
                                    for m in draft.steps.get("credentials", {}).values())
                    if ambiguous:
                        self._forget(draft.id)
                    return self._result(self._failure(draft, exc, state="needs_secret" if ambiguous else "repair_required"))
        # Sending is deliberately outside the queue lock. A lost response can
        # duplicate this test on retry; use the same attempt ID in its text.
        if not draft.steps.get("test_delivery", {}).get("message_id") and any(a.enabled for a in draft.candidate.accounts):
            steps = copy.deepcopy(draft.steps)
            steps["test_delivery"] = {"started": True}
            draft = self._save(draft, steps=steps)
            try:
                self._check_active()
                result = self.host.send(self.host.resolve_target(draft.candidate.target),
                                        text("setup.notification", draft.candidate.language) + "\n" + draft.id, timeout=30)
                message_id = result.get("message_id")
                if (result.get("success") is not True or isinstance(message_id, bool)
                        or not isinstance(message_id, (str, int)) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", str(message_id))):
                    raise HostError("delivery_unverified")
                steps["test_delivery"]["message_id"] = str(message_id)
                steps.pop("error_code", None)
                draft = self._save(draft, steps=steps)
            except Exception as exc:
                return self._result(self._failure(draft, exc, state="repair_required"))
        paused = draft.steps.get("cron", {}).get("paused") and any(a.enabled for a in draft.candidate.accounts)
        return self._result(self._save(draft, state="scheduled" if paused else "completed"))
