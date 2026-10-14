"""Local operational checks and explicitly requested live probes."""

from dataclasses import replace

from .hermes_host import HostError
from .imap_client import ImapMailbox
from .message import MailContent
from .migrations import SCHEMA_VERSION
from .policy import POLICY_VERSION
from .service import error_code, get_status
from .triage import triage_message


def next_action(code):
    if code in {"credential_unavailable", "imap_authentication_failed", "imap_mailbox_unavailable", "invalid_settings"}:
        return "hermes mail-notifier setup에서 계정·앱 비밀번호·메일함을 확인하세요."
    if code in {"credentials_not_reloadable", "credential_reload_check_failed", "destination_unavailable"}:
        return "현재 Hermes 프로필에 모델·Telegram 자격증명과 기본 채널을 저장하세요."
    if code.startswith("cron_"):
        return "hermes mail-notifier setup으로 cron을 복구하거나 hermes cron resume JOB_ID로 재개하세요."
    if code in {"queue_full", "cache_full", "work_held", "queue_scope_required", "pending_mail_not_found"}:
        return "hermes mail-notifier queue list로 확인한 뒤 queue retry MAIL_ID 또는 --account ID를 사용하세요."
    if code == "uidvalidity_changed":
        return "메일 서버 상태를 확인한 뒤 hermes mail-notifier reset ACCOUNT_ID로 새 기준점을 설정하세요."
    if code == "already_running":
        return "현재 실행이 끝난 뒤 다시 실행하세요."
    if code == "no_accounts":
        return "hermes mail-notifier accounts add로 계정을 연결하세요."
    if code == "not_run_yet":
        return "hermes mail-notifier check로 최초 확인을 실행하세요."
    return "hermes mail-notifier doctor --json으로 상태를 확인하고 해당 설정을 수정한 뒤 재시도하세요."


def diagnose(host, *, live=False, mailbox_factory=ImapMailbox):
    if type(live) is not bool:
        raise HostError("invalid_live_option")
    checks, queue = [], {}
    def add(name, status="ok", code="ok"):
        checks.append({"name": name, "status": status, "code": code,
                       "next_action": "" if status == "ok" else next_action(code)})
    # Keep check records even when a prior component is unavailable.
    def check(name, callback):
        try:
            value = callback()
        except Exception as exc:
            add(name, "error", error_code(exc))
        else:
            add(name)
            return value
    check("host", host.check_compatibility)
    settings = check("settings", host.load_settings)
    status = check("storage", lambda: get_status(host)) if settings else None
    if settings:
        check("credential_reload", lambda: host.check_environment(settings))
        check("destination", lambda: host.resolve_target(settings.target))
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
        for account in settings.accounts:
            if account.enabled:
                def imap_probe(account=account):
                    with mailbox_factory(account, host.get_password(account), timeout=20) as mailbox:
                        mailbox.snapshot()
                check(f"live_imap:{account.id}", imap_probe)
        mail = MailContent("diagnostic@example.invalid", "Diagnostic", "회의 일정 확인 요청", "",
                           "내일 회의에 참석 가능한지 회신해 주세요.", False, False)
        probe_settings = replace(settings, ignored_senders=(), always_notify_senders=())
        check("live_model", lambda: triage_message(mail, probe_settings, host.complete_structured))
        check("live_delivery", lambda: host.send(host.resolve_target(settings.target), "메일 알림 진단 테스트 · 실제 메일 내용은 포함되지 않습니다."))
    return {"success": not any(row["status"] == "error" for row in checks), "checks": checks,
            "queue": queue, "versions": {"schema": SCHEMA_VERSION, "policy": POLICY_VERSION}}
