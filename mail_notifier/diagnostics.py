"""Local operational checks and explicitly requested live probes."""

import urllib.request
from dataclasses import replace

from .credential_session import parse_page_url
from .hermes_host import HostError
from .i18n import resolve_ui_language, text
from .imap_client import ImapMailbox
from .message import MailContent
from .migrations import SCHEMA_VERSION
from .policy import POLICY_VERSION
from .service import error_code, get_status
from .triage import triage_message


def _credential_page_check(url):
    """Validate the operator page locally; never fetch it here."""
    if not url:
        raise HostError("credential_page_unavailable")
    try:
        parse_page_url(url)
    except (ValueError, TypeError):
        raise HostError("credential_page_insecure") from None
    return url


def _probe_credential_page(url, timeout=5):
    """Confirm the configured HTTPS page answers; report no page content."""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "hermes-mail-notifier-doctor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured HTTPS only
            if response.status >= 400:
                raise HostError()
    except Exception:
        raise HostError("credential_page_unreachable") from None


def next_action(code, *, language="ko") -> str:
    if code in {"credential_page_unavailable", "credential_page_insecure", "credential_page_unreachable"}:
        return text("guidance.credential_page", language)
    if code in {"credential_unavailable", "imap_authentication_failed", "imap_mailbox_unavailable", "invalid_settings"}:
        return text("guidance.account", language)
    if code in {"credentials_not_reloadable", "credential_reload_check_failed", "destination_unavailable"}:
        return text("guidance.credentials", language)
    if code in {"model_unavailable", "invalid_model_output"}:
        return text("guidance.model", language)
    if code == "model_not_configured":
        return text("guidance.model_not_configured", language)
    if code == "model_auth_failed":
        return text("guidance.model_auth_failed", language)
    if code == "model_out_of_credits":
        return text("guidance.model_out_of_credits", language)
    if code == "model_rate_limited":
        return text("guidance.model_rate_limited", language)
    if code == "model_not_found":
        return text("guidance.model_not_found", language)
    if code.startswith("cron_"):
        return text("guidance.cron", language)
    if code in {"queue_full", "cache_full", "work_held", "queue_scope_required", "pending_mail_not_found"}:
        return text("guidance.queue", language)
    if code == "uidvalidity_changed":
        return text("guidance.uidvalidity", language)
    if code == "already_running":
        return text("guidance.running", language)
    if code == "no_accounts":
        return text("guidance.no_accounts", language)
    if code == "not_run_yet":
        return text("guidance.first_check", language)
    if code in {"invalid_app_password", "credential_crypto_unavailable"}:
        return text("guidance.app_password", language)
    if code == "interactive_terminal_required":
        return text("guidance.terminal", language)
    if code in {"imap_connection_failed", "imap_timeout", "imap_invalid_fetch_response",
                "imap_invalid_uid_response", "imap_invalid_uid_state"}:
        return text("guidance.imap", language)
    if code in {"credential_save_failed", "settings_save_failed"}:
        return text("guidance.save_failed", language)
    if code in {"storage_failed", "storage_unavailable", "setup_storage_unavailable", "unsupported_setup_schema"}:
        return text("guidance.storage", language)
    if code in {"delivery_failed", "delivery_unverified"}:
        return text("guidance.delivery", language)
    if code in {"incompatible_hermes", "incompatible_hermes_onboarding", "hermes_executable_missing", "profile_unavailable"}:
        return text("guidance.incompatible", language)
    if code in {"setup_changed_retry", "setup_not_found", "setup_confirmation_required",
                "setup_already_committing", "setup_work_cancelled", "notification_not_ready"}:
        return text("guidance.stale_setup", language)
    if code in {"setup_limit_reached", "setup_queue_full"}:
        return text("guidance.setup_busy", language)
    if code in {"account_already_exists_use_edit", "account_not_found", "invalid_setup_action",
                "invalid_setup_draft", "invalid_target", "invalid_schedule", "invalid_enabled"}:
        return text("guidance.setup_input", language)
    if code in {"mail_not_found", "decision_not_found", "feedback_not_found"}:
        return text("guidance.not_found", language)
    if code in {"invalid_feedback", "invalid_feedback_action", "invalid_policy_changes", "invalid_limit",
                "invalid_queue_action", "invalid_work_transition", "invalid_credential_url",
                "invalid_credential_binding", "invalid_credential_submission", "invalid_cron_response",
                "duplicate_owned_cron_jobs", "cron_ownership_failed", "invalid_live_option"}:
        return text("guidance.invalid_request", language)
    return text("guidance.default", language)


