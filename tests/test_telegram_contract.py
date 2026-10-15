"""Pinned, offline Hermes admission contracts; opt-in never masks a missing host."""

import asyncio
import contextvars
import importlib
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("HERMES_CONTRACT") == "1", "set HERMES_CONTRACT=1 with pinned Hermes")
class TelegramAdmissionContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.telegram_support import GatewayHarness
        self.harness = GatewayHarness()
        self.addAsyncCleanup(self.harness.close)

    async def test_denied_sender_never_receives_an_admission_receipt(self):
        result = await self.harness.submit_native(user_id="222", allowed_users="111")
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
            return original[0](**kwargs)
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
        self.assertIsNone(await self.harness.submit_ordinary(user_id="222"))

    async def test_shared_slot_abi_mismatch_fails_closed(self):
        with patch.dict(sys.modules, {"_hermes_mail_notifier_onboarding_v1": SimpleNamespace(ABI=2)}):
            with self.assertRaises(RuntimeError):
                self.harness.auth.acquire_broker()


if __name__ == "__main__":
    unittest.main()
