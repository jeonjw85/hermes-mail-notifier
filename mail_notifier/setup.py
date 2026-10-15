"""Terminal input/output adapter for the shared onboarding service."""

import getpass
import json
import os
import sys
import warnings
from dataclasses import replace

from .config import Account, ConfigError, secret_name
from .diagnostics import next_action
from .hermes_host import HostError
from .i18n import resolve_ui_language, text
from .imap_client import ImapMailbox
from .onboarding import OnboardingService
from .onboarding_auth import SetupOwner
from .policy import POLICY_VERSION
from .service import error_code


def cli_language(settings, explicit=""):
    detected = next((os.environ[name] for name in ("LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(name)), "")
    return resolve_ui_language(explicit=explicit, saved=settings.ui_language, detected=detected)


def _password(read_secret, existing=False, *, language="en"):
    if read_secret is getpass.getpass and not sys.stdin.isatty():
        raise HostError("interactive_terminal_required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = read_secret(text("setup.password", language) + (text("setup.keep_password", language) if existing else "") + ": ")
    if not isinstance(password, str) or any(char in password for char in "\r\n\x00"):
        raise HostError("invalid_app_password")
    return password


class _Terminal:
    def __init__(self, host, service, draft, read, read_secret, language):
        self.host, self.service, self.draft = host, service, draft
        self.read, self.read_secret, self.language = read, read_secret, language

    def say(self, key, **values):
        print(text(key, self.language, **values))

    def ask(self, key, default="", *, clear=False):
        answer = self.read(text(key, self.language) + (f" [{default}]" if default else "") + ": ").strip()
        return "" if clear and answer == "-" else answer or default

    def yes(self, key):
        return self.ask(key, "no").casefold() in {"yes", "y", "예", "네"}

    def change(self, action, values):
        self.draft = self.service.change(self.draft.id, self.draft.owner, self.draft.revision, action=action, values=values)

    def field(self, name, default, validate, *, account=False, clear=False):
        while True:
            value = self.ask("setup." + name, default, clear=clear)
            try:
                validate(value)
                if account:
                    self.change("form", {name: value})
                return value
            except (ConfigError, HostError) as exc:
                self.say("setup.input_error", code=error_code(exc))
                if not value and not default and not clear:
                    raise KeyboardInterrupt

    def choose(self):
        while True:
            value = self.ask("setup.choose")
            if not value or value == "cancel":
                raise KeyboardInterrupt
            for index, account in enumerate(self.draft.candidate.accounts, 1):
                if value in (str(index), account.id):
                    return account
            self.say("setup.input_error", code="account_not_found")

    def password(self, existing=False):
        return _password(self.read_secret, existing, language=self.language)

    def validate(self, password):
        while True:
            self.draft = self.service.validate_password(self.draft.id, self.draft.owner, self.draft.revision, password)
            if self.draft.state == "awaiting_confirmation":
                return True
            if self.draft.state == "awaiting_secret" and "error_code" not in self.draft.steps:
                # A resumed multi-account draft may need more than one secret.
                # The service selects the next unvalidated connection.
                account = next(a for a in self.draft.candidate.accounts if a.id == self.draft.form["account_id"])
                print(f"{account.label} ({account.provider}) · {account.username}")
                password = self.password()
                continue
            self.say("setup.connection_error", code=self.draft.steps.get("error_code", "app_password_required"))
            action = self.ask("setup.retry", "cancel")
            if action == "cancel":
                raise KeyboardInterrupt
            if action == "back":
                return False
            if action == "password":
                password = self.password()

    def account(self, old=None):
        while True:
            form = self.draft.form
            self.change("form", {"action": "edit" if old else "add", "account_id": old.id if old else ""})
            def valid(name, value):
                defaults = dict(id="draft", password_secret=secret_name("draft"), label="draft", provider="gmail", username="draft")
                Account(**{**defaults, name: value})
            provider = self.field("provider", old.provider if old else form.get("provider", "gmail"),
                                  lambda v: valid("provider", v), account=True)
            username = self.field("username", old.username if old else form.get("username", ""),
                                  lambda v: valid("username", v), account=True)
            label, mailbox = (old.label, old.mailbox) if old else (username, "INBOX")
            if old or self.yes("setup.advanced_account"):
                label = self.field("label", label, lambda v: valid("label", v), account=True)
                mailbox = self.field("mailbox", mailbox, lambda v: valid("mailbox", v), account=True)
            self.say("setup.provider." + provider)
            values = dict(provider=provider, username=username, label=label, mailbox=mailbox)
            if old:
                values["account_id"] = old.id
            try:
                self.change("edit" if old else "add", values)
            except HostError as exc:
                if exc.code != "account_already_exists_use_edit":
                    raise
                self.say("setup.input_error", code=exc.code)
                continue
            selected = self.draft.form["account_id"]
            same = old is not None and selected == old.id
            if old and not same:
                self.say("setup.identity_changed")
            if self.validate(self.password(existing=same)):
                return
            # Editing an unpublished candidate avoids appending another identity
            # when the user backs up after a failed connection probe.
            old = next(a for a in self.draft.candidate.accounts if a.id == selected)

    def advanced(self):
        settings = self.draft.candidate
        for name in ("target", "schedule", "language", "ui_language", "important_senders", "ignored_senders",
                     "important_keywords", "always_notify_senders", "importance_instructions"):
            current = getattr(settings, name)
            is_list = isinstance(current, tuple)
            parse = (lambda v: tuple(item.strip() for item in v.split(",") if item.strip())) if is_list else lambda v: v
            def validate(value):
                replace(settings, **{name: parse(value)})
                if name == "schedule":
                    self.host.validate_schedule(value)
            value = self.field(name, ", ".join(current) if is_list else current, validate,
                               clear=is_list or name == "importance_instructions")
            self.change("settings", {name: parse(value)})
            settings = self.draft.candidate

    def preview(self):
        preview = self.service.preview(self.draft.id, self.draft.owner)
        self.say("setup.disable_only" if preview.get("activation_checks") == "not_run_disable_only" else "setup.preview")
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return preview

    def result(self, result):
        if result["state"] == "completed":
            if result.get("activation_checks") == "not_run_disable_only":
                self.say("telegram.disabled_saved")
                return 0
            self.say("setup.completed", job=result.get("cron_job_id") or text("status.none", self.language))
            self.say("setup.policy", version=POLICY_VERSION)
            self.say("setup.retention")
            self.say("setup.gateway")
            return 0
        self.say("setup.partial", state=result["state"], steps=", ".join(result["completed_steps"]),
                 code=result.get("error", ""), next=result.get("next_action", ""))
        if result.get("error"):
            print(next_action(result["error"], language=self.language))
        return 1


def setup_plugin(host, *, read=input, read_secret=getpass.getpass, mailbox_factory=ImapMailbox,
                 account_action=None, account_id=None, ui_language: str = "") -> int:
    terminal = None
    language = "en"
    try:
        language = cli_language(host.load_settings(), ui_language)
        if account_action not in (None, "add", "edit"):
            raise HostError("invalid_setup_action")
        service = OnboardingService(host, mailbox_factory=mailbox_factory)
        draft = service.start(SetupOwner(str(host.home()), "cli"), detected_language=language)
        terminal = _Terminal(host, service, draft, read, read_secret, language)
        terminal.say("setup.title")
        disable_only = service.preview(draft.id, draft.owner).get("activation_checks") == "not_run_disable_only"
        if draft.state == "scheduled" and (draft.steps.get("test_delivery", {}).get("message_id") or disable_only):
            preview = terminal.preview()
            if not disable_only and terminal.yes("setup.resume"):
                return terminal.result(service.repair(draft.id, draft.owner, draft.revision, step="cron", resume_cron=True))
            if not terminal.yes("setup.continue_paused"):
                return terminal.result(service.preview(draft.id, draft.owner))
            service.acknowledge(draft.id, draft.owner, draft.revision,
                                current_fingerprint=preview["current_settings_fingerprint"])
            terminal.say("setup.paused_management")
            terminal.draft = service.start(draft.owner, detected_language=language)
        elif draft.state in {"committing", "repair_required", "scheduled"}:
            preview = terminal.preview()
            if terminal.yes("setup.repair"):
                step = "test_delivery" if "cron" in draft.steps else "cron" if "settings_fingerprint" in draft.steps else "settings"
                return terminal.result(service.repair(draft.id, draft.owner, draft.revision, step=step))
            if not preview.get("can_handoff_delivery"):
                return terminal.result(preview)
            terminal.say("setup.unfinished_delivery")
            if not terminal.yes("setup.continue_unfinished"):
                return terminal.result(preview)
            service.acknowledge(draft.id, draft.owner, draft.revision,
                                current_fingerprint=preview["current_settings_fingerprint"], allow_unfinished_delivery=True)
            terminal.draft = service.start(draft.owner, detected_language=language)
        if draft.state == "needs_review":
            preview = terminal.preview()
            terminal.say("setup.review_external")
            if terminal.yes("setup.restart"):
                service.acknowledge(draft.id, draft.owner, draft.revision,
                                    current_fingerprint=preview["current_settings_fingerprint"])
                terminal.draft = service.start(draft.owner, detected_language=language)
            else:
                return terminal.result(preview)
        draft = terminal.draft
        if ui_language:
            values = {"ui_language": language}
            if not draft.original_saved:
                values["language"] = language
            terminal.change("settings", values)
        if draft.state == "needs_secret" and draft.form.get("account_id"):
            terminal.validate(terminal.password())
        elif account_action == "edit":
            old = next((a for a in terminal.draft.candidate.accounts if a.id == account_id), None)
            if old is None:
                raise HostError("account_not_found")
            terminal.account(old)
        elif account_action == "add" or not terminal.draft.candidate.accounts:
            terminal.account()
        else:
            while True:
                for index, account in enumerate(terminal.draft.candidate.accounts, 1):
                    state = text("status.enabled" if account.enabled else "status.disabled", language)
                    print(f"{index}. {account.label} ({account.provider}) {account.id} · {state}")
                action = terminal.ask("setup.action", "done").lower()
                if action == "done":
                    break
                if action == "add":
                    terminal.account()
                elif action == "edit":
                    terminal.account(terminal.choose())
                elif action in {"enable", "disable"}:
                    account = terminal.choose()
                    terminal.change(action, {"account_id": account.id})
                    if action == "enable":
                        terminal.validate("")
                else:
                    terminal.say("setup.input_error", code="invalid_setup_action")
        terminal.preview()
        if not account_action and terminal.yes("setup.advanced"):
            terminal.advanced()
            terminal.preview()
        # Even a no-change repeat explicitly approves the preserved snapshot.
        if terminal.draft.state == "draft":
            terminal.change("settings", {"ui_language": terminal.draft.candidate.ui_language})
        if not terminal.yes("setup.confirm"):
            raise KeyboardInterrupt
        current = terminal.draft
        return terminal.result(service.commit(current.id, current.owner, current.revision))
    except (KeyboardInterrupt, EOFError):
        if terminal:
            current = terminal.draft
            try:
                terminal.service.cancel(current.id, current.owner, current.revision)
            except HostError:
                # Never claim cancellation/rollback after an external write.
                return terminal.result(terminal.service.preview(current.id, current.owner))
        print(text("setup.cancelled", language))
        return 1
    except Exception as exc:
        code = error_code(exc)
        print(text("setup.failed", language, code=code))
        print(next_action(code, language=language))
        return 1
