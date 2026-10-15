"""Pinned, offline Hermes admission contracts; opt-in never masks a missing host."""

import asyncio
import contextvars
import importlib
import json
import os
import sys
import time
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("HERMES_CONTRACT") == "1", "set HERMES_CONTRACT=1 with pinned Hermes")
class TelegramAdmissionContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.telegram_support import GatewayHarness
        self.harness = GatewayHarness()
        self.addAsyncCleanup(self.harness.close)

    async def test_denied_sender_never_receives_an_admission_receipt(self):
        with self.assertLogs("gateway.run", "WARNING") as logs:
            result = await self.harness.submit_native(user_id="222", allowed_users="111")
            await self.harness.drain()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("Unauthorized user (ignored):", logs.output[0])
        self.assertIn("unrecognized telegram user 222", logs.output[0])
        self.assertIsNone(result)
        self.assertEqual(self.harness.effects, [])

    async def test_routed_profile_and_receiving_bot_survive_real_loader(self):
        from hermes_constants import get_hermes_home
        receipt = await self.harness.submit_native(user_id="111", route="beta", bot_id="700")
        self.assertEqual(receipt.owner.profile_home, str(self.harness.beta_home))
        self.assertEqual(receipt.owner.bot_id, "700")
        self.assertEqual(receipt.context.run(get_hermes_home), self.harness.beta_home)

    async def test_private_topic_identity_is_not_dropped(self):
        receipt = await self.harness.submit_native(route="beta", thread_id=9)
        self.assertEqual(receipt.owner.thread_id, "9")

    async def test_copied_reference_with_identical_ids_has_no_transport_proof(self):
        from gateway.platforms.event import MessageEvent
        from gateway.run import _profile_runtime_scope
        update, bot = self.harness.update()
        item = self.harness.telegram.native_input(update, bot, kind="web_app", value="SECRET-CIPHERTEXT")
        reference = self.harness.broker.offer(item, now=time.monotonic())
        with _profile_runtime_scope(self.harness.home):
            source = self.harness.adapter.build_source(chat_id="111", user_id="111", scope_id="700", message_id="42")
            await self.harness.adapter.handle_message(MessageEvent(text="mail-notifier-input:" + reference,
                                                                   source=source, message_id="42"))
            await self.harness.drain()
        self.assertIsNone(await self.harness.broker.wait(reference, timeout=.01))
        self.assertEqual(self.harness.effects, [])

    async def test_fail_open_hook_paths_do_not_release_native_input_or_leak_value(self):
        manager = self.harness.profiles["default"].manager
        original = list(manager._hooks["post_gateway_admission"])
        def broken(**kwargs):
            raise RuntimeError("intentional admission failure")
        async def slow(**kwargs):
            await asyncio.sleep(1)
            return await original[0](**kwargs)
        config = self.harness.home / "config.yaml"
        config.write_text(config.read_text() + "plugins:\n  hook_callback_timeout: 0.01\n", encoding="utf-8")
        for callbacks in ([], [broken], [slow]):
            with self.subTest(callbacks=[c.__name__ for c in callbacks]):
                manager._hooks["post_gateway_admission"] = callbacks
                self.assertIsNone(await self.harness.submit_native())
                await self.harness.drain()
        self.assertEqual(self.harness.effects, [])
        captured = repr(self.harness.events) + repr(self.harness.model_inputs)
        self.assertNotIn("SECRET-CIPHERTEXT", captured)
        self.assertTrue(self.harness.model_inputs)
        for home in (self.harness.home, self.harness.beta_home):
            for path in home.rglob("*.log"):
                self.assertNotIn("SECRET-CIPHERTEXT", path.read_text())

    async def test_busy_and_paused_paths_have_no_receipt(self):
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(self.harness.home):
            source = self.harness.adapter.build_source(chat_id="111", user_id="111", scope_id="700")
            key = self.harness.runner._session_key_for_source(source)
            state = self.harness.runner._session_state(key)
            state.turn.agent = SimpleNamespace(interrupt=lambda *args: None)
            self.assertIsNone(await self.harness.submit_native())
            state.turn.agent = None
        (self.harness.home / "ESTOP").touch()
        self.assertIsNone(await self.harness.submit_native())
        self.assertEqual(self.harness.effects, [])

    async def test_reference_event_is_secret_free_and_has_no_raw_sdk_payload(self):
        self.assertIsNotNone(await self.harness.submit_native())
        event = self.harness.event_objects[0]
        self.assertTrue(event.text.startswith("mail-notifier-input:"))
        self.assertIsNone(event.raw_message)
        self.assertFalse(event.internal)
        self.assertFalse(event.allow_gateway_control)
        self.assertEqual(self.harness.model_inputs, [])
        self.assertNotIn("SECRET-CIPHERTEXT", repr(event))

    async def test_sdk_bot_anonymous_group_and_missing_identities_are_rejected(self):
        from telegram import Chat
        for changes in ({"is_bot": True}, {"user_id": ""}, {"chat_type": "group"},
                        {"sender_chat": Chat(id=-1, type="channel")}):
            with self.subTest(changes=list(changes)):
                update, bot = self.harness.update(**changes)
                self.assertIsNone(self.harness.telegram.native_input(update, bot, kind="web_app", value="secret"))

    async def test_real_profile_namespaces_share_mailbox_but_not_unload_leases(self):
        alpha = self.harness.profiles["default"]
        beta = self.harness.profiles["beta"]
        alpha_auth = importlib.import_module(alpha.module.__package__ + ".onboarding_auth")
        beta_auth = importlib.import_module(beta.module.__package__ + ".onboarding_auth")
        self.assertIsNot(alpha_auth.SetupOwner, beta_auth.SetupOwner)
        self.assertEqual(alpha_auth.owner_key(beta_auth.SetupOwner(str(self.harness.beta_home), "cli")),
                         (str(self.harness.beta_home), "cli", "", "", "", ""))
        self.assertIs(alpha.broker._state, beta.broker._state)
        update, bot = self.harness.update(message_id=43)
        item = alpha.module.native_input(update, bot, kind="web_app", value="SECRET-CIPHERTEXT")
        reference = beta.broker.offer(item, now=time.monotonic())
        alpha.manager.unload()
        self.assertIn(reference, beta.broker._state.records)
        receipt = await self.harness.submit_native(route="beta", broker=beta.broker)
        self.assertEqual(receipt.owner.profile_home, str(self.harness.beta_home))

    async def test_origin_matches_real_host_tool_context_and_rejects_non_live_calls(self):
        receipt = await self.harness.submit_ordinary(route="beta", bot_id="700")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.owner.profile_home, str(self.harness.beta_home))
        self.assertEqual(receipt.owner.bot_id, "700")
        self.assertEqual(receipt.message_id, "42")
        auth = self.harness.auth
        self.assertIsNone(auth.OriginProofs.current())
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": receipt.session_key, "HERMES_SESSION_MESSAGE_ID": "42",
                                     "HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "111"}):
            self.assertIsNone(contextvars.Context().run(auth.OriginProofs.current))
        saved = self.harness.tool_context
        def check_changes():
            from agent.delegation_context import delegated_child_context
            from gateway.session_context import _SESSION_MESSAGE_ID, _SESSION_KEY, _SESSION_PLATFORM, _CRON_SESSION
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            self.assertIsNotNone(auth.OriginProofs.current())
            for var, value in ((_SESSION_MESSAGE_ID, "43"), (_SESSION_KEY, "other"),
                               (_SESSION_PLATFORM, "cli"), (_CRON_SESSION, "1")):
                token = var.set(value)
                try:
                    self.assertIsNone(auth.OriginProofs.current())
                finally:
                    var.reset(token)
            token = set_hermes_home_override(str(self.harness.home))
            try:
                self.assertIsNone(auth.OriginProofs.current())
            finally:
                reset_hermes_home_override(token)
            with delegated_child_context():
                self.assertIsNone(auth.OriginProofs.current())
            # The same user/chat/message numbers can exist on another bot. An
            # observation alone must not borrow a proof issued for the first bot.
            observation = self.harness.broker._state.observation
            previous = observation.get()
            update, bot = self.harness.update(bot_id="701")
            self.harness.telegram.observe_native(self.harness.broker, update, bot)
            try:
                self.assertIsNone(auth.OriginProofs.current())
            finally:
                observation.set(previous)
            now = time.monotonic()
            with patch.object(auth, "time", SimpleNamespace(monotonic=lambda: now + 300)):
                self.assertIsNone(auth.OriginProofs.current())
        saved.run(check_changes)

    async def test_ordinary_messages_need_sdk_observation_and_denied_senders_get_no_origin(self):
        self.assertIsNone(await self.harness.submit_ordinary(observe=False))
        self.assertEqual(len(self.harness.tool_results), 1)
        with self.assertLogs("plugins.platforms.telegram.adapter", "WARNING") as logs:
            self.assertIsNone(await self.harness.submit_ordinary(user_id="222"))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("Blocked unauthorized user 222 in chat 222", logs.output[0])

    async def test_shared_slot_abi_mismatch_fails_closed(self):
        with patch.dict(sys.modules, {"_hermes_mail_notifier_onboarding_v1": SimpleNamespace(ABI=2)}):
            with self.assertRaises(RuntimeError):
                self.harness.auth.acquire_broker()

    async def test_public_loader_wires_native_mail_and_denies_before_panel_or_db(self):
        with self.assertLogs("gateway.run", "WARNING"):
            await self.harness.dispatch_native(user_id="222")
        self.assertFalse([kwargs for _, kwargs in self.harness.native_sent if kwargs.get("reply_markup")])
        self.assertFalse(list(self.harness.home.rglob("setup.db")))
        await self.harness.dispatch_native(route="beta", thread_id=9)
        panels = [kwargs for _, kwargs in self.harness.native_sent if kwargs.get("reply_markup")]
        self.assertTrue(panels)
        self.assertEqual(panels[-1]["chat_id"], "111")
        self.assertEqual(panels[-1]["message_thread_id"], 9)
        self.assertTrue(list(self.harness.beta_home.rglob("setup.db")))
        self.assertFalse([p for p in self.harness.home.rglob("setup.db") if not p.is_relative_to(self.harness.beta_home)])
        self.assertEqual(self.harness.model_inputs, [])

    async def connected_preview(self, *, route="beta", thread_id=9, submit=True):
        from telegram import ForceReply
        from urllib.parse import parse_qs, urlsplit
        from tests.test_credential_session import compact, encrypt
        self.harness.fake_mail_services()
        await self.harness.dispatch_native(route=route, thread_id=thread_id)
        await self.harness.press("Gmail", route=route)
        prompt = next(message for message, kwargs in reversed(self.harness.native_sent)
                      if isinstance(kwargs.get("reply_markup"), ForceReply))
        await self.harness.dispatch_native(text="me@example.com", reply=prompt, route=route, thread_id=thread_id)
        await self.harness.press("Secure app password", route=route)
        markup = self.harness.native_sent[-1][1]["reply_markup"]
        url = markup.keyboard[0][0].web_app.url
        public = {k: v[0] for k, v in parse_qs(parse_qs(urlsplit(url).fragment)["mn"][0]).items()}
        binding = json.loads(public["binding"])
        challenge = SimpleNamespace(**{k: public[k] for k in ("session_id", "server_public_key", "binding")})
        payload = encrypt(challenge, compact({"password": "test-only-secret", "draft_id": binding[3], "revision": int(binding[4])}).encode())
        if submit:
            await self.harness.dispatch_native(web_app=payload, route=route, thread_id=thread_id)
        return payload

    async def test_public_native_full_connection_and_revoked_confirmation_are_secret_free(self):
        payload = await self.connected_preview()
        probes = len(self.harness.imap_probes)
        await self.harness.dispatch_native(web_app=payload, route="beta", thread_id=9)
        self.assertEqual(len(self.harness.imap_probes), probes)
        self.assertEqual(self.harness.mail_passwords, {})
        token, message = self.harness.button("Start with these settings")
        with self.assertLogs("gateway.run", "WARNING"):
            await self.harness.dispatch_native(callback=token, reply=message, route="beta", allowed_users="222")
        self.assertEqual(self.harness.mail_passwords, {})
        await self.harness.dispatch_native(callback=token, reply=message, route="beta")
        self.assertEqual(len(self.harness.mail_passwords), 1)
        self.assertEqual({home for home, _, _ in self.harness.mail_effects}, {str(self.harness.beta_home)})
        self.assertEqual(set(self.harness.mail_scopes), {self.harness.profiles["beta"].manager.scope_key})
        self.assertTrue(all(worker != threading.get_ident() for _, _, worker in self.harness.mail_effects))
        self.assertEqual([effect for _, effect, _ in self.harness.mail_effects].count("cron"), 1)
        self.assertIn("Setup complete", self.harness.native_sent[-1][1]["text"])
        self.assertEqual(self.harness.model_inputs, [])
        captured = repr(self.harness.events) + repr(self.harness.event_objects) + repr(self.harness.native_sent)
        self.assertNotIn("test-only-secret", captured)
        self.assertNotIn(payload, captured)

    async def test_secret_wait_plain_text_and_stale_prompt_never_reach_agent(self):
        from telegram import ForceReply
        self.harness.fake_mail_services()
        await self.harness.dispatch_native()
        await self.harness.press("Gmail")
        prompt = next(message for message, kwargs in reversed(self.harness.native_sent)
                      if isinstance(kwargs.get("reply_markup"), ForceReply))
        await self.harness.dispatch_native(text="me@example.com", reply=prompt)
        await self.harness.dispatch_native(text="accidental-test-only-password")
        await self.harness.dispatch_native(text="stale-test-only-password", reply=prompt)
        captured = repr(self.harness.events) + repr(self.harness.model_inputs) + repr(self.harness.native_sent)
        self.assertNotIn("accidental-test-only-password", captured)
        self.assertNotIn("stale-test-only-password", captured)
        self.assertEqual(self.harness.imap_probes, [])
        self.assertEqual(self.harness.model_inputs, [])

    async def test_group_guidance_has_no_password_button_or_draft(self):
        await self.harness.dispatch_native(chat_type="group")
        self.assertIn("private chat", self.harness.native_sent[-1][1]["text"])
        self.assertNotIn("reply_markup", self.harness.native_sent[-1][1])
        self.assertFalse(list(self.harness.home.rglob("setup.db")))

    async def test_unrelated_native_commands_messages_buttons_and_webapps_pass_through(self):
        from telegram.ext import CallbackQueryHandler, MessageHandler, filters
        captured = []
        async def core(update, context):
            captured.append(update)
        self.harness.application.add_handler(CallbackQueryHandler(core))
        self.harness.application.add_handler(MessageHandler(filters.ALL, core))
        for kwargs in ({"text": "hello"}, {"text": "/help"}, {"callback": "core:continue"},
                       {"callback": "other_plugin:yes"}, {"web_app": '{"kind":"another_plugin"}'}):
            await self.harness.dispatch_native(**kwargs)
        self.assertEqual(len(captured), 5)
        self.assertFalse(list(self.harness.home.rglob("setup.db")))

    async def test_cancel_during_slow_confirm_revokes_write_lease_before_publication(self):
        await self.connected_preview()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        module = importlib.import_module(self.harness.profiles["beta"].controller.__module__)
        def slow(host, settings):
            entered.set()
            release.wait(3)
        self.harness.patch(module.HermesHost, "check_environment", slow)
        await self.harness.press("Start with these settings", route="beta", drain=False)
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        await self.harness.press("Cancel", route="beta", drain=False)
        # Allow admitted cancellation to execute, without letting the slow probe finish.
        await asyncio.sleep(.1)
        release.set()
        await self.harness.drain()
        self.assertEqual(self.harness.mail_passwords, {})
        self.assertFalse([e for _, e, _ in self.harness.mail_effects if e in {"cron", "send"}])

    async def test_natural_setup_tool_uses_real_live_origin_and_the_receiving_bot(self):
        from telegram.ext import MessageHandler, filters
        self.harness.request_origin = True
        self.harness.requested_tool = "mail_notifier_setup"
        self.harness.application.add_handler(MessageHandler(filters.TEXT, self.harness.adapter._handle_text_message))
        await self.harness.dispatch_native(text="Connect my email", route="beta", thread_id=9, language_code="ko-KR")
        panels = [kwargs for _, kwargs in self.harness.native_sent if kwargs.get("reply_markup")]
        self.assertTrue(panels)
        self.assertEqual(panels[-1]["chat_id"], "111")
        self.assertEqual(panels[-1]["message_thread_id"], 9)
        self.assertIn("메일 연결", panels[-1]["text"])
        self.assertTrue(list(self.harness.beta_home.rglob("setup.db")))
        self.assertTrue(any("panel_opening" in value for value in self.harness.model_inputs))
        controller = self.harness.profiles["beta"].controller
        count = len(self.harness.native_sent)
        result = json.loads(contextvars.Context().run(controller.open_setup_panel, {}))
        self.assertEqual(result["next_action"], "/mail")
        self.assertEqual(len(self.harness.native_sent), count)

    async def test_unloading_transport_profile_does_not_revoke_routed_profile_commit(self):
        await self.connected_preview()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        module = importlib.import_module(self.harness.profiles["beta"].controller.__module__)
        def slow(host, settings):
            entered.set()
            release.wait(3)
        self.harness.patch(module.HermesHost, "check_environment", slow)
        await self.harness.press("Start with these settings", route="beta", drain=False)
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        self.harness.profiles["default"].manager.unload()
        await asyncio.sleep(0)
        release.set()
        await self.harness.drain()
        self.assertEqual(len(self.harness.mail_passwords), 1)
        self.assertTrue(all(home == str(self.harness.beta_home) for home, _, _ in self.harness.mail_effects))

    async def test_malformed_or_oversized_plugin_webapp_is_not_forwarded_to_core(self):
        from telegram.ext import MessageHandler, filters
        captured = []
        async def core(update, context):
            captured.append(update)
        self.harness.application.add_handler(MessageHandler(filters.ALL, core))
        for payload in ('{"kind":"mail_notifier_credential","ciphertext":"test-only-malformed"',
                        '{"kind":"mail_notifier_credential","ciphertext":"' + "A" * 5000 + '"}'):
            await self.harness.dispatch_native(web_app=payload)
        self.assertEqual(captured, [])
        self.assertEqual(self.harness.model_inputs, [])

    async def test_revoked_encrypted_submission_never_decrypts_or_validates(self):
        payload = await self.connected_preview(submit=False)
        with self.assertLogs("gateway.run", "WARNING"):
            await self.harness.dispatch_native(web_app=payload, route="beta", thread_id=9, allowed_users="222")
        self.assertEqual(self.harness.imap_probes, [])
        self.assertEqual(self.harness.mail_passwords, {})
        await self.harness.dispatch_native(web_app=payload, route="beta", thread_id=9)
        self.assertEqual(len(self.harness.imap_probes), 1)
        self.harness.button("Start with these settings")

    async def test_cancel_while_confirmation_is_queued_prevents_it_from_starting(self):
        await self.connected_preview()
        receipt = await self.harness.submit_native(route="beta")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def busy():
            entered.set()
            release.wait(3)
            return {}
        runtime = self.harness.profiles["beta"].controller.runtime
        job = asyncio.create_task(runtime.submit(receipt, busy))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        await self.harness.press("Start with these settings", route="beta", drain=False)
        await asyncio.sleep(.05)
        await self.harness.press("Cancel", route="beta", drain=False)
        await asyncio.sleep(.05)
        release.set()
        await job
        await self.harness.drain()
        self.assertEqual(self.harness.mail_passwords, {})
        self.assertFalse([e for _, e, _ in self.harness.mail_effects if e in {"cron", "send"}])

    async def test_onboarding_capability_probe_is_independent_and_fails_closed(self):
        import hermes_cli.plugins as plugins
        module = importlib.import_module(self.harness.profiles["default"].controller.__module__)
        host = module.HermesHost(self.harness.profiles["default"].controller.ctx)
        with patch.object(host, "check_compatibility", side_effect=AssertionError("legacy probe must not run")):
            host.check_onboarding_compatibility()
        with patch.object(plugins, "VALID_HOOKS", plugins.VALID_HOOKS - {"post_gateway_admission"}):
            with self.assertRaises(module.HostError) as caught:
                host.check_onboarding_compatibility()
        self.assertEqual(caught.exception.code, "incompatible_hermes_onboarding")

    async def test_different_loader_namespaces_share_actual_profile_execution_limit(self):
        alpha, beta = self.harness.profiles["default"], self.harness.profiles["beta"]
        receipt = await self.harness.submit_native(route="beta")
        entered = asyncio.Queue()
        release = threading.Event()
        self.addCleanup(release.set)
        loop = asyncio.get_running_loop()
        def slow():
            loop.call_soon_threadsafe(entered.put_nowait, "started")
            release.wait(3)
            return {}
        jobs = [asyncio.create_task(p.controller.runtime.submit(receipt, slow)) for p in (alpha, beta)]
        await asyncio.wait_for(entered.get(), 2)
        await asyncio.sleep(.05)
        self.assertTrue(entered.empty())
        release.set()
        await asyncio.gather(*jobs)
        self.assertEqual(entered.qsize(), 1)

    async def test_existing_fifty_account_panel_is_sent_within_telegram_text_limits(self):
        from gateway.run import _profile_runtime_scope
        from mail_notifier.config import Account, Settings, dump_settings
        from tests.support import account_data
        accounts = tuple(Account(**account_data("a" + str(i), label="📬" * 100 + str(i))) for i in range(50))
        with _profile_runtime_scope(self.harness.beta_home):
            self.harness.profiles["beta"].controller.ctx.set_config("configuration", dump_settings(Settings(accounts=accounts)))
        await self.harness.dispatch_native(route="beta")
        messages = [kwargs["text"] for _, kwargs in self.harness.native_sent]
        self.assertTrue(all(len(message.encode("utf-16-le")) // 2 <= 4096 for message in messages))
        self.assertIn("a0@example.com", "".join(messages))
        self.assertIn("a49@example.com", "".join(messages))
        self.harness.button("Connect another account")

    async def test_public_reload_survives_old_async_cleanup_and_repeated_unload(self):
        from gateway.run import _profile_runtime_scope
        from telegram.ext import Application
        profile = self.harness.profiles["default"]
        await self.harness.dispatch_native()
        old = profile.controller
        manifest = profile.manager._plugins["hermes-mail-notifier"].manifest
        profile.manager.unload()
        profile.manager._load_plugin(manifest)
        loaded = profile.manager._plugins["hermes-mail-notifier"]
        self.assertFalse(loaded.error)
        controller = next(cb.__self__ for cb in profile.manager._hooks["post_gateway_admission"])
        # The pinned host de-duplicates factory qualnames per live native client.
        # A supported reconnect/rebuild supplies a new client; do not reset or
        # patch its private wired-factory cache to force hot-reload behavior.
        self.harness.application = Application.builder().bot(self.harness.bot).updater(None).build()
        self.harness.application._initialized = True
        with _profile_runtime_scope(self.harness.home):
            broker = controller.broker
            self.harness.adapter._wire_plugin_handlers(self.harness.application)
        self.harness.profiles["default"] = SimpleNamespace(manager=profile.manager, controller=controller,
                                                           broker=broker, module=profile.module)
        old.unload()
        await asyncio.sleep(0)
        self.harness.native_sent.clear()
        await self.harness.dispatch_native()
        self.assertTrue(any(kwargs.get("reply_markup") for _, kwargs in self.harness.native_sent))
        self.assertNotIn("setup_work_cancelled", repr(self.harness.native_sent))


if __name__ == "__main__":
    unittest.main()
