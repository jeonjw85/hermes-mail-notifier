"""Isolated real gateway fixture. No live Telegram, model or user profile data."""

import asyncio
import contextvars
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class GatewayHarness:
    def __init__(self):
        self._cleanup = ExitStack()
        self._closed = False
        self.network_attempts = []
        self._network_lock = threading.Lock()
        # Last callback: fail even when host exception handling swallowed a guard.
        self._cleanup.callback(self._assert_no_egress)
        self.managers = []
        try:
            self._guard_network()
            self.temp = tempfile.TemporaryDirectory(prefix=".mail-telegram-contract-", dir=ROOT.parent)
            self._cleanup.callback(self.temp.cleanup)
            self.root = Path(self.temp.name).resolve()
            self.home = self.root / ".hermes"
            self.beta_home = self.home / "profiles" / "beta"
            self.beta_home.mkdir(parents=True)
            self._cleanup.enter_context(patch.dict(os.environ, {
                "HOME": str(self.root), "HERMES_HOME": str(self.home),
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                "LANG": "en_US.UTF-8"}, clear=True))
            # Registered before host imports: also runs on partial construction.
            self._cleanup.callback(self._close_host_state)
            from gateway.config import GatewayConfig, Platform, PlatformConfig
            from gateway.run import GatewayRunner
            from plugins.platforms.telegram.adapter import TelegramAdapter
            self.auth = importlib.import_module("mail_notifier.onboarding_auth")
            self.telegram = importlib.import_module("mail_notifier.telegram_gateway")
            self.effects = []
            self.events = []
            self.event_objects = []
            self.model_inputs = []
            self.sent = []
            self.native_sent = []
            self.callback_answers = []
            self.profiles = {}
            self.request_origin = False
            self.requested_tool = "mail_contract_origin"
            self.tool_results = []
            self.tool_done = asyncio.Event()
            from gateway.run import _profile_runtime_scope
            from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
            import hermes_cli.plugins as plugins
            previous_manager = plugins._plugin_manager
            def restore_manager():
                if plugins._plugin_manager in self.managers:
                    plugins._plugin_manager = previous_manager
            self._cleanup.callback(restore_manager)
            for name, home in (("default", self.home), ("beta", self.beta_home)):
                with _profile_runtime_scope(home):
                    (home / "config.yaml").write_text(
                        "model:\n  provider: openai-api\n  default: test-only-model\n"
                        "  api_mode: chat_completions\n  base_url: http://127.0.0.1:1/v1\n  context_length: 128000\n"
                        "auxiliary:\n  title_generation:\n    enabled: false\n"
                        "platform_toolsets:\n  telegram: [mail_contract, mail_notifier]\n"
                        "display:\n  tool_progress: off\n  thinking: false\n"
                        "gateway:\n  unauthorized_dm_behavior: ignore\n", encoding="utf-8")
                    (home / ".env").write_text("TELEGRAM_ALLOWED_USERS=111\nOPENAI_API_KEY=test-only-key\n", encoding="utf-8")
                    manager = get_plugin_manager()
                    self.managers.append(manager)
                    self._cleanup.callback(self._release_manager, manager)
                    manager._discovered = True
                    manifest = PluginManifest(name="hermes-mail-notifier", key="hermes-mail-notifier",
                                              version="0.2.0", source="project", path=str(ROOT))
                    manager._load_plugin(manifest)
                    loaded = manager._plugins[manifest.name]
                    if loaded.error:
                        raise AssertionError(loaded.error)
                    module = importlib.import_module(loaded.module.__name__ + ".mail_notifier.telegram_gateway")
                    ctx = PluginContext(manifest, manager)
                    controller = next(cb.__self__ for cb in manager._hooks["post_gateway_admission"]
                                      if cb.__module__.startswith(loaded.module.__name__))
                    broker = controller.broker
                    original_wait = broker.wait
                    async def short_wait(reference, *, timeout=30, wait=original_wait):
                        return await wait(reference, timeout=min(timeout, .1))
                    self.patch(broker, "wait", short_wait)
                    ctx.register_tool(name="mail_contract_origin", toolset="mail_contract", description="Contract probe",
                                      handler=self.origin_tool,
                                      schema={"name": "mail_contract_origin", "description": "Contract probe",
                                              "parameters": {"type": "object", "properties": {}}})
                    manager._hooks.setdefault("pre_gateway_dispatch", []).append(self.record_event)
                    self.profiles[name] = SimpleNamespace(manager=manager, module=module, broker=broker, controller=controller)
                    # Parse the real host locale catalog outside the event loop;
                    # the first denied-message/agent turn would otherwise block it.
                    from agent.i18n import t
                    t("gateway.unauthorized.owner_hint_generic")
            self.broker = self.profiles["default"].broker
            self.config = GatewayConfig(multiplex_profiles=True, sessions_dir=self.home / "sessions",
                                        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="700:test-only")})
            self.runner = GatewayRunner(self.config)
            self._cleanup.callback(self._close_runner)
            self.adapter = TelegramAdapter(self.config.platforms[Platform.TELEGRAM])
            self.adapter.gateway_runner = self.runner
            self.runner.adapters[Platform.TELEGRAM] = self.adapter
            self.runner._wire_adapter_handlers(self.adapter)
            self.patch(self.adapter, "send", self.send)
            self.patch(self.adapter, "send_typing", self.noop)
            from run_agent import AIAgent
            from agent import model_metadata, usage_pricing
            # There is no live model server behind our loopback RPC fake. Its
            # discovery/pricing queries are model-network boundaries too.
            self.patch(model_metadata, "detect_local_server_type", lambda *args, **kwargs: None)
            self.patch(usage_pricing, "fetch_endpoint_model_metadata", lambda *args, **kwargs: {})
            def model_call(agent, api_kwargs, *args, **kwargs):
                return self.model_response(api_kwargs)
            self.patch(AIAgent, "_interruptible_api_call", model_call)
            self.patch(AIAgent, "_interruptible_streaming_api_call", model_call)
            from telegram import Bot, User
            from telegram.ext import Application, ExtBot
            self.bot = ExtBot(token="700:test-only")
            self.bot._bot_user = User(id=700, first_name="Test bot", is_bot=True, username="mail_contract_bot")
            self.application = Application.builder().bot(self.bot).updater(None).build()
            self.application._initialized = True
            self.patch(Bot, "send_message", self.native_send)
            self.patch(ExtBot, "send_message", self.native_send)
            self.patch(Bot, "answer_callback_query", self.answer_callback)
            self.patch(ExtBot, "answer_callback_query", self.answer_callback)
            with _profile_runtime_scope(self.home):
                self.adapter._wire_plugin_handlers(self.application)
            self.native_counter = 100
        except BaseException:
            self._dispose()
            raise

    def patch(self, obj, name, value):
        self._cleanup.enter_context(patch.object(obj, name, value))

    def _deny_egress(self, operation):
        # Do not retain URLs, payloads, or credentials in failure diagnostics.
        with self._network_lock:
            self.network_attempts.append(operation)
        raise AssertionError("unexpected offline egress: " + operation)

    def _guard_network(self):
        guards = self._cleanup.enter_context(ExitStack())
        for obj, prefix, names in (
            (socket, "socket.", ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                                 "getnameinfo", "create_connection")),
            (socket.socket, "socket.socket.", ("connect", "connect_ex", "sendto")),
        ):
            for name in names:
                def deny(*args, operation=prefix + name, **kwargs):
                    self._deny_egress(operation)
                guards.enter_context(patch.object(obj, name, deny))
        # Even importing the HTTP client is inside the resolver/connection fence.
        import httpx
        def deny_http(*args, **kwargs):
            self._deny_egress("httpx.Client.send")
        async def deny_async_http(*args, **kwargs):
            self._deny_egress("httpx.AsyncClient.send")
        guards.enter_context(patch.object(httpx.Client, "send", deny_http))
        guards.enter_context(patch.object(httpx.AsyncClient, "send", deny_async_http))

    def _assert_no_egress(self):
        with self._network_lock:
            attempts = list(self.network_attempts)
        if attempts:
            raise AssertionError("unexpected offline egress: " + ", ".join(attempts))

    def _release_manager(self, manager):
        import hermes_cli.plugins as plugins
        try:
            plugins._clear_plugin_submodules(manager)
            manager.unload()
        finally:
            with plugins._plugin_managers_lock:
                for home, cached in list(plugins._plugin_managers_by_home.items()):
                    if cached is manager:
                        del plugins._plugin_managers_by_home[home]

    def _close_runner(self):
        # ExitStack runs the remaining cleanup even if one resource fails close.
        with ExitStack() as cleanup:
            cleanup.callback(self.runner.close_all_session_db_handles)
            cleanup.callback(self.runner.session_store.close_all_db_handles)
            for cached in self.runner._agent_cache.values():
                cleanup.callback(cached[0].close)
            cleanup.callback(self.runner._shutdown_executor, drain_timeout=2)

    def _close_host_state(self):
        # Do not import new host modules after an import/setup failure.
        with ExitStack() as cleanup:
            if module := sys.modules.get("agent.secret_scope"):
                cleanup.callback(module.set_multiplex_active, False)
            if module := sys.modules.get("hermes_logging"):
                cleanup.callback(module._reset_queued_handlers)
                cleanup.callback(module.flush_log_queue)
            if module := sys.modules.get("hermes_state_registry"):
                cleanup.callback(module.close_all_under, self.root)

    def _dispose(self):
        if not self._closed:
            self._closed = True
            self._cleanup.close()

    def record_event(self, **kwargs):
        self.events.append(kwargs)

    async def noop(self, *args, **kwargs):
        pass

    async def send(self, *args, **kwargs):
        from gateway.platforms.base import SendResult
        self.sent.append((args, kwargs))
        return SendResult(success=True)

    async def native_send(self, *args, **kwargs):
        from telegram import Chat, Message
        message = Message(message_id=1000 + len(self.native_sent), date=datetime.now(timezone.utc),
                          chat=Chat(id=int(kwargs["chat_id"]), type="private"), from_user=self.bot.bot,
                          text=kwargs["text"], message_thread_id=kwargs.get("message_thread_id"),
                          is_topic_message=bool(kwargs.get("message_thread_id")),
                          reply_markup=kwargs.get("reply_markup"))
        message.set_bot(self.bot)
        self.native_sent.append((message, kwargs))
        return message

    async def answer_callback(self, *args, **kwargs):
        self.callback_answers.append(kwargs)
        return True

    def button(self, label):
        for message, kwargs in reversed(self.native_sent):
            markup = kwargs.get("reply_markup")
            for row in getattr(markup, "inline_keyboard", ()):
                for button in row:
                    if button.text == label:
                        return button.callback_data, message
        raise AssertionError("Missing native button: " + label)

    async def press(self, label, **kwargs):
        token, message = self.button(label)
        return await self.dispatch_native(callback=token, reply=message, **kwargs)

    def reload_profile(self, name="default", *, rewire=True):
        """Real loader replacement; public rewire keeps the exact native client."""
        from gateway.run import _profile_runtime_scope
        profile = self.profiles[name]
        home = self.home if name == "default" else self.beta_home
        manifest = profile.controller.ctx.manifest
        with _profile_runtime_scope(home):
            profile.manager.unload()
            profile.manager._load_plugin(manifest)
            loaded = profile.manager._plugins[manifest.name]
            if loaded.error:
                raise AssertionError(loaded.error)
            controller = next(cb.__self__ for cb in profile.manager._hooks["post_gateway_admission"])
            broker = controller.broker
            # Match initial fixture setup: shorten only the native caller's wait
            # for denial, never the host authorizer or receipt creation policy.
            original_wait = broker.wait
            async def short_wait(reference, *, timeout=30):
                return await original_wait(reference, timeout=min(timeout, .1))
            self.patch(broker, "wait", short_wait)
            self.profiles[name] = SimpleNamespace(manager=profile.manager, controller=controller,
                                                 broker=broker, module=profile.module)
            if rewire:
                self.adapter.rewire_plugin_handlers()
        return controller

    def fake_mail_services(self):
        """Only replace the external service boundaries, not admission or config."""
        from tests.test_service import FakeMailbox
        from tests.support import classification_data
        self.mail_effects = []
        self.mail_scopes = []
        self.mail_passwords = {}
        self.imap_probes = []
        self.cron_paused = False
        self.delivery_failure = False
        for profile in self.profiles.values():
            module = importlib.import_module(profile.controller.__module__)
            def mailbox(account, password):
                self.imap_probes.append(account.id)
                return FakeMailbox(account)
            self.patch(module, "ImapMailbox", mailbox)
            cls = module.HermesHost
            def save_password(host, account, password):
                key = str(host.home()), account.id
                self.mail_passwords[key] = password
                self.mail_effects.append((str(host.home()), "password", threading.get_ident()))
            def get_password(host, account):
                return self.mail_passwords[(str(host.home()), account.id)]
            def schedule(host, settings, store):
                self.mail_scopes.append(host.ctx._manager.scope_key)
                self.mail_effects.append((str(host.home()), "cron", threading.get_ident()))
                store.set_meta("cron_paused", self.cron_paused)
                store.set_meta("cron_job_id", "synthetic-job")
                return "synthetic-job"
            def resume(host, job_id, store):
                self.cron_paused = False
                store.set_meta("cron_paused", False)
            def model(host, payload, **kwargs):
                self.mail_scopes.append(host.ctx._manager.scope_key)
                self.mail_effects.append((str(host.home()), "model", threading.get_ident()))
                return classification_data()
            def send(host, target, text, **kwargs):
                from mail_notifier.hermes_host import HostError
                self.mail_effects.append((str(host.home()), "send", threading.get_ident()))
                if self.delivery_failure:
                    raise HostError("delivery_failed")
                return {"success": True, "message_id": "synthetic-delivery"}
            for name, value in (("save_password", save_password), ("get_password", get_password),
                                ("install_schedule", schedule), ("resume_schedule", resume),
                                ("complete_structured", model), ("send", send),
                                ("check_environment", lambda host, settings: None),
                                ("check_compatibility", lambda host: None)):
                self.patch(cls, name, value)
            # Operator configuration is separate from the settings snapshot.
            self.patch(cls, "credential_page_url", lambda host: "https://operator.example/credential/")

    async def dispatch_native(self, *, text="/mail", callback=None, reply=None, web_app=None,
                              user_id="111", bot=None, thread_id=None, chat_type="private", route=None,
                              allowed_users="111", drain=True, language_code="en"):
        from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User, WebAppData
        from gateway.profile_routing import ProfileRoute
        from gateway.run import _profile_runtime_scope
        self.native_counter += 1
        number = self.native_counter
        bot = bot or self.bot
        self.config.profile_routes = [ProfileRoute(name="mail", platform="telegram", profile=route, chat_id=user_id)] if route else []
        (self.home / ".env").write_text(f"TELEGRAM_ALLOWED_USERS={allowed_users}\nOPENAI_API_KEY=test-only-key\n", encoding="utf-8")
        user = User(id=int(user_id), first_name="Tester", is_bot=False, language_code=language_code)
        chat = Chat(id=int(user_id) if chat_type == "private" else -123, type=chat_type)
        message = Message(message_id=number, date=datetime.now(timezone.utc), chat=chat, from_user=user,
                          text=None if web_app is not None else text, reply_to_message=reply,
                          message_thread_id=thread_id,
                          is_topic_message=thread_id is not None,
                          entities=[MessageEntity("bot_command", 0, len(text.split()[0]))] if web_app is None and text.startswith("/") else [],
                          web_app_data=WebAppData(data=web_app, button_text="secure") if web_app is not None else None)
        message.set_bot(bot)
        if callback is not None:
            query = CallbackQuery(id=str(number), from_user=user, chat_instance="test", data=callback,
                                  message=reply or message)
            query.set_bot(bot)
            update = Update(update_id=number, callback_query=query)
        else:
            update = Update(update_id=number, message=message)
        update.set_bot(bot)
        with _profile_runtime_scope(self.home):
            await self.application.process_update(update)
            if drain:
                await self.drain()
        return update

    def model_response(self, api_kwargs):
        from openai.types.chat import ChatCompletion
        self.model_inputs.append(json.dumps(api_kwargs, default=str))
        messages = api_kwargs.get("messages", [])
        tool = self.request_origin and not any(m.get("role") == "tool" for m in messages)
        message = {"role": "assistant", "content": None if tool else "contract complete"}
        if tool:
            available = {entry.get("function", {}).get("name") for entry in api_kwargs.get("tools", [])}
            name, arguments = self.requested_tool, "{}"
            if name not in available:
                name = "tool_call"
                arguments = json.dumps({"calls": [{"name": self.requested_tool, "arguments": {}}]})
            message["tool_calls"] = [{"id": "contract-call", "type": "function",
                                      "function": {"name": name, "arguments": arguments}}]
        return ChatCompletion(id="contract", object="chat.completion", created=1, model="test-only-model",
                              choices=[{"index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": message}],
                              usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})

    def origin_tool(self, args, **kwargs):
        self.tool_context = contextvars.copy_context()
        self.tool_results.append(self.auth.OriginProofs.current())
        self.loop.call_soon_threadsafe(self.tool_done.set)
        return "{}"

    def update(self, *, user_id="111", bot_id="700", message_id=42, thread_id=None, is_bot=False, chat_type="private", sender_chat=None):
        from telegram import Bot, Chat, Message, Update, User
        bot = Bot(token=f"{bot_id}:test-only")
        bot._bot_user = User(id=int(bot_id), first_name="Test bot", is_bot=True)
        user = User(id=int(user_id), first_name="Tester", is_bot=is_bot, language_code="ko") if user_id else None
        chat = Chat(id=int(user_id or "111"), type=chat_type)
        message = Message(message_id=message_id, date=datetime.now(timezone.utc), chat=chat, from_user=user,
                          sender_chat=sender_chat, text="hello", message_thread_id=thread_id)
        message.set_bot(bot)
        update = Update(update_id=message_id + 1000, message=message)
        update.set_bot(bot)
        return update, bot

    async def submit_native(self, *, user_id="111", allowed_users="111", route=None, bot_id="700", thread_id=None,
                            value="SECRET-CIPHERTEXT", broker=None):
        from gateway.profile_routing import ProfileRoute
        from gateway.run import _profile_runtime_scope
        (self.home / ".env").write_text(f"TELEGRAM_ALLOWED_USERS={allowed_users}\nOPENAI_API_KEY=test-only-key\n", encoding="utf-8")
        self.config.profile_routes = [ProfileRoute(name="mail", platform="telegram", profile=route, chat_id=user_id)] if route else []
        update, bot = self.update(user_id=user_id, bot_id=bot_id, thread_id=thread_id)
        item = self.telegram.native_input(update, bot, kind="web_app", value=value)
        if item is None:
            return None
        broker = broker or self.broker
        # Shorten only the caller's wait, not authorizer, routing or hook dispatch.
        original_wait = broker.wait
        async def wait(reference, *, timeout=30):
            return await original_wait(reference, timeout=.1)
        original_handle = self.adapter.handle_message
        async def capture(event):
            self.event_objects.append(event)
            await original_handle(event)
        with _profile_runtime_scope(self.home), patch.object(broker, "wait", wait), patch.object(self.adapter, "handle_message", capture):
            result = await self.telegram.admit_native(self.adapter, broker, item)
        if result:
            self.effects.append(result[0].to_dict())
            return result[0]
        return None

    async def submit_ordinary(self, *, route=None, bot_id="700", user_id="111", observe=True):
        from gateway.profile_routing import ProfileRoute
        from gateway.run import _profile_runtime_scope
        self.config.profile_routes = [ProfileRoute(name="mail", platform="telegram", profile=route, chat_id=user_id)] if route else []
        update, bot = self.update(user_id=user_id, bot_id=bot_id)
        self.request_origin = True
        self.loop = asyncio.get_running_loop()
        self.tool_done.clear()
        self.tool_results.clear()
        with _profile_runtime_scope(self.home):
            if observe:
                self.telegram.observe_native(self.broker, update, bot)
            await self.adapter._handle_text_message(update, SimpleNamespace(bot=bot))
            try:
                await asyncio.wait_for(self.tool_done.wait(), 3)
            except TimeoutError:
                pass
            await self.drain()
        return self.tool_results[0] if self.tool_results else None

    async def drain(self):
        while tasks := (list(getattr(self.adapter, "_background_tasks", ())) +
                        list(getattr(self.adapter, "_pending_text_batch_tasks", {}).values()) +
                        [task for p in self.profiles.values() for task in p.controller._tasks]):
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            await asyncio.sleep(0)

    async def close(self):
        if self._closed:
            return
        try:
            tasks = (list(getattr(getattr(self, "adapter", None), "_background_tasks", ())) +
                     list(getattr(getattr(self, "adapter", None), "_pending_text_batch_tasks", {}).values()))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for profile in self.profiles.values():
                profile.controller.unload()
            await asyncio.sleep(0)
            for profile in self.profiles.values():
                await profile.controller.runtime.close_profile(profile.broker.profile_home)
        finally:
            self._dispose()
