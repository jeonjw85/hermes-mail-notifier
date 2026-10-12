"""Terminal setup and the public mail-notifier subcommands."""

import argparse
import getpass
import json
import sys
import uuid
import warnings
from dataclasses import replace

from .config import Account, ConfigError, Settings, secret_name
from .hermes_host import HostError
from .imap_client import ImapMailbox
from .service import check_mail, error_code, get_status, reset_account

_PROVIDER_HELP = {
    "naver": "네이버: IMAP 사용 + 2단계 인증 + 애플리케이션 비밀번호 (https://help.naver.com/service/30029/contents/21344)",
    "daum": "다음/한메일: IMAP 사용 + 카카오계정 2단계 인증 + 앱 비밀번호 (https://cs.daum.net/faq/service/43/category/9234/detail/31143)",
    "gmail": "Gmail: Google 2단계 인증 + 앱 비밀번호 발급 가능한 계정 (https://support.google.com/accounts/answer/185833?hl=ko)",
}


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="mail_command", required=True)
    commands.add_parser("setup", help="계정·중요도·Telegram·확인 주기 설정")
    commands.add_parser("check", help="새 메일을 즉시 확인")
    status = commands.add_parser("status", help="계정별 처리 상태와 cron 확인")
    status.add_argument("--json", action="store_true", help="JSON으로 출력")
    reset = commands.add_parser("reset", help="선택 계정의 현재 메일함을 새 시작점으로 설정")
    reset.add_argument("account_id")


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
    value = _ask(read, "계정 ID 또는 표시 번호")
    for index, account in enumerate(accounts, 1):
        if value == account.id or value == str(index):
            return index - 1, account
    raise HostError("account_not_found")


def setup_plugin(host, *, read=input, read_secret=getpass.getpass) -> int:
    stage = "입력·연결 검사"
    try:
        host.check_compatibility()
        original = host.load_settings()
        accounts = list(original.accounts)
        passwords, baselines = {}, {}
        print("메일 알림 설정 · Enter는 현재 값 유지, 목록/지침의 '-'는 비우기, Ctrl-C는 취소")
        while True:
            for index, account in enumerate(accounts, 1):
                print(f"{index}. {account.label} ({account.provider}) {account.id} · {'사용' if account.enabled else '중지'}")
            action = _ask(read, "작업 add/edit/disable/enable/done", "done").lower()
            if action == "done":
                break
            if action not in {"add", "edit", "disable", "enable"}:
                raise HostError("invalid_setup_action")
            index, old = _choose_account(read, accounts) if action != "add" else (None, None)
            if action == "disable":
                accounts[index] = replace(old, enabled=False)
                continue
            if action == "enable":
                with ImapMailbox(old, passwords.get(old.id) or host.get_password(old)) as mailbox:
                    baselines[old.id] = mailbox.snapshot()
                accounts[index] = replace(old, enabled=True)
                continue
            provider = _ask(read, "서비스 naver/daum/gmail", old.provider if old else "gmail").lower()
            if provider not in _PROVIDER_HELP:
                raise ConfigError("provider must be naver, daum, or gmail")
            print(_PROVIDER_HELP[provider])
            label = _ask(read, "계정 별칭", old.label if old else provider)
            username = _ask(read, "로그인 아이디/메일 주소", old.username if old else "")
            mailbox_name = _ask(read, "메일함", old.mailbox if old else "INBOX")
            same_identity = old is not None and (provider, username, mailbox_name) == (old.provider, old.username, old.mailbox)
            account_id = old.id if same_identity else uuid.uuid4().hex
            account = Account(account_id, label, provider, username, secret_name(account_id), mailbox_name,
                              old.enabled if old else True)
            if any(a.id != account_id and (a.provider, a.username, a.mailbox) == (provider, username, mailbox_name) for a in accounts):
                raise HostError("account_already_exists_use_edit")
            password = _password(read_secret, existing=same_identity)
            if password:
                passwords[account_id] = password
            elif not same_identity:
                raise HostError("app_password_required")
            with ImapMailbox(account, password or passwords.get(account_id) or host.get_password(account)) as mailbox:
                baselines[account_id] = mailbox.snapshot()
            if same_identity:
                accounts[index] = account
            else:
                if old:
                    accounts[index] = replace(old, enabled=False)
                    print("로그인 정보 변경: 이전 연결은 중지하고 새 연결 시점 이후 메일부터 확인합니다.")
                accounts.append(account)
        target = _ask(read, "Telegram 목적지 telegram 또는 telegram:chat_id[:thread_id]", original.target)
        schedule = _ask(read, "확인 주기", original.schedule)
        language = _ask(read, "요약 언어", original.language)
        rules = {}
        for name, label in (("important_senders", "중요 발신자/도메인 (쉼표 구분)"),
                            ("ignored_senders", "제외 발신자/도메인 (쉼표 구분)"),
                            ("important_keywords", "중요 키워드 (쉼표 구분)")):
            value = _ask(read, label, ", ".join(getattr(original, name)), clear=True)
            rules[name] = tuple(item.strip() for item in value.split(",") if item.strip())
        instructions = _ask(read, "추가 중요도 판단 기준", original.importance_instructions, clear=True)
        settings = Settings(accounts=tuple(accounts), target=target, schedule=schedule, language=language,
                            importance_instructions=instructions, **rules)
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
                stage = "설정 저장 중"
                host.save_settings(settings)
                stage = "설정 저장 완료, cron 등록"
                job_id = host.install_schedule(settings, store)
                stage = "설정 저장·cron 등록 완료, Telegram 확인"
                if any(account.enabled for account in accounts):
                    host.send(settings.target, "메일 알림 설정 완료. 연결 시점 이후의 중요한 새 메일을 요약해 알려드립니다.")
        finally:
            store.close()
        print(f"설정 완료 · cron: {job_id or '없음 (활성 계정 없음)'}")
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


def run_cli(args: argparse.Namespace, host) -> int:
    try:
        if args.mail_command == "setup":
            return setup_plugin(host)
        if args.mail_command == "check":
            report = check_mail(host)
            print(json.dumps(report.to_dict(), ensure_ascii=False))
            return 1 if report.errors else 0
        if args.mail_command == "reset":
            print(json.dumps(reset_account(host, args.account_id), ensure_ascii=False))
            return 0
        status = get_status(host)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"메일 알림 · {status['schedule']} · {status['target']} · 대기 {status['pending']}건")
            for account in status["accounts"]:
                print(f"{account['account_id']} · {account['label']} ({account['provider']}) · "
                      f"{'사용' if account['enabled'] else '중지'} · 대기 {account['pending']} · "
                      f"최근 오류 {account.get('last_error') or '없음'}")
            print(f"cron: {status['cron']['job_id'] or '없음'} · {status['cron']['state']}")
        return 0
    except Exception as exc:
        print(json.dumps({"error": error_code(exc)}, ensure_ascii=False))
        return 1
