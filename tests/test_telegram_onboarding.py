"""First-run UI with real setup storage/service and real credential cryptography.

Only external IMAP/model/cron/delivery boundaries use the existing offline host.
Pinned native admission/registration is additionally exercised by contract tests.
"""

import contextvars
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mail_notifier.config import Settings
from mail_notifier.onboarding_auth import AdmissionReceipt, NativeInput, SetupOwner, owner_key
from tests.support import feature
from tests.test_cli import SetupHost
from tests.test_credential_session import compact, encrypt
from tests.test_service import FakeMailbox


class TelegramOnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.telegram_onboarding")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SetupHost(self.temp.name)
        self.host.check_onboarding_compatibility = lambda: None
        self.host.credential_page_url = lambda: "https://operator.example/credential/"
        self.owner = SetupOwner(str(Path(self.temp.name)), "telegram", "700", "111", "111", "9")
        self.receipt = AdmissionReceipt(self.owner, "session", "42", contextvars.copy_context())
        self.controller = self.module.TelegramOnboarding(SimpleNamespace())
        # These are UI/service unit tests, not an authorization fake. The pinned
        # contract tests above use the real public loader/hook to bind profiles.
        self.module.views._ui()["profiles"][self.owner.profile_home] = self.controller
        self.addCleanup(lambda: self.module.views._ui()["profiles"].pop(self.owner.profile_home, None))
        self.addAsyncCleanup(self.controller.runtime.close_profile, self.owner.profile_home)
        self.probes = []
        def mailbox(account, password):
            self.probes.append(password)
            return FakeMailbox(account)
        for name, value in (("HermesHost", lambda ctx: self.host), ("ImapMailbox", mailbox)):
            p = patch.object(self.module, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.update = 0

    async def send(self, kind, value="", language="en"):
        self.update += 1
        item = NativeInput(kind, "700", "111", "111", "9", str(42 + self.update), str(self.update), language, value)
        return await self.controller.handle(replace(self.receipt, message_id=item.message_id), item)

    async def press(self, view, label):
        token = next(b["callback_data"] for b in view["buttons"] if b["text"] == label)
        self.assertLessEqual(len(token.encode()), 64)
        self.assertNotIn(self.owner.profile_home, token)
        return await self.send("callback", token)

    async def address(self, view):
        bound = await self.controller.bind_prompt(self.receipt, view, "500")
        return await self.send("reply", json.dumps({"prompt_message_id": "500", "text": "me@example.com"})), bound

    async def password(self, view, password="test-only-secret"):
        opened = await self.press(view, "Secure app password" if "App password" in view["text"] else "앱 비밀번호 안전하게 입력")
        fragment = parse_qs(urlsplit(opened["web_app_url"]).fragment)
        public = {key: values[0] for key, values in parse_qs(fragment["mn"][0]).items()}
        binding = json.loads(public["binding"])
        challenge = SimpleNamespace(**{k: public[k] for k in ("session_id", "server_public_key", "binding")})
        payload = encrypt(challenge, compact({"password": password, "draft_id": binding[3], "revision": int(binding[4])}).encode())
        return await self.send("web_app", payload), payload

    async def test_first_connection_requires_confirm_and_never_exposes_secret_in_views(self):
        for language, confirm in (("en", "Start with these settings"), ("ko", "이 설정으로 시작")):
            with self.subTest(language=language):
                view = await self.send("start", language=language)
                view = await self.press(view, "English" if language == "en" else "한국어")
                view = await self.press(view, "Gmail")
                self.assertEqual(view["input_field"], "username")
                view, _ = await self.address(view)
                self.assertIn("support.google.com", view["text"])
                preview, _ = await self.password(view)
                self.assertNotIn("test-only-secret", repr(preview))
                self.assertEqual(self.host.settings.accounts, ())
                self.assertEqual(self.host.passwords, {})
                self.assertIn("telegram:111:9", preview["text"])
                self.assertIn("every 5m", preview["text"])
                self.assertIn("INBOX", preview["text"])
                result = await self.press(preview, confirm)
                self.assertEqual(result["state"], "completed")
                self.assertEqual(len(self.host.settings.accounts), 1)
                self.assertEqual(self.host.settings.ui_language, language)
                self.assertEqual(self.host.settings.language, language)
                self.assertEqual(self.host.schedule_calls, 1)
                self.assertEqual(self.host.model_calls, 1)
                self.assertEqual(len(self.host.deliveries), 1)
                if language == "en":
                    # A separate, empty synthetic installation for the second locale.
                    await self.controller.runtime.close_profile(self.owner.profile_home)
                    self.controller.runtime.open_profile(self.owner.profile_home)
                    for path in Path(self.temp.name).iterdir():
                        if path.is_file():
                            path.unlink()
                    self.host = SetupHost(self.temp.name)
                    self.host.check_onboarding_compatibility = lambda: None
                    self.host.credential_page_url = lambda: "https://operator.example/credential/"

    async def test_stale_reply_revision_and_other_owner_cannot_change_draft(self):
        view = await self.press(await self.send("start"), "Gmail")
        view, bound = await self.address(view)
        stale = await self.send("reply", json.dumps({"prompt_message_id": "500", "text": "other@example.com"}))
        self.assertEqual(stale["error"], "setup_changed_retry")
        token = bound["buttons"][0]["callback_data"]
        foreign = AdmissionReceipt(SetupOwner(self.owner.profile_home, "telegram", "701", "111", "111", "9"), "s", "45", contextvars.copy_context())
        item = NativeInput("callback", "701", "111", "111", "9", "45", "900", "en", token)
        denied = await self.controller.handle(foreign, item)
        self.assertEqual(denied["error"], "setup_changed_retry")
        self.assertEqual(self.host.events, [])

    async def test_password_text_is_discarded_and_unconfigured_url_only_blocks_secure_input(self):
        view = await self.press(await self.send("start"), "Naver")
        view, _ = await self.address(view)
        self.assertIn("help.naver.com", view["text"])
        self.assertIn("Korean", view["text"])
        self.host.credential_page_url = lambda: ""
        result = await self.press(view, "Secure app password")
        self.assertNotIn("web_app_url", result)
        self.assertIn("unavailable", result["text"])
        self.assertIn("사용", result["text"])
        result = await self.send("password_text")
        self.assertEqual(result["state"], "awaiting_secret")
        self.assertEqual(self.probes, [])

    async def test_duplicate_encrypted_update_skips_validation_and_publication(self):
        view = await self.press(await self.send("start"), "Gmail")
        view, _ = await self.address(view)
        preview, payload = await self.password(view)
        probes = len(self.probes)
        duplicate = await self.send("web_app", payload)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.probes), probes)
        self.assertEqual(self.host.events, [])
        self.assertEqual(preview["revision"], duplicate["revision"])

    async def test_malformed_envelopes_count_toward_the_same_submission_budget(self):
        view = await self.press(await self.send("start"), "Gmail")
        view, _ = await self.address(view)
        _, payload = await self.password(view)
        for _ in range(5):
            rejected = await self.send("web_app", '{"kind":"mail_notifier_credential","session_id":"unknown"}')
            self.assertEqual(rejected["error"], "invalid_credential_submission")
        limited = await self.send("web_app", payload)
        self.assertEqual(limited["error"], "invalid_credential_submission")
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(self.host.events, [])

    async def test_paused_handoff_does_not_claim_resumed_or_replay_writes(self):
        self.host.paused = True
        view = await self.press(await self.send("start"), "Gmail")
        view, _ = await self.address(view)
        preview, _ = await self.password(view)
        paused = await self.press(preview, "Start with these settings")
        self.assertEqual(paused["state"], "scheduled")
        self.assertIn("paused", paused["text"])
        self.assertNotIn("Setup complete", paused["text"])
        acknowledged = await self.press(paused, "Keep paused and continue")
        self.assertEqual(acknowledged["state"], "acknowledged")
        self.assertTrue(self.host.paused)
        self.assertEqual(len(self.host.settings.accounts), 1)
        new = await self.send("start")
        self.assertNotEqual(new["draft_id"], acknowledged["draft_id"])
        self.assertEqual(self.host.schedule_calls, 1)

    async def test_conflict_acknowledgement_requires_current_review_and_starts_fresh(self):
        view = await self.press(await self.send("start"), "Gmail")
        view, _ = await self.address(view)
        preview, _ = await self.password(view)
        self.host.settings = Settings(target="telegram:222", language="fr")
        conflict = await self.press(preview, "Start with these settings")
        self.assertEqual(conflict["state"], "needs_review")
        self.assertIn("telegram:222", conflict["text"])
        self.assertIn("not rolled back", conflict["text"])
        self.assertEqual(self.host.events, [])
        acknowledged = await self.press(conflict, "Acknowledge and restart from current settings")
        self.assertEqual(acknowledged["state"], "acknowledged")
        fresh = await self.send("start")
        self.assertNotEqual(fresh["draft_id"], acknowledged["draft_id"])
        self.assertEqual(self.host.settings.accounts, ())
        self.assertEqual(self.host.settings.target, "telegram:222")

    async def test_failed_delivery_retries_only_the_recorded_step(self):
        view = await self.press(await self.send("start"), "Gmail")
        view, _ = await self.address(view)
        preview, _ = await self.password(view)
        self.host.send_error = True
        partial = await self.press(preview, "Start with these settings")
        self.assertEqual(partial["state"], "repair_required")
        self.assertIn("Settings published: yes", partial["text"])
        self.assertIn("delivery_failed", partial["text"])
        self.host.send_error = False
        complete = await self.press(partial, "Retry failed step")
        self.assertEqual(complete["state"], "completed")
        self.assertEqual(len(self.host.settings.accounts), 1)
        self.assertEqual(self.host.events.count("password"), 1)
        self.assertEqual(self.host.schedule_calls, 1)
        self.assertEqual(self.host.model_calls, 1)

    async def test_existing_settings_are_not_retargeted_or_translated_by_detected_locale(self):
        self.host.settings = Settings(target="telegram:222:3", language="fr", schedule="every 10m", max_fetches=3)
        view = await self.press(await self.send("start", language="ko-KR"), "English")
        view = await self.press(view, "Gmail")
        view, _ = await self.address(view)
        preview, _ = await self.password(view)
        self.assertIn("telegram:222:3", preview["text"])
        self.assertIn("every 10m", preview["text"])
        self.assertIn("fr", preview["text"])

    async def test_back_before_password_then_schedule_restores_pending_account_binding(self):
        view = await self.press(await self.send("start"), "Gmail")
        secret, _ = await self.address(view)
        root = await self.press(secret, "Back")
        schedule = await self.press(root, "Check schedule")
        secret = await self.press(schedule, "Every 10 minutes")
        self.assertEqual(secret["state"], "awaiting_secret")
        self.assertIn("me@example.com", secret["text"])
        preview, _ = await self.password(secret)
        self.assertEqual(preview["state"], "awaiting_confirmation")
        self.assertEqual(self.host.settings.accounts, ())
        await self.press(preview, "Start with these settings")
        self.assertEqual(len(self.host.settings.accounts), 1)
        self.assertEqual(self.host.settings.schedule, "every 10m")

    async def test_back_before_password_then_review_restores_pending_account_binding(self):
        view = await self.press(await self.send("start"), "Gmail")
        secret, _ = await self.address(view)
        root = await self.press(secret, "Back")
        secret = await self.press(root, "Review pending changes")
        self.assertEqual(secret["state"], "awaiting_secret")
        self.assertIn("me@example.com", secret["text"])
        preview, _ = await self.password(secret)
        self.assertEqual(preview["state"], "awaiting_confirmation")
        await self.press(preview, "Start with these settings")
        self.assertEqual(self.host.settings.accounts[0].username, "me@example.com")

    async def test_back_after_password_then_expired_confirmation_restores_account_binding(self):
        view = await self.press(await self.send("start"), "Gmail")
        secret, _ = await self.address(view)
        preview, _ = await self.password(secret)
        root = await self.press(preview, "Back")
        schedule = await self.press(root, "Check schedule")
        preview = await self.press(schedule, "Every 10 minutes")
        self.assertEqual(preview["state"], "awaiting_confirmation")
        resource = self.module.views._shared().runtime.profiles[self.owner.profile_home].resource
        now = resource.service.clock()
        resource.service.clock = lambda: now + 601
        secret = await self.press(preview, "Start with these settings")
        self.assertEqual(secret["state"], "needs_secret")
        self.assertIn("me@example.com", secret["text"])
        self.assertEqual(self.host.events, [])
        preview, _ = await self.password(secret)
        await self.press(preview, "Start with these settings")
        self.assertEqual(self.host.settings.accounts[0].username, "me@example.com")

    async def test_unload_retains_only_nonsecret_waiting_guard_for_inactive_transport(self):
        from mail_notifier.onboarding_auth import AdmissionBroker
        guard = {}
        self.module.views._ui()["transports"][self.owner.bot_id] = {"controller": self.controller, "waiting": guard}
        view = await self.press(await self.send("start"), "Gmail")
        await self.address(view)
        self.controller._broker = AdmissionBroker(self.owner.profile_home)
        resource = self.module.views._shared().runtime.profiles[self.owner.profile_home].resource
        self.controller.unload()
        waiting = guard.get(owner_key(self.owner))
        self.assertIsNotNone(waiting, "unload must preserve the secret-text interception marker")
        self.assertEqual(set(waiting), {"owner", "expires"})
        self.assertNotIn(owner_key(self.owner), self.module.views._ui()["waiting"])
        self.assertFalse(resource.service._secrets)
        self.assertFalse(resource.sessions._sessions)
