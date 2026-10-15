"""The native mailbox must not turn a copied marker into authority."""

import asyncio
import contextvars
import dataclasses
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = importlib.import_module("mail_notifier.onboarding_auth")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = str(Path(self.temp.name).resolve())
        self.broker = self.auth.AdmissionBroker(self.home)
        self.addCleanup(self.broker.close)
        clock = patch("mail_notifier.onboarding_auth.time", SimpleNamespace(monotonic=lambda: 100))
        clock.start()
        self.addCleanup(clock.stop)

    def item(self, **changes):
        values = dict(kind="web_app", bot_id="700", user_id="111", chat_id="111", thread_id="9",
                      message_id="42", update_key="1001", language_code="ko", value="SECRET-CIPHERTEXT")
        return self.auth.NativeInput(**(values | changes))

    def admit(self, reference, *, now=100, **changes):
        fields = dict(platform="telegram", source=dict(chat_type="dm", user_id="111", chat_id="111",
                                                       thread_id="9", scope_id="700"),
                      message_id="42", session_key="agent:beta:telegram:111:9", text="mail-notifier-input:" + reference,
                      now=now)
        return self.broker.admit(**(fields | changes))

    async def test_receipt_requires_native_handoff_and_is_consumed_once(self):
        item = self.item()
        reference = self.broker.offer(item, now=100)
        self.assertNotIn(item.value, reference)
        self.assertNotIn(item.value, repr(item))
        self.assertIsNone(self.admit(reference))
        with self.broker.native_scope(reference):
            receipt = self.admit(reference)
            self.assertIsNone(self.admit(reference))
        self.assertIs(await self.broker.wait(reference, timeout=.01), receipt)
        self.assertEqual(receipt.owner.profile_home, self.home)
        self.assertEqual(receipt.owner.bot_id, "700")
        self.assertIsInstance(receipt.context, contextvars.Context)
        self.assertNotIn("context=", repr(receipt))
        self.assertNotIn("context", receipt.to_dict())
        self.assertNotIn(item.value, json.dumps(receipt.to_dict()))
        with patch("mail_notifier.onboarding_auth.time.monotonic", return_value=101):
            self.assertEqual(self.broker.take(reference, receipt).value, item.value)
            with self.assertRaises(ValueError):
                self.broker.take(reference, receipt)

    async def test_identity_changes_and_non_private_sources_never_admit(self):
        for field, value in (("scope_id", "701"), ("user_id", "222"), ("chat_id", "222"),
                             ("thread_id", "10"), ("chat_type", "group"), ("is_bot", True)):
            with self.subTest(field=field):
                reference = self.broker.offer(self.item(update_key=field), now=100)
                source = dict(chat_type="dm", user_id="111", chat_id="111", thread_id="9", scope_id="700")
                source[field] = value
                with self.broker.native_scope(reference):
                    self.assertIsNone(self.admit(reference, source=source))
        reference = self.broker.offer(self.item(update_key="message"), now=100)
        with self.broker.native_scope(reference):
            self.assertIsNone(self.admit(reference, message_id="43"))
            self.assertIsNone(self.admit(reference, platform="cli"))

    async def test_mailbox_has_128_slots_and_expires_at_30_seconds(self):
        references = [self.broker.offer(self.item(update_key=str(i)), now=100) for i in range(128)]
        with self.assertRaises(ValueError):
            self.broker.offer(self.item(update_key="overflow"), now=129.999)
        with self.broker.native_scope(references[0]):
            self.assertIsNone(self.admit(references[0], now=130))
        self.assertTrue(self.broker.offer(self.item(update_key="replacement"), now=130))

    async def test_duplicates_oversize_and_missing_identity_fail_closed(self):
        self.broker.offer(self.item(), now=100)
        for changes in ({}, {"bot_id": ""}, {"user_id": ""}, {"chat_id": "-123"},
                        {"message_id": ""}, {"value": "가" * 1366}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    self.broker.offer(self.item(**changes), now=100)

    async def test_timeout_and_foreign_or_fabricated_receipts_cannot_take_input(self):
        reference = self.broker.offer(self.item(), now=100)
        self.assertIsNone(await self.broker.wait(reference, timeout=.001))
        with self.broker.native_scope(reference):
            self.assertIsNone(self.admit(reference))
        reference = self.broker.offer(self.item(update_key="new"), now=100)
        with self.broker.native_scope(reference):
            receipt = self.admit(reference)
        forged = dataclasses.replace(receipt, session_key="other")
        with patch("mail_notifier.onboarding_auth.time.monotonic", return_value=101):
            with self.assertRaises(ValueError):
                self.broker.take(reference, forged)
            self.assertEqual(self.broker.take(reference, receipt).message_id, "42")

    async def test_owner_keys_are_structural_normalized_and_immutable(self):
        owner = self.auth.SetupOwner(self.home + "/.", "telegram", "700", "111", "111", "9")
        foreign = SimpleNamespace(**dataclasses.asdict(owner))
        self.assertEqual(self.auth.owner_key(foreign), (self.home, "telegram", "700", "111", "111", "9"))
        self.assertEqual(self.auth.owner_key(owner), self.auth.owner_key(foreign))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            owner.chat_id = "222"
        with self.assertRaises(ValueError):
            self.auth.SetupOwner(self.home, "cron")

    async def test_unload_before_wait_resumes_revokes_an_already_issued_receipt(self):
        reference = self.broker.offer(self.item(), now=100)
        waiter = asyncio.create_task(self.broker.wait(reference, timeout=1))
        await asyncio.sleep(0)
        with self.broker.native_scope(reference):
            self.assertIsNotNone(self.admit(reference))
        self.broker.close()
        self.assertIsNone(await waiter)


if __name__ == "__main__":
    unittest.main()