def diagnose(host, *, live=False, mailbox_factory=ImapMailbox, page_probe=_probe_credential_page):
    if type(live) is not bool:
        raise HostError("invalid_live_option")
    checks, queue = [], {}
    def add(name, status="ok", code="ok"):
        checks.append({"name": name, "status": status, "code": code, "next_action": ""})
    # Keep check records even when a prior component is unavailable.
    def check(name, callback, *, failure="error"):
        try:
            value = callback()
        except Exception as exc:
            add(name, failure, error_code(exc))
        else:
            add(name)
            return value
    check("host", host.check_compatibility)
    settings = check("settings", host.load_settings)
    language = resolve_ui_language(saved=settings.ui_language if settings else "")
    status = check("storage", lambda: get_status(host)) if settings else None
    if settings:
        check("credential_reload", lambda: host.check_environment(settings))
        check("destination", lambda: host.resolve_target(settings.target))
        # Secure input degrades to CLI password entry, so an unset or unusable
        # page is a warning; only unreachable live pages are errors.
        page = check("credential_page", lambda: _credential_page_check(host.credential_page_url()), failure="warning")
        for account in settings.accounts:
            if account.enabled:
                def credential(account=account):
                    try:
                        host.get_password(account)
                    except Exception:
                        raise HostError("credential_unavailable") from None
                check(f"credential:{account.id}", credential)
        if not any(a.enabled for a in settings.accounts):
            add("accounts", "warning", "no_accounts")
    if status and settings is not None:
        queue = {name: status[name] for name in ("pending", "discovered", "fetched", "ready", "held", "cached_bodies", "last_run")}
        state = status["cron"]["state"]
        healthy = state in ("active", "scheduled", "running")
        broken = state in ("missing", "not_configured", "failed", "error") and any(a.enabled for a in settings.accounts)
        add("cron", "ok" if healthy else "error" if broken else "warning", "ok" if healthy else f"cron_{state}")
        if status["cron"].get("error"):
            add("cron_access", "error", status["cron"]["error"])
        if status["pending"] >= settings.max_pending:
            add("queue", "warning", "queue_full")
        if status["cached_bodies"] >= settings.max_cached_bodies:
            add("cache", "warning", "cache_full")
        if status["held"]:
            add("held", "warning", "work_held")
        if not status["last_run"]:
            add("last_run", "warning", "not_run_yet")
        for account in status["accounts"]:
            if account.get("last_error"):
                add(f"account:{account['account_id']}", "warning", account["last_error"])
    if live and settings:
        if page:
            # Only a live opt-in probes the network; the URL itself is never echoed.
            def live_page():
                try:
                    return page_probe(page, timeout=5)
                except Exception:
                    raise HostError("credential_page_unreachable") from None
            check("live_credential_page", live_page)
        for account in settings.accounts:
            if account.enabled:
                def imap_probe(account=account):
                    with mailbox_factory(account, host.get_password(account), timeout=20) as mailbox:
                        mailbox.snapshot()
                check(f"live_imap:{account.id}", imap_probe)
        mail = MailContent("diagnostic@example.invalid", "Diagnostic", text("diagnostic.subject", settings.language), "",
                           text("diagnostic.body", settings.language), False, False)
        probe_settings = replace(settings, ignored_senders=(), always_notify_senders=())
        check("live_model", lambda: triage_message(mail, probe_settings, host.complete_structured))
        check("live_delivery", lambda: host.send(host.resolve_target(settings.target), text("diagnostic.notification", settings.language)))
    for row in checks:
        if row["status"] != "ok":
            row["next_action"] = next_action(row["code"], language=language)
    return {"success": not any(row["status"] == "error" for row in checks), "checks": checks,
            "queue": queue, "versions": {"schema": SCHEMA_VERSION, "policy": POLICY_VERSION}}
