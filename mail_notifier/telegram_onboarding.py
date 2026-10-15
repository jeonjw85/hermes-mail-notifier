"""Authenticated first connection. No SDK, host, storage or tasks at registration.

Native callbacks only escrow input and obtain a fresh receipt through the public
gateway. Work, settings, crypto and SQLite run in the receipt's worker context.
Only the opaque reference can reach normal gateway/agent processing.
"""

# Host and Telegram dependencies are supplied by Hermes, and imported lazily.
# pyright: reportMissingImports=false
import asyncio
import json
import re
import time
from contextlib import closing
from collections.abc import Coroutine

from .config import settings_fingerprint
from .credential_session import CredentialBinding, CredentialSessions, credential_url
from .hermes_host import HermesHost, HostError
from .i18n import resolve_ui_language, text
from .imap_client import ImapMailbox
from .onboarding import OnboardingService
from .onboarding_auth import NativeInput, OriginProofs, _shared, acquire_broker, owner_key
from .onboarding_runtime import OnboardingRuntime, bind_operation, check_active
from .service import error_code, get_status
from .telegram_gateway import admit_native, native_input, observe_native, on_admission
from . import telegram_views as views


def _text_parts(value):
    part, units = [], 0
    for char in value:
        size = 2 if ord(char) > 0xffff else 1
        if units + size > 3500:
            yield "".join(part)
            part, units = [], 0
        part.append(char)
        units += size
    yield "".join(part)


class _Profile:
    def __init__(self, host, broker):
        host.check_onboarding_compatibility()
        self.host = host
        self.broker = broker
        self.home = str(host.home().resolve())
        self.service = OnboardingService(host, mailbox_factory=ImapMailbox, check_active=check_active)
        self.sessions = CredentialSessions()
        self.challenges = {}

    def prune(self):
        check_active()
        self.service.expire()
        self.sessions.prune()
        for key, record in list(self.challenges.items()):
            if time.time() >= int(record["challenge"].expires_at):
                del self.challenges[key]
        views.prune()
        if self.broker is not None:
            self.broker.prune()

    def close(self):
        self.service.close()
        self.sessions.close()
        self.challenges.clear()
        views.clear_profile(self.home)


