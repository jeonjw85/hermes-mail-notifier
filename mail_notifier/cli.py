"""Public terminal commands, using the same operations as Hermes tools."""

import argparse
import getpass
import json

from . import operations
from .diagnostics import diagnose, next_action
from .imap_client import ImapMailbox
from .i18n import text
from .service import check_mail, error_code, get_status, reset_account
from .setup import setup_plugin as _setup_plugin
from .setup import cli_language


def setup_plugin(host, *, read=input, read_secret=getpass.getpass, **kwargs):
    return _setup_plugin(host, read=read, read_secret=read_secret, mailbox_factory=ImapMailbox, **kwargs)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    def help_text(name):
        return text("cli.help." + name, "en") + " / " + text("cli.help." + name, "ko")
    commands = parser.add_subparsers(dest="mail_command", required=True)
    setup = commands.add_parser("setup", help=help_text("setup"))
    setup.add_argument("--ui-language", choices=("ko", "en"), default="", help=help_text("ui_language"))
    commands.add_parser("check", help=help_text("check"))
    status = commands.add_parser("status", help=help_text("status"))
    status.add_argument("--json", action="store_true", help=help_text("json"))
    reset = commands.add_parser("reset", help=help_text("reset"))
    reset.add_argument("account_id")
    accounts = commands.add_parser("accounts", help=help_text("accounts"))
    actions = accounts.add_subparsers(dest="account_action", required=True)
    for action in ("list", "add", "edit", "enable", "disable"):
        child = actions.add_parser(action)
        if action in ("edit", "enable", "disable"):
            child.add_argument("account_id")
        if action in ("list", "enable", "disable"):
            child.add_argument("--json", action="store_true")
        if action in ("add", "edit"):
            child.add_argument("--ui-language", choices=("ko", "en"), default="", help=help_text("ui_language"))
    doctor = commands.add_parser("doctor", help=help_text("doctor"))
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--json", action="store_true")
    evaluation = commands.add_parser("evaluate", help=help_text("evaluate"))
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
                    child.add_argument("--" + field.replace("_", "-"), nargs="+", help=help_text("list"))
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


def _print_operation(result, json_mode, language="en"):
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "checks" in result:
        for row in result["checks"]:
            print(f"{row['name']}: {row['status']} · {row['code']}")
            if row["next_action"]:
                print(f"  {text('cli.next_action', language)}: {row['next_action']}")
    else:
        # JSON keeps explicit content queries readable while escaping terminal controls.
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _print_status(status, language="en"):
    values = {key: value for key, value in status.items() if key != "language"}
    print(text("status.header", language, **values))
    print(text("status.counts", language, **values))
    last = status.get("last_run")
    none = text("status.none", language)
    print(text("status.last", language, value=last["finished_at"] if last else none))
    for account in status["accounts"]:
        print(text("status.account", language, **{**account,
              "enabled": text("status.enabled" if account["enabled"] else "status.disabled", language),
              "last_error": account.get("last_error") or none}))
    print(f"cron: {status['cron']['job_id'] or none} · {status['cron']['state']}")


def run_cli(args: argparse.Namespace, host) -> int:
    language = "en"
    try:
        language = cli_language(host.load_settings(), getattr(args, "ui_language", ""))
        if args.mail_command == "setup":
            return setup_plugin(host, ui_language=getattr(args, "ui_language", ""))
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
            _print_operation(result, args.json, language)
            return 0 if result.get("success", True) else 1
        if args.mail_command == "accounts":
            if args.account_action in ("add", "edit"):
                return setup_plugin(host, account_action=args.account_action, account_id=getattr(args, "account_id", None),
                                    ui_language=getattr(args, "ui_language", ""))
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
            _print_status(status, language)
        return 0
    except Exception as exc:
        code = error_code(exc)
        print(json.dumps({"error": code, "next_action": next_action(code, language=language)}, ensure_ascii=False))
        return 1
