"""Isolated real gateway fixture. No live Telegram, model or user profile data."""

import asyncio
import contextvars
import importlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class GatewayHarness:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".mail-telegram-contract-", dir=ROOT)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / ".hermes"
        self.beta_home = self.home / "profiles" / "beta"
        self.beta_home.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "HERMES_HOME": str(self.home),
                                          "PATH": str(Path(os.sys.executable).parent) + os.pathsep + os.defpath,
                                          "LANG": "en_US.UTF-8"}, clear=True)
        self.env.start()
        self.managers = []
        self.patches = []
        try:
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
            self.profiles = {}
            self.request_origin = False
            self.tool_results = []
            self.tool_done = asyncio.Event()
            from gateway.run import _profile_runtime_scope
            from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
            for name, home in (("default", self.home), ("beta", self.beta_home)):
                with _profile_runtime_scope(home):
                    (home / "config.yaml").write_text(
                        "model:\n  provider: openai-api\n  default: test-only-model\n"
                        "  api_mode: chat_completions\n  base_url: http://127.0.0.1:1/v1\n  context_length: 128000\n"
                        "auxiliary:\n  title_generation:\n    enabled: false\n"
                        "platform_toolsets:\n  telegram: [mail_contract]\n"
                        "display:\n  tool_progress: off\n  thinking: false\n"
                        "gateway:\n  unauthorized_dm_behavior: ignore\n", encoding="utf-8")
                    (home / ".env").write_text("TELEGRAM_ALLOWED_USERS=111\nOPENAI_API_KEY=test-only-key\n", encoding="utf-8")
                    manager = get_plugin_manager()
                    self.managers.append(manager)
                    manager._discovered = True
                    manifest = PluginManifest(name="hermes-mail-notifier", key="hermes-mail-notifier",
                                              version="0.2.0", source="project", path=str(ROOT))
                    manager._load_plugin(manifest)
                    loaded = manager._plugins[manifest.name]
                    if loaded.error:
                        raise AssertionError(loaded.error)
                    module = importlib.import_module(loaded.module.__name__ + ".mail_notifier.telegram_gateway")
                    ctx = PluginContext(manifest, manager)
                    broker = module.register_admission(ctx)
                    ctx.register_tool(name="mail_contract_origin", toolset="mail_contract", description="Contract probe",
                                      handler=self.origin_tool,
                                      schema={"name": "mail_contract_origin", "description": "Contract probe",
                                              "parameters": {"type": "object", "properties": {}}})
                    manager._hooks.setdefault("pre_gateway_dispatch", []).append(self.record_event)
                    self.profiles[name] = SimpleNamespace(manager=manager, module=module, broker=broker)
            self.broker = self.profiles["default"].broker
            self.config = GatewayConfig(multiplex_profiles=True, sessions_dir=self.home / "sessions",
                                        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="700:test-only")})
            self.runner = GatewayRunner(self.config)
            self.adapter = TelegramAdapter(self.config.platforms[Platform.TELEGRAM])
            self.adapter.gateway_runner = self.runner
            self.runner.adapters[Platform.TELEGRAM] = self.adapter
            self.runner._wire_adapter_handlers(self.adapter)
            self.patch(self.adapter, "send", self.send)
            self.patch(self.adapter, "send_typing", self.noop)
            from run_agent import AIAgent
            def model_call(agent, api_kwargs, *args, **kwargs):
                return self.model_response(api_kwargs)
            self.patch(AIAgent, "_interruptible_api_call", model_call)
            self.patch(AIAgent, "_interruptible_streaming_api_call", model_call)
            # No unexpected network operation may escape this offline fixture.
            network = patch("socket.socket.connect", side_effect=AssertionError("unexpected network"))
            network.start()
            self.patches.append(network)
            import httpx
            self.patch(httpx.Client, "send", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected HTTP")))
        except BaseException:
            self.env.stop()
            self.temp.cleanup()
            raise

    def patch(self, obj, name, value):
        handle = patch.object(obj, name, value)
        handle.start()
        self.patches.append(handle)

    def record_event(self, **kwargs):
        self.events.append(kwargs)

    async def noop(self, *args, **kwargs):
        pass

    async def send(self, *args, **kwargs):
        from gateway.platforms.base import SendResult
        self.sent.append((args, kwargs))
        return SendResult(success=True)

    def model_response(self, api_kwargs):
        from openai.types.chat import ChatCompletion
        self.model_inputs.append(json.dumps(api_kwargs, default=str))
        messages = api_kwargs.get("messages", [])
        tool = self.request_origin and not any(m.get("role") == "tool" for m in messages)
        message = {"role": "assistant", "content": None if tool else "contract complete"}
        if tool:
            available = {entry.get("function", {}).get("name") for entry in api_kwargs.get("tools", [])}
            name, arguments = "mail_contract_origin", "{}"
            if name not in available:
                name = "tool_call"
                arguments = json.dumps({"calls": [{"name": "mail_contract_origin", "arguments": {}}]})
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
        while tasks := list(getattr(self.adapter, "_background_tasks", ())):
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            await asyncio.sleep(0)

    async def close(self):
        from agent.secret_scope import set_multiplex_active
        tasks = list(getattr(self.adapter, "_background_tasks", ()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.runner._shutdown_executor(drain_timeout=2)
        for cached in self.runner._agent_cache.values():
            cached[0].close()
        for manager in reversed(self.managers):
            manager.unload()
        for handle in reversed(self.patches):
            handle.stop()
        for db in self.runner._session_db_handles.values():
            await db.close()
        self.runner.session_store.close_all_db_handles()
        from hermes_state_registry import close_all_under
        close_all_under(self.root)
        from hermes_logging import flush_log_queue, _reset_queued_handlers
        flush_log_queue()
        _reset_queued_handlers()
        set_multiplex_active(False)
        self.env.stop()
        self.temp.cleanup()
