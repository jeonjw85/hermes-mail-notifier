"""Hermes native plugin entry point; registration has no runtime side effects."""

import json

from .mail_notifier.cli import configure_parser, run_cli
from .mail_notifier import operations
from .mail_notifier.diagnostics import diagnose, next_action
from .mail_notifier.hermes_host import HermesHost, HostError
from .mail_notifier.service import check_mail, error_code, get_status
from .mail_notifier.telegram_onboarding import TelegramOnboarding


def _parameters(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_TEXT = {"type": "string"}
_SCOPE = {"mail_id": _TEXT, "account_id": _TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}
_POLICY = {name: ({"type": "string"} if name in ("importance_instructions", "language") else
                 {"type": "array", "items": {"type": "string"}}) for name in operations.POLICY_FIELDS}
_TOOLS = (
    ("history", "Read saved email decisions, reasons and delivery history; use mail_id for a single message.", operations.history, _parameters(_SCOPE)),
    ("queue", "List pending jobs or explicitly retry/retarget a mail_id or account_id. Retarget changes saved destinations.", operations.queue,
     _parameters({**_SCOPE, "action": {"type": "string", "enum": ["list", "retry", "retarget"]}, "target": _TEXT})),
    ("feedback", "Add, list or remove explicit user corrections. Corrections guide future judgments without resending old mail.", operations.feedback,
     _parameters({"action": {"type": "string", "enum": ["add", "list", "remove"]}, "mail_id": _TEXT,
                  "feedback_id": {"type": "integer", "minimum": 1},
                  "decision": {"type": "string", "enum": ["notify", "suppress", "review"]},
                  "note": {"type": "string", "maxLength": 1000}}, ("action",))),
    ("policy", "Show policy or update explicitly requested preferences. Sender and keyword preferences are AI context.", operations.policy,
     _parameters({"changes": _parameters(_POLICY)})),
    ("doctor", "Diagnose local configuration and backlog. live=true also connects to IMAP, calls the model and sends a Telegram test.", diagnose,
     _parameters({"live": {"type": "boolean"}})),
)


def _validate(value, schema):
    kind = schema["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if type(value) is not types[kind]:
        raise HostError("invalid_tool_arguments")
    if "enum" in schema and value not in schema["enum"]:
        raise HostError("invalid_tool_arguments")
    if kind == "object":
        if set(value) - set(schema["properties"]) or set(schema.get("required", ())) - set(value):
            raise HostError("invalid_tool_arguments")
        for name, item in value.items():
            _validate(item, schema["properties"][name])
    elif kind == "array":
        for item in value:
            _validate(item, schema["items"])
    elif kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise HostError("invalid_tool_arguments")
    elif kind == "string" and len(value) > schema.get("maxLength", 10000):
        raise HostError("invalid_tool_arguments")


def register(ctx):
    onboarding = TelegramOnboarding(ctx)
    if all(callable(getattr(ctx, method, None)) for method in ("register_hook", "register_telegram_handler", "on_unload")):
        ctx.register_hook("post_gateway_admission", onboarding.on_admission)
        ctx.register_telegram_handler(onboarding.telegram_factory)
        ctx.on_unload(onboarding.unload)
    name = "mail_notifier_setup"
    description = "Open the mail connection panel in the current authenticated private Telegram chat. Takes no arguments; never accepts passwords."
    ctx.register_tool(name=name, toolset="mail_notifier", description=description,
                      handler=onboarding.open_setup_panel,
                      schema={"name": name, "description": description, "parameters": _parameters({})})

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
    for name, description, basic_handler in (
        ("mail_notifier_check", "Check new emails and send important summaries to the configured Telegram target.", check),
        ("mail_notifier_status", "Show email monitoring status and pending notifications without reading mail.", status),
    ):
        ctx.register_tool(
            name=name, toolset="mail_notifier", description=description, handler=basic_handler,
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        )
    for suffix, description, operation, parameters in _TOOLS:
        name = "mail_notifier_" + suffix
        def handler(args, _operation=operation, _parameters=parameters, **kwargs):
            try:
                _validate(args, _parameters)
                result = {"success": True, **_operation(HermesHost(ctx), **args)}
            except Exception as exc:
                code = error_code(exc)
                result = {"success": False, "error": code, "next_action": next_action(code)}
            return json.dumps(result, ensure_ascii=False)
        ctx.register_tool(name=name, toolset="mail_notifier", description=description, handler=handler,
                          schema={"name": name, "description": description, "parameters": parameters})
