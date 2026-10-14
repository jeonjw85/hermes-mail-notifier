"""Public terminal commands, using the same operations as Hermes tools."""

import argparse
import getpass
import json

from . import operations
from .diagnostics import diagnose, next_action
from .imap_client import ImapMailbox
from .service import check_mail, error_code, get_status, reset_account
from .setup import setup_plugin as _setup_plugin


def setup_plugin(host, *, read=input, read_secret=getpass.getpass, **kwargs):
    return _setup_plugin(host, read=read, read_secret=read_secret, mailbox_factory=ImapMailbox, **kwargs)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="mail_command", required=True)
    commands.add_parser("setup", help="계정·중요도·Telegram·확인 주기 설정")
    commands.add_parser("check", help="새 메일을 즉시 확인")
    status = commands.add_parser("status", help="계정별 처리 상태와 cron 확인")
    status.add_argument("--json", action="store_true", help="JSON으로 출력")
    reset = commands.add_parser("reset", help="선택 계정의 현재 메일함을 새 시작점으로 설정")
    reset.add_argument("account_id")
    accounts = commands.add_parser("accounts", help="계정별 조회·설정·중지·재개")
    actions = accounts.add_subparsers(dest="account_action", required=True)
    for action in ("list", "add", "edit", "enable", "disable"):
        child = actions.add_parser(action)
        if action in ("edit", "enable", "disable"):
            child.add_argument("account_id")
        if action in ("list", "enable", "disable"):
            child.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor", help="로컬 진단; --live는 실제 연결·모델·테스트 전송")
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--json", action="store_true")
    evaluation = commands.add_parser("evaluate", help="합성 사례로 현재 Hermes 모델을 평가 (실제 모델 호출)")
    evaluation.add_argument("--dataset")
    evaluation.add_argument("--runs", type=int, default=3)
    evaluation.add_argument("--limit", type=int)
    evaluation.add_argument("--output")
    for name, actions in (("history", ("list", "show")), ("queue", ("list", "retry", "retarget")),
                           ("feedback", ("add", "list", "remove")), ("policy", ("show", "set"))):
        group = commands.add_parser(name).add_subparsers(dest="action", required=True)
        for action in actions:
            child = group.add_parser(action)
            child.add_argument("--json", action="store_true")
            if name in ("history", "queue"):
                child.add_argument("--account", dest="account_id")
                if action in ("show", "retry", "retarget"):
                    child.add_argument("mail_id", nargs="?" if name == "queue" else None)
                if action == "list":
                    child.add_argument("--limit", type=int, default=20)
                if action == "retarget":
                    child.add_argument("--to", dest="target", required=True)
            if name == "feedback":
                if action == "add":
                    child.add_argument("mail_id")
                    child.add_argument("--decision", choices=("notify", "suppress", "review"), required=True)
                    child.add_argument("--note", default="")
                if action == "remove":
                    child.add_argument("feedback_id", type=int)
            if name == "policy" and action == "set":
                child.add_argument("--instructions", dest="importance_instructions")
                child.add_argument("--language")
                for field in ("important_senders", "ignored_senders", "always_notify_senders", "important_keywords"):
                    child.add_argument("--" + field.replace("_", "-"), nargs="+", help="공백으로 구분; 비우기는 --clear")
                child.add_argument("--clear", nargs="+", choices=operations.POLICY_FIELDS)


def _operation(args, host):
    if args.mail_command == "doctor":
        return diagnose(host, live=args.live, mailbox_factory=ImapMailbox)
    fields = vars(args)
    if args.mail_command == "history":
        return operations.history(host, **{k: fields[k] for k in ("mail_id", "account_id", "limit") if k in fields})
    if args.mail_command == "queue":
        return operations.queue(host, **{k: fields[k] for k in ("action", "mail_id", "account_id", "target", "limit") if k in fields})
    if args.mail_command == "feedback":
        return operations.feedback(host, **{k: fields[k] for k in ("action", "mail_id", "feedback_id", "decision", "note") if k in fields})
    changes = None
    if args.action == "set":
        changes = {k: fields[k] for k in operations.POLICY_FIELDS if fields.get(k) is not None}
        for name in fields.get("clear") or ():
            changes[name] = "" if name in ("importance_instructions", "language") else []
    return operations.policy(host, changes=changes)


def _print_operation(result, json_mode):
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "checks" in result:
        for row in result["checks"]:
            print(f"{row['name']}: {row['status']} · {row['code']}")
            if row["next_action"]:
                print(f"  다음 조치: {row['next_action']}")
    else:
        # JSON keeps explicit content queries readable while escaping terminal controls.
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _print_status(status):
    print(f"메일 알림 · {status['schedule']} · {status['target']} · 대기 {status['pending']}건")
    print(f"정책 {status['policy_version']} · DB v{status['schema_version']} · "
          f"수집 대기 {status['discovered']} · 판단 대기 {status['fetched']} · "
          f"전송 대기 {status['ready']} · 보류 {status['held']}")
    last = status.get("last_run")
    print(f"마지막 실행: {last['finished_at'] if last else '없음'}")
    for account in status["accounts"]:
        print(f"{account['account_id']} · {account['label']} ({account['provider']}) · "
              f"{'사용' if account['enabled'] else '중지'} · 대기 {account['pending']} · "
              f"최근 오류 {account.get('last_error') or '없음'}")
    print(f"cron: {status['cron']['job_id'] or '없음'} · {status['cron']['state']}")


def run_cli(args: argparse.Namespace, host) -> int:
    try:
        if args.mail_command == "setup":
            return setup_plugin(host)
        if args.mail_command == "check":
            report = check_mail(host)
            print(json.dumps(report.to_dict(), ensure_ascii=False))
            return 1 if report.errors else 0
        if args.mail_command == "evaluate":
            from .evaluation import DEFAULT_DATASET, evaluate, load_cases, write_report
            if args.limit is not None and args.limit < 1:
                raise ValueError("invalid_evaluation_limit")
            cases = load_cases(args.dataset or DEFAULT_DATASET)
            if args.limit is not None:
                cases = cases[:args.limit]
            result = evaluate(cases, host.complete_structured, settings=host.load_settings(), runs=args.runs)
            result["mode"] = "live-model"
            if args.output:
                write_report(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            smoke = len(cases) < 120 or args.runs < 3
            return 0 if result["release_pass"] or (smoke and not any(run["metrics"]["errors"] for run in result["runs"])) else 1
        if args.mail_command == "reset":
            print(json.dumps(reset_account(host, args.account_id), ensure_ascii=False))
            return 0
        if args.mail_command in ("doctor", "history", "queue", "feedback", "policy"):
            result = _operation(args, host)
            _print_operation(result, args.json)
            return 0 if result.get("success", True) else 1
        if args.mail_command == "accounts":
            if args.account_action in ("add", "edit"):
                return setup_plugin(host, account_action=args.account_action, account_id=getattr(args, "account_id", None))
            if args.account_action == "list":
                result = {"accounts": get_status(host)["accounts"]}
            else:
                result = operations.set_account_enabled(host, args.account_id, args.account_action == "enable", mailbox_factory=ImapMailbox)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        status = get_status(host)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            _print_status(status)
        return 0
    except Exception as exc:
        code = error_code(exc)
        print(json.dumps({"error": code, "next_action": next_action(code)}, ensure_ascii=False))
        return 1