class TelegramOnboarding:
    def __init__(self, ctx):
        self.ctx = ctx
        self.runtime = OnboardingRuntime()
        self._broker = None
        self._closed = False
        self._tasks = set()
        self._task_homes = {}
        self._loop = None

    @property
    def broker(self):
        if self._closed:
            raise HostError("setup_work_cancelled")
        if self._broker is None:
            self._broker = acquire_broker()
            self.runtime.open_profile(self._broker.profile_home)
            with _shared().lock:
                views._ui()["profiles"][self._broker.profile_home] = self
        return self._broker

    async def on_admission(self, *, session_key: str, platform: str, source: dict, message_id: str, text: str, **kwargs):
        if self._closed:
            return None
        self._loop = asyncio.get_running_loop()
        return on_admission(self.broker, platform=platform, source=source, message_id=message_id,
                            session_key=session_key, text=text)

    def _controller(self, home):
        with _shared().lock:
            controller = views._ui()["profiles"].get(home)
        if controller is None:
            raise HostError("incompatible_hermes_onboarding")
        if controller._closed:
            raise HostError("setup_work_cancelled")
        return controller

    def _resource(self, receipt) -> _Profile:
        home = receipt.owner.profile_home
        controller = self._controller(home)
        return self.runtime.resource(home, lambda: controller._create_profile(home))

    def _create_profile(self, home):
        # Invoke on the routed registration, not the receiving transport's
        # profile-specific module/PluginContext (dispatch_tool has a fixed scope).
        host = HermesHost(self.ctx)
        if str(host.home().resolve()) != home:
            raise HostError("setup_not_found")
        return _Profile(host, self._broker)

    def _read(self, resource, draft_id, owner):
        resource.service.expire()
        with closing(resource.host.open_setup_store()) as store:
            return store.get(draft_id, owner)

    def _view(self, resource, draft, *, page=0, resume_confirmation=False):
        preview = resource.service.preview(draft.id, draft.owner)
        if preview["revision"] != draft.revision:
            draft = self._read(resource, draft.id, draft.owner)
        settings = resource.host.load_settings()
        if preview.get("current_settings_fingerprint") and preview["current_settings_fingerprint"] != settings_fingerprint(settings):
            raise HostError("setup_changed_retry")
        status = get_status(resource.host) if draft.form.get("action") == "recovery" else None
        result = views.panel(draft, settings, language=draft.candidate.ui_language, page=page,
                             resume_confirmation=resume_confirmation, status=status,
                             current_fingerprint=preview.get("current_settings_fingerprint"))
        result["completed_steps"] = preview["completed_steps"]
        if draft.state in {"committing", "scheduled", "repair_required", "needs_review", "completed"}:
            result["text"] += "\n" + text("telegram.evidence", draft.candidate.ui_language,
                                           saved=text("telegram.yes" if "settings" in preview["completed_steps"] else "telegram.no", draft.candidate.ui_language),
                                           steps=", ".join(preview["completed_steps"]), next=preview.get("next_action", "/mail"))
        result["text"] = views.bounded(result["text"], 3500)
        with _shared().lock:
            key = owner_key(draft.owner)
            views._ui()["languages"][key] = {"owner": key, "language": draft.candidate.ui_language, "expires": time.time() + 86400}
            waiting_maps = [views._ui()["waiting"]]
            transport = views._ui()["transports"].get(draft.owner.bot_id)
            if transport is not None:
                waiting_maps.append(transport["waiting"])
            for waiting in waiting_maps:
                if draft.state in {"awaiting_secret", "needs_secret", "validating"} or draft.form.get("action") == "rotate":
                    waiting[key] = {"owner": key, "expires": time.time() + 86400}
                else:
                    waiting.pop(key, None)
        return result

    async def handle(self, receipt, item):
        """Internal receipt consumer, never a model-facing mutation API."""
        self._loop = asyncio.get_running_loop()
        try:
            reference = None
            if (owner_key(receipt.owner)[1:] != ("telegram", item.bot_id, item.user_id, item.chat_id, item.thread_id)
                    or receipt.message_id != item.message_id):
                raise HostError("setup_changed_retry")
            if item.kind == "callback":
                action = views.peek_action(item.value, receipt.owner)
                if action["action"] == "cancel":
                    self.runtime.interrupt(receipt, action["draft_id"], action["revision"])
                else:
                    reference = action["draft_id"], action["revision"]
            controller = self._controller(receipt.owner.profile_home)
            return await self.runtime.submit(receipt, lambda: controller._safe_work(receipt, item), reference=reference)
        except Exception as exc:
            return self._error(exc, item.language_code, receipt.owner)

    def _safe_work(self, receipt, item):
        try:
            return self._work(receipt, item)
        except Exception as exc:
            return self._error(exc, item.language_code, receipt.owner)

    def _error(self, exc, detected, owner):
        code = error_code(exc)
        with _shared().lock:
            saved = views._ui()["languages"].get(owner_key(owner), {}).get("language", "")
        language = resolve_ui_language(saved=saved, detected=detected)
        return {"text": "Mail Notifier\n" + text("telegram.stale" if code in {"setup_changed_retry", "setup_not_found"}
                                                else "telegram.error", language, code=code),
                "buttons": [], "input_field": None, "error": code, "language": language}

    def _work(self, receipt, item):
        check_active()
        owner = receipt.owner
        if (owner_key(owner)[1:] != ("telegram", item.bot_id, item.user_id, item.chat_id, item.thread_id)
                or receipt.message_id != item.message_id):
            raise HostError("setup_changed_retry")
        resource = self._resource(receipt)
        resource.prune()
        service = resource.service
        if item.kind == "callback":
            action = views.take_action(item.value, owner)
            draft = self._read(resource, action["draft_id"], owner)
            if draft.revision != action["revision"]:
                raise HostError("setup_changed_retry")
            operation, values = action["action"], action["values"]
            if operation == "page":
                return self._view(resource, draft, page=values["page"])
            if operation == "provider" and draft.form.get("action") == "connection_provider":
                draft = service.change(draft.id, owner, draft.revision, action="edit",
                                       values={"account_id": draft.form["account_id"], "provider": values["provider"]})
            elif operation in {"provider", "back", "add"}:
                form = {"action": "connect", "input_field": "", "prompt_message_id": "", "provider": ""}
                form["account_id"] = ""
                if operation == "provider":
                    form.update(provider=values["provider"], input_field="username")
                elif operation == "back":
                    form["action"] = ""
                draft = service.change(draft.id, owner, draft.revision, action="form", values=form)
            elif operation in {"account", "menu", "input", "rotate"}:
                form = {"input_field": "", "prompt_message_id": ""}
                if operation == "account":
                    if values["account_id"] not in {a.id for a in draft.candidate.accounts}:
                        raise HostError("account_not_found")
                    form.update(action="account", account_id=values["account_id"])
                elif operation == "menu":
                    if values["menu"] not in {"settings", "schedule", "ui_language", "language", "policy", "recovery", "connection_provider"}:
                        raise HostError("invalid_setup_action")
                    form["action"] = values["menu"]
                elif operation == "rotate":
                    form["action"] = "rotate"
                else:
                    if values["field"] not in {"label", "username", "mailbox", *views.EDIT_SETTINGS}:
                        raise HostError("invalid_setup_action")
                    form["input_field"] = values["field"]
                draft = service.change(draft.id, owner, draft.revision, action="form", values=form)
            elif operation in {"setting", "clear", "current_target", "test", "review"}:
                if operation == "setting":
                    changes = {values["field"]: values["value"]}
                elif operation == "clear":
                    field = values["field"]
                    if field not in views.EDIT_SETTINGS:
                        raise HostError("invalid_setup_action")
                    changes = {field: "" if field == "importance_instructions" else ()}
                elif operation == "current_target":
                    changes = {"target": "telegram:" + owner.chat_id + (":" + owner.thread_id if owner.thread_id else "")}
                else:
                    changes = {"ui_language": draft.candidate.ui_language}
                draft = service.change(draft.id, owner, draft.revision, action="settings", values=changes)
            elif operation in {"disable", "enable"}:
                draft = service.change(draft.id, owner, draft.revision, action=operation, values={"account_id": draft.form["account_id"]})
                if operation == "enable" and draft.state == "awaiting_secret":
                    bind_operation(draft.id, draft.revision)
                    draft = service.validate_password(draft.id, owner, draft.revision, "")
            elif operation == "language":
                changes = {"ui_language": values["language"]}
                if not draft.original_saved:
                    changes["language"] = values["language"]
                draft = service.change(draft.id, owner, draft.revision, action="settings", values=changes)
                if not draft.candidate.accounts:
                    # Initial locale selection stays inside the connection flow.
                    draft = service.change(draft.id, owner, draft.revision, action="form", values={})
            elif operation == "cancel":
                draft = service.cancel(draft.id, owner, draft.revision)
                resource.sessions.invalidate(draft.id, owner)
            elif operation == "resume":
                if draft.state == "scheduled":
                    return self._view(resource, draft, resume_confirmation=True)
                draft = service.change(draft.id, owner, draft.revision, action="form", values={"action": "resume"})
                draft = service.change(draft.id, owner, draft.revision, action="settings", values={"ui_language": draft.candidate.ui_language})
            elif operation == "recovery_back":
                return self._view(resource, draft)
            elif operation in {"confirm", "confirm_resume", "repair"}:
                bind_operation(draft.id, draft.revision)
                if operation == "confirm" or (operation == "confirm_resume" and draft.state == "awaiting_confirmation"):
                    service.commit(draft.id, owner, draft.revision, resume_cron=operation == "confirm_resume")
                else:
                    service.repair(draft.id, owner, draft.revision, step="cron" if operation == "confirm_resume" else values["step"],
                                   resume_cron=operation == "confirm_resume")
                draft = self._read(resource, draft.id, owner)
            elif operation == "acknowledge":
                draft = service.acknowledge(draft.id, owner, draft.revision, current_fingerprint=values["current_fingerprint"])
            elif operation in {"secure", "guide"}:
                view = self._view(resource, draft)
                if operation == "guide":
                    return view
                if draft.state not in {"awaiting_secret", "needs_secret"} and draft.form.get("action") != "rotate":
                    raise HostError("setup_changed_retry")
                base = resource.host.credential_page_url()
                if not base:
                    view["text"] += "\n" + text("telegram.url_unavailable", "en") + "\n" + text("telegram.url_unavailable", "ko")
                    return view
                account = next(a for a in draft.candidate.accounts if a.id == draft.form.get("account_id"))
                binding = CredentialBinding(owner, draft.id, draft.revision)
                challenge = resource.sessions.issue(binding)
                for key, record in list(resource.challenges.items()):
                    if record["binding"].draft_id == draft.id and owner_key(record["binding"].owner) == owner_key(owner):
                        del resource.challenges[key]
                resource.challenges[challenge.session_id] = {"binding": binding, "challenge": challenge, "accepted": False}
                view["web_app_url"] = credential_url(base, challenge, provider=account.provider,
                                                      account_label=account.username, ui_language=draft.candidate.ui_language)
                return view
            else:
                raise HostError("invalid_setup_action")
        elif item.kind in {"start", "password_text"}:
            draft = service.start(owner, detected_language=item.language_code)
        elif item.kind in {"reply", "oversized_text"}:
            draft = service.start(owner, detected_language=item.language_code)
            if item.kind == "oversized_text":
                view = self._view(resource, draft)
                view["text"] = views.bounded(view["text"] + "\n" + text("telegram.input_limit", view["language"]), 3500)
                return view
            reply = json.loads(item.value)
            field = draft.form.get("input_field")
            if (draft.state != "draft" or not field
                    or draft.form.get("prompt_message_id") != reply.get("prompt_message_id")):
                raise HostError("setup_changed_retry")
            value = reply.get("text", "")
            if not isinstance(value, str):
                raise HostError("invalid_settings")
            if len(value.encode("utf-8")) > 16 * 1024:
                view = self._view(resource, draft)
                view["text"] += "\n" + text("telegram.input_limit", view["language"])
                return view
            if field == "username" and (not re.fullmatch(r"[^\s@\x00-\x1f]{1,128}@[^\s@\x00-\x1f]{1,128}\.[^\s@\x00-\x1f]{1,63}", value) or len(value) > 254):
                raise HostError("invalid_settings")
            if field in views.EDIT_SETTINGS:
                value = value if field == "importance_instructions" else tuple(v.strip() for v in re.split(r"[,\n]", value) if v.strip())
                draft = service.change(draft.id, owner, draft.revision, action="settings", values={field: value})
            elif field in {"label", "username", "mailbox"}:
                values = {field: value}
                if draft.form.get("account_id"):
                    values["account_id"] = draft.form["account_id"]
                else:
                    values.update(provider=draft.form["provider"], label=value.split("@", 1)[0])
                draft = service.change(draft.id, owner, draft.revision, action="edit" if "account_id" in values else "add", values=values)
            else:
                raise HostError("invalid_setup_action")
        elif item.kind == "web_app":
            wire = None
            try:
                wire = json.loads(item.value)
            except (ValueError, RecursionError):
                pass
            if type(wire) is not dict or not isinstance(wire.get("session_id"), str):
                resource.sessions.reject(owner)
            record = resource.challenges.get(wire.get("session_id"))
            if record is None or owner_key(record["binding"].owner) != owner_key(owner):
                resource.sessions.reject(owner)
            binding = record["binding"]
            draft = self._read(resource, binding.draft_id, owner)
            if draft.revision != binding.revision and not record["accepted"]:
                raise HostError("setup_changed_retry")
            accepted = resource.sessions.accept(item.value, binding, update_key=item.update_key)
            if accepted.duplicate:
                return {**self._view(resource, draft), "duplicate": True}
            record["accepted"] = True
            if draft.revision != binding.revision or accepted.password is None:
                raise HostError("setup_changed_retry")
            bind_operation(draft.id, draft.revision)
            draft = service.validate_password(draft.id, owner, draft.revision, accepted.password)
        else:
            raise HostError("invalid_setup_action")
        return self._view(resource, draft)

    async def bind_prompt(self, receipt, view, message_id):
        def bind():
            resource = self._resource(receipt)
            draft = self._read(resource, view["draft_id"], receipt.owner)
            if draft.revision != view["revision"] or draft.form.get("input_field") != view.get("input_field"):
                raise HostError("setup_changed_retry")
            draft = resource.service.change(draft.id, receipt.owner, draft.revision, action="form",
                                            values={"prompt_message_id": str(message_id)})
            return self._view(resource, draft)
        return await self.runtime.submit(receipt, bind)

    def open_setup_panel(self, args: dict, **kwargs) -> str:
        if type(args) is not dict or args:
            return json.dumps({"success": False, "error": "invalid_tool_arguments"})
        try:
            return self._open_setup_panel()
        except Exception:
            return json.dumps({"success": False, "error": "setup_origin_unavailable", "next_action": "/mail",
                               "message": text("telegram.entry", "ko") + "\n" + text("telegram.entry", "en")}, ensure_ascii=False)

    def _open_setup_panel(self):
        receipt = OriginProofs.current()
        if receipt is None or self._closed:
            return json.dumps({"success": False, "error": "setup_origin_unavailable", "next_action": "/mail",
                               "message": text("telegram.entry", "ko") + "\n" + text("telegram.entry", "en")}, ensure_ascii=False)
        with _shared().lock:
            transport = views._ui()["transports"].get(receipt.owner.bot_id)
        if transport is None or transport["controller"]._closed or transport["loop"].is_closed():
            return json.dumps({"success": False, "error": "setup_origin_unavailable", "next_action": "/mail"})
        item = NativeInput("start", receipt.owner.bot_id, receipt.owner.user_id, receipt.owner.chat_id,
                           receipt.owner.thread_id, receipt.message_id, "origin:" + receipt.message_id,
                           OriginProofs.language_code(receipt), "")
        def schedule():
            self._spawn(self._display(receipt, item, transport["bot"], transport["sdk"]))
        transport["loop"].call_soon_threadsafe(schedule)
        return json.dumps({"success": True, "status": "panel_opening"})

    def _spawn(self, coro: Coroutine):
        if self._closed:
            coro.close()
            return
        task = asyncio.create_task(coro, name="mail-notifier-native")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: self._task_homes.pop(t, None))
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

    async def _display(self, receipt, item, bot, sdk):
        self._task_homes[asyncio.current_task()] = receipt.owner.profile_home
        if self.runtime.is_busy(receipt.owner.profile_home) or item.kind == "web_app":
            await bot.send_message(chat_id=receipt.owner.chat_id,
                                   message_thread_id=int(receipt.owner.thread_id) if receipt.owner.thread_id else None,
                                   text=text("telegram.queued", resolve_ui_language(detected=item.language_code)),
                                   parse_mode=None, disable_web_page_preview=True)
        view = await self.handle(receipt, item)
        if self._closed:
            return
        await self._send_view(receipt, view, bot, sdk)

    async def _send_view(self, receipt, view, bot, sdk):
        ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo = sdk
        kwargs = {"chat_id": receipt.owner.chat_id, "message_thread_id": int(receipt.owner.thread_id) if receipt.owner.thread_id else None,
                  "text": view["text"], "parse_mode": None, "disable_web_page_preview": True,
                  "connect_timeout": 10, "read_timeout": 30, "write_timeout": 30}
        if view.get("web_app_url"):
            kwargs["reply_markup"] = ReplyKeyboardMarkup([[KeyboardButton(text("telegram.secure", view["language"]),
                                                                          web_app=WebAppInfo(view["web_app_url"]))]], resize_keyboard=True, one_time_keyboard=True)
        elif view.get("input_field"):
            kwargs["reply_markup"] = ForceReply(selective=True)
        else:
            # Remove any secure-input reply keyboard before displaying a preview.
            await bot.send_message(**{**kwargs, "text": "Mail Notifier", "reply_markup": ReplyKeyboardRemove()})
            kwargs["reply_markup"] = InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], callback_data=b["callback_data"])] for b in view["buttons"]])
        parts = list(_text_parts(kwargs["text"]))
        for part in parts[:-1]:
            await bot.send_message(**{**kwargs, "text": part, "reply_markup": None})
        sent = await bot.send_message(**{**kwargs, "text": parts[-1]})
        if view.get("input_field"):
            bound = await self.bind_prompt(receipt, view, sent.message_id)
            await bot.send_message(**{**kwargs, "text": "Mail Notifier", "reply_markup": InlineKeyboardMarkup([
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])] for b in bound["buttons"]])})

    def telegram_factory(self, native, adapter) -> None:
        from hermes_cli.plugins import get_plugin_manager
        _install_intake(native, adapter, get_plugin_manager(), self.ctx.manifest.name)

    def unload(self):
        if self._closed:
            return
        self._closed = True
        home = self._broker.profile_home if self._broker is not None else None
        revoked = None
        if self._broker is not None:
            self._broker.close()
            revoked = self.runtime.revoke_profile(home)
            views.clear_profile(home)
        with _shared().lock:
            for bot_id, record in list(views._ui()["transports"].items()):
                if record["controller"] is self:
                    del views._ui()["transports"][bot_id]
            if home is not None and views._ui()["profiles"].get(home) is self:
                del views._ui()["profiles"][home]
        def cancel():
            for task in self._tasks:
                if self._task_homes.get(task) in {None, home}:
                    task.cancel()
            if home is not None:
                asyncio.create_task(self.runtime.close_profile(home, profile=revoked), name="mail-notifier-unload")
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(cancel)


