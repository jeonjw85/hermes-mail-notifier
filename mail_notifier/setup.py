"""Recoverable, terminal-only account drafts and atomic settings publication."""

import getpass
import sys
import time
import uuid
import warnings
from dataclasses import replace

from .config import Account, ConfigError, Settings, secret_name
from .hermes_host import HostError
from .i18n import text
from .imap_client import ImapMailbox, MailboxError
from .policy import POLICY_VERSION
from .service import error_code

_PROVIDER_HELP = {
    "naver": "네이버: IMAP 사용 + 2단계 인증 + 애플리케이션 비밀번호 (https://help.naver.com/service/30029/contents/21344)",
    "daum": "다음/한메일: IMAP 사용 + 카카오계정 2단계 인증 + 앱 비밀번호 (https://cs.daum.net/faq/service/43/category/9234/detail/31143)",
    "gmail": "Gmail: Google 2단계 인증 + 앱 비밀번호 발급 가능한 계정 (https://support.google.com/accounts/answer/185833?hl=ko)",
}


def _ask(read, label, default="", *, clear=False):
    answer = read(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return "" if clear and answer == "-" else answer or default


def _password(read_secret, existing=False):
    if read_secret is getpass.getpass and not sys.stdin.isatty():
        raise HostError("interactive_terminal_required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = read_secret("앱 비밀번호" + (" [Enter: 기존 값 유지]" if existing else "") + ": ")
    if any(char in password for char in "\r\n\x00"):
        raise HostError("invalid_app_password")
    return password


def _choose_account(read, accounts):
    while True:
        value = _ask(read, "계정 ID 또는 표시 번호 (cancel: 취소)")
        if not value or value == "cancel":
            raise KeyboardInterrupt
        for index, account in enumerate(accounts, 1):
            if value == account.id or value == str(index):
                return index - 1, account
        print("account_not_found · 계정 ID를 다시 입력하세요.")


def _valid(read, label, default, validate, *, clear=False):
    while True:
        value = _ask(read, label, default, clear=clear)
        try:
            validate(value)
            return value
        except (ConfigError, HostError) as exc:
            print(f"입력 확인: {error_code(exc)} · 이 항목을 다시 입력하세요.")
            if not value and not default and not clear:
                raise KeyboardInterrupt


def _account_field(name, value):
    defaults = dict(id="draft", password_secret=secret_name("draft"), label="draft", provider="gmail", username="draft")
    Account(**{**defaults, name: value})


def _probe(host, account, password, mailbox_factory, read, read_secret):
    while True:
        try:
            with mailbox_factory(account, password or host.get_password(account)) as mailbox:
                return password, mailbox.snapshot()
        except (MailboxError, HostError) as exc:
            print(f"연결 실패: {error_code(exc)}")
            action = _ask(read, "retry: 재시도 / password: 비밀번호 수정 / back: 입력 수정 / cancel: 취소", "cancel")
            if action == "cancel":
                raise KeyboardInterrupt
            if action == "back":
                return None
            if action == "password":
                password = _password(read_secret, existing=bool(password)) or password


def _draft(host, old, accounts, passwords, read, read_secret, mailbox_factory):
    draft = old
    while True:
        provider = _valid(read, "서비스 naver/daum/gmail", draft.provider if draft else "gmail",
                          lambda value: _account_field("provider", value.lower())).lower()
        print(_PROVIDER_HELP[provider])
        label = _valid(read, "계정 별칭", draft.label if draft else provider, lambda value: _account_field("label", value))
        username = _valid(read, "로그인 아이디/메일 주소", draft.username if draft else "", lambda value: _account_field("username", value))
        mailbox_name = _valid(read, "메일함", draft.mailbox if draft else "INBOX", lambda value: _account_field("mailbox", value))
        same = old is not None and (provider, username, mailbox_name) == (old.provider, old.username, old.mailbox)
        identifier = old.id if same and old is not None else uuid.uuid4().hex
        draft = Account(identifier, label, provider, username, secret_name(identifier), mailbox_name, old.enabled if old else True)
        if any(a.id != identifier and (a.provider, a.username, a.mailbox) == (provider, username, mailbox_name) for a in accounts):
            print("account_already_exists_use_edit · 로그인 입력을 수정하세요.")
            continue
        password = _password(read_secret, existing=same) or passwords.get(identifier)
        while not password and not same:
            print("app_password_required")
            password = _password(read_secret)
        result = _probe(host, draft, password, mailbox_factory, read, read_secret)
        if result is not None:
            return draft, *result


def setup_plugin(host, *, read=input, read_secret=getpass.getpass, mailbox_factory=ImapMailbox,
                 account_action=None, account_id=None) -> int:
    stage = "입력·연결 검사"
    try:
        host.check_compatibility()
        original = host.load_settings()
        accounts = list(original.accounts)
        passwords, baselines = {}, {}
        if account_action not in (None, "add", "edit"):
            raise HostError("invalid_setup_action")
        print("메일 알림 설정 · Enter는 현재 값 유지, 목록/지침의 '-'는 비우기, Ctrl-C는 취소")
        while True:
            for index, account in enumerate(accounts, 1):
                print(f"{index}. {account.label} ({account.provider}) {account.id} · {'사용' if account.enabled else '중지'}")
            action = account_action or _ask(read, "작업 add/edit/disable/enable/done", "done").lower()
            if action == "done":
                break
            if action not in {"add", "edit", "disable", "enable"}:
                print("invalid_setup_action · 작업을 다시 선택하세요.")
                continue
            if account_action == "edit":
                match = next(((i, a) for i, a in enumerate(accounts) if a.id == account_id), None)
                if match is None:
                    raise HostError("account_not_found")
                index, old = match
            else:
                index, old = _choose_account(read, accounts) if action != "add" else (len(accounts), None)
            if action == "disable" and old is not None:
                accounts[index] = replace(old, enabled=False)
                continue
            if action == "enable" and old is not None:
                result = _probe(host, old, passwords.get(old.id), mailbox_factory, read, read_secret)
                if result is None:
                    continue
                password, baselines[old.id] = result
                if password:
                    passwords[old.id] = password
                accounts[index] = replace(old, enabled=True)
                continue
            account, password, baseline = _draft(host, old, accounts, passwords, read, read_secret, mailbox_factory)
            if password:
                passwords[account.id] = password
            baselines[account.id] = baseline
            if old and account.id == old.id:
                accounts[index] = account
            else:
                if old:
                    accounts[index] = replace(old, enabled=False)
                    print("로그인 정보 변경: 이전 연결은 중지하고 새 연결 시점 이후 메일부터 확인합니다.")
                accounts.append(account)
            if account_action:
                break
        settings = replace(original, accounts=tuple(accounts))
        if not account_action:
            for name, label in (("target", "Telegram 목적지 telegram 또는 telegram:chat_id[:thread_id]"),
                                ("schedule", "확인 주기"), ("language", "요약 언어")):
                validate = host.validate_schedule if name == "schedule" else lambda value: replace(settings, **{name: value})
                value = _valid(read, label, getattr(settings, name), validate)
                settings = replace(settings, **{name: value})
            for name, label in (("important_senders", "중요 발신자/도메인 (쉼표 구분)"),
                                ("ignored_senders", "제외 발신자/도메인 (쉼표 구분)"),
                                ("important_keywords", "중요 키워드 (쉼표 구분)")):
                parse = lambda value: tuple(item.strip() for item in value.split(",") if item.strip())
                value = _valid(read, label, ", ".join(getattr(settings, name)),
                               lambda value: replace(settings, **{name: parse(value)}), clear=True)
                settings = replace(settings, **{name: parse(value)})
            instructions = _ask(read, "추가 중요도 판단 기준", original.importance_instructions, clear=True)
            settings = replace(settings, importance_instructions=instructions)
        stage = "자격증명 재로드 검사"
        host.check_environment(settings)
        store = host.open_store()
        try:
            with store.run_lock() as acquired:
                if not acquired:
                    raise HostError("already_running")
                if host.load_settings() != original:
                    raise HostError("settings_changed_retry")
                for account in accounts:
                    if account.id in passwords:
                        host.save_password(account, passwords[account.id])
                for account in accounts:
                    if account.id in baselines:
                        store.initialize(account, *baselines[account.id])
                        store.resume_source(account.id, time.time())
                stage = "설정 저장 중"
                host.save_settings(settings)
                stage = "설정 저장 완료, cron 등록"
                job_id = host.install_schedule(settings, store)
                stage = "설정 저장·cron 등록 완료, Telegram 확인"
                if any(account.enabled for account in accounts):
                    host.send(settings.target, text("setup.notification", settings.language))
        finally:
            store.close()
        print(f"설정 완료 · cron: {job_id or '없음 (활성 계정 없음)'}")
        print(f"정책: {POLICY_VERSION} · 업무·개인 메일 중심. 중요 발신자·키워드는 AI 판단의 참고 정보입니다.")
        print("대기 알림은 기존 목적지를 유지합니다. 판단 이유·요약은 완료 후 30일 보존합니다.")
        print("자동 확인은 Hermes gateway 실행 중에 동작합니다. 상태: hermes mail-notifier status")
        return 0
    except (KeyboardInterrupt, EOFError):
        print(f"설정 취소 · 단계: {stage}. 다시 실행: hermes mail-notifier setup")
        return 1
    except Exception as exc:
        print(f"설정 실패 · 단계: {stage} · 오류: {error_code(exc)}")
        if error_code(exc) == "credentials_not_reloadable":
            print("cron에서 현재 자격증명을 다시 읽을 수 없습니다. 같은 프로필의 Hermes 설정으로 모델·Telegram 인증을 저장하거나 재로드 가능한 비밀정보 소스를 구성하세요.")
        print("입력/연결을 확인하고 hermes mail-notifier setup을 다시 실행하세요.")
        return 1
