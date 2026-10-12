"""Hermes native plugin entry point; registration has no runtime side effects."""

import json

from .mail_notifier.cli import configure_parser, run_cli
from .mail_notifier.hermes_host import HermesHost
from .mail_notifier.service import check_mail, error_code, get_status


def register(ctx):
    def command(args):
        return run_cli(args, HermesHost(ctx))

    def check(args, **kwargs):
        try:
            report = check_mail(HermesHost(ctx))
            result = {"success": not report.errors, **report.to_dict()}
        except Exception as exc:
            result = {"success": False, "error": error_code(exc)}
        return json.dumps(result, ensure_ascii=False)

    def status(args, **kwargs):
        try:
            result = {"success": True, **get_status(HermesHost(ctx))}
        except Exception as exc:
            result = {"success": False, "error": error_code(exc)}
        return json.dumps(result, ensure_ascii=False)

    ctx.register_cli_command(name="mail-notifier", help="중요 메일 요약과 Telegram 알림",
                             setup_fn=configure_parser, handler_fn=command)
    for name, description, handler in (
        ("mail_notifier_check", "Check new emails and send important summaries to the configured Telegram target.", check),
        ("mail_notifier_status", "Show email monitoring status and pending notifications without reading mail.", status),
    ):
        ctx.register_tool(
            name=name, toolset="mail_notifier", description=description, handler=handler,
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        )