def _install_intake(native, adapter, manager, plugin_name):
    """Client-lifetime guard: host rewire deduplicates factories across reloads.

    Capture no registration, broker or profile resource. Resolve the live bound
    factory through the public host registry on each update; an inactive plugin
    still consumes its stale prompts/credentials but can start no work.
    """
    from telegram import Update, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
    from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters
    # Only this client's expiring identity markers survive profile unload. They
    # contain no authority, keys, input content or registration/module objects.
    waiting = {}
    sdk = ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo

    def is_waiting(identity):
        with _shared().lock:
            for owner, record in list(waiting.items()):
                if time.time() >= record["expires"]:
                    del waiting[owner]
            return any(owner[2:] == identity for owner in waiting)

    def current():
        for factory, name in manager.get_platform_handler_factories("telegram"):
            if name == plugin_name and getattr(factory, "__name__", "") == "telegram_factory":
                controller = getattr(factory, "__self__", None)
                if controller is not None and not controller._closed:
                    return controller
        return None

    class PluginInput(filters.MessageFilter):
        def filter(self, message):
            web = message.web_app_data
            if web is not None:
                marker = bool(re.search(r'"kind"\s*:\s*"mail_notifier_credential"', web.data[:4096]))
                if len(web.data.encode()) > 4096:
                    return marker
                try:
                    return json.loads(web.data).get("kind") == "mail_notifier_credential"
                except (ValueError, AttributeError, TypeError):
                    return marker
            reply = message.reply_to_message
            if (reply is not None and reply.from_user is not None and reply.from_user.is_bot
                    and str(reply.from_user.id) == str(native.bot.id) and (reply.text or "").startswith("Mail Notifier\n")):
                return True
            if message.text and not message.text.startswith("/") and message.from_user and message.chat.type == "private":
                identity = (str(native.bot.id), str(message.from_user.id), str(message.chat.id), str(message.message_thread_id or ""))
                return is_waiting(identity)
            return False

    async def observe(update, context):
        controller = current()
        if controller is None:
            return
        controller._loop = asyncio.get_running_loop()
        observe_native(controller.broker, update, context.bot)
        with _shared().lock:
            views._ui()["transports"][str(context.bot.id)] = {"controller": controller, "bot": context.bot, "loop": controller._loop, "sdk": sdk,
                                                             "waiting": waiting}

    async def receive(update, context):
        query, message = update.callback_query, update.effective_message
        if query is not None:
            await query.answer()
        # Resolve AFTER the answer await: unload may have revoked that generation.
        controller = current()
        if controller is None or message is None:
            raise ApplicationHandlerStop
        if update.effective_chat.type != "private":
            await context.bot.send_message(chat_id=message.chat_id, message_thread_id=message.message_thread_id,
                                           text=text("telegram.group", resolve_ui_language(detected=getattr(update.effective_user, "language_code", ""))),
                                           parse_mode=None, disable_web_page_preview=True)
            raise ApplicationHandlerStop
        if query:
            kind, value = "callback", query.data
        elif message.web_app_data:
            kind, value = "web_app", message.web_app_data.data
        elif (message.text or "").split(" ", 1)[0].split("@", 1)[0] == "/mail":
            kind, value = "start", ""
        else:
            reply = message.reply_to_message
            # Retained non-secret markers also discard text during a reload gap.
            identity = (str(context.bot.id), str(update.effective_user.id), str(message.chat_id), str(message.message_thread_id or ""))
            if is_waiting(identity):
                kind, value = "password_text", ""
            else:
                if len((message.text or "").encode("utf-8")) > 16 * 1024:
                    kind, value = "oversized_text", ""
                else:
                    kind, value = "reply", json.dumps({"prompt_message_id": str(reply.message_id) if reply else "", "text": message.text or ""}, ensure_ascii=False)
        item = native_input(update, context.bot, kind=kind, value=value)
        if item is not None:
            async def admitted():
                try:
                    result = await admit_native(adapter, controller.broker, item)
                    if result is not None and not controller._closed:
                        await controller._display(result[0], result[1], context.bot, sdk)
                    elif not controller._closed:
                        await context.bot.send_message(chat_id=item.chat_id, text=text("telegram.retry", resolve_ui_language(detected=item.language_code)),
                                                       parse_mode=None, disable_web_page_preview=True)
                except (HostError, ValueError):
                    return
            controller._spawn(admitted())
        raise ApplicationHandlerStop

    for handler, group in ((TypeHandler(Update, observe), -1), (CommandHandler("mail", receive), 0),
                           (CallbackQueryHandler(receive, pattern=r"^mn:[A-Za-z0-9_-]+$"), 0),
                           (MessageHandler(PluginInput() & ~filters.COMMAND, receive), 0)):
        native.add_handler(handler, group=group)
