"""Real registered/native/admitted setup with only remote service I/O replaced."""

import asyncio
import io
import json
import logging
import os
import threading
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout

from tests.telegram_support import GatewayHarness


@unittest.skipUnless(os.environ.get("HERMES_CONTRACT") == "1", "set HERMES_CONTRACT=1 with pinned Hermes")
class OnboardingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stderr = redirect_stderr(self.output)
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.addCleanup(self.stderr.__exit__, None, None, None)
        self.logs = logging.StreamHandler(self.output)
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)
        self.harness = GatewayHarness()
        self.addAsyncCleanup(self.harness.close)
        self.harness.offline_mail_services()

    async def inspect(self, operation, *, route=None):
        receipt = await self.harness.submit_native(route=route)
        self.assertIsNotNone(receipt)
        controller = self.harness.profiles[route or "default"].controller
        resource = self.harness.profile_resource(route or "default")
        return await controller.runtime.submit(receipt, lambda: operation(resource, receipt.owner))

    def audit(self, passwords, payloads=()):
        self.harness.assert_secret_free(self, passwords, captured=self.output, ciphertexts=payloads)
        self.assertEqual(self.harness.network_attempts, [])

    async def test_bilingual_empty_profiles_complete_with_real_secret_config_cron_and_no_secret_sinks(self):
        h = self.harness
        from telegram.ext import MessageHandler, filters
        h.request_origin = True
        h.requested_tool = "mail_notifier_setup"
        h.application.add_handler(MessageHandler(filters.TEXT, h.adapter._handle_text_message))
        for route, language, confirmation in ((None, "en", "Start with these settings"), ("beta", "ko", "이 설정으로 시작")):
            with self.subTest(language=language):
                password = "test-only-SENTINEL-" + language
                await h.dispatch_native(text="메일 연결해줘" if language == "ko" else "Connect my email",
                                        route=route, language_code=language)
                self.assertTrue(any("panel_opening" in value for value in h.model_inputs),
                                "real tool result must return through the host model conversation")
                payload = await h.secure_preview(route=route, language=language, password=password)
                home = h.beta_home if route else h.home
                self.assertNotIn(password, (home / ".env").read_text())
                await h.press(confirmation, route=route)
                def inspect(resource, owner):
                    settings = resource.host.load_settings()
                    with closing(resource.host.open_store()) as store:
                        cursor = store.cursor(settings.accounts[0].id)
                        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 3)
                    with closing(resource.host.open_setup_store()) as setup:
                        self.assertEqual(setup.db.execute("PRAGMA user_version").fetchone()[0], 2)
                        self.assertEqual(setup.db.execute("SELECT state FROM drafts").fetchall()[0][0], "completed")
                    self.assertEqual(resource.host.get_password(settings.accounts[0]), password)
                    jobs = resource.host.manage_cron("list", include_disabled=True)["jobs"]
                    return {"language": settings.language, "ui_language": settings.ui_language,
                            "target": settings.target, "cursor": cursor, "jobs": len(jobs)}
                self.assertEqual(await self.inspect(inspect, route=route),
                                 {"language": language, "ui_language": language, "target": "telegram:111", "cursor": (7, 100), "jobs": 1})
                self.assertTrue(any(("설정 완료" if language == "ko" else "Setup complete") in kwargs["text"]
                                    for _, kwargs in h.native_sent))
                deliveries = [json.loads(line) for line in (home / "test-deliveries.jsonl").read_text().splitlines()]
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(deliveries[0]["target"], "telegram:111")
                self.assertTrue(deliveries[0]["text"].startswith("메일 알림 설정 완료." if language == "ko"
                                                                 else "Mail notifications are configured."))
                self.assertTrue(any(kind == "reload" for kind, *_ in h.external_requests))
                self.audit(["test-only-SENTINEL-en", "test-only-SENTINEL-ko"], [payload])

    async def test_same_update_and_ciphertext_and_confirmation_replays_do_not_duplicate_effects(self):
        h = self.harness
        password = "test-only-SENTINEL-duplicates"
        payload = await h.secure_preview(password=password, submit=False)
        update = await h.dispatch_native(web_app=payload)
        await h.application.process_update(update)  # exact same Telegram update
        await h.drain()
        await h.dispatch_native(web_app=payload)  # same ciphertext, new update
        self.assertEqual(len(h.imap_probes), 1)
        token, message = h.button("Start with these settings")
        with self.assertLogs("gateway.run", "WARNING"):
            await h.dispatch_native(callback=token, reply=message, allowed_users="222")
        self.assertNotIn(password, (h.home / ".env").read_text())
        self.assertFalse((h.home / "test-deliveries.jsonl").exists())
        update = await h.dispatch_native(callback=token, reply=message)
        await h.application.process_update(update)
        await h.drain()
        await h.dispatch_native(callback=token, reply=message)
        await h.dispatch_native(web_app=payload)
        result = await self.inspect(lambda r, o: {"jobs": len(r.host.manage_cron("list", include_disabled=True)["jobs"]),
                                                 "accounts": len(r.host.load_settings().accounts)})
        self.assertEqual(result, {"jobs": 1, "accounts": 1})
        self.assertEqual(len(h.imap_probes), 2)  # validation and post-confirmation snapshot only
        self.assertEqual(len((h.home / "test-deliveries.jsonl").read_text().splitlines()), 1)
        self.audit([password], [payload])

    async def test_cancel_during_real_validation_and_queued_validation_leaves_no_writes_or_cached_secret(self):
        h = self.harness
        async def admission_tail(**kwargs):
            # Receipt creation precedes the real host's session-guard cleanup.
            # Force that gap so queue entry alone cannot hide a busy-path race.
            await asyncio.sleep(.05)
        h.profiles["default"].manager._hooks.setdefault("on_processing_complete", []).append(admission_tail)
        password = "test-only-SENTINEL-cancel-validation"
        payload = await h.secure_preview(password=password, submit=False)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(account, value):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release validation")
        h.mailbox_hook = slow
        await h.dispatch_native(web_app=payload, drain=False)
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        await asyncio.wait_for(asyncio.gather(*list(h.adapter._background_tasks)), 3)
        # A second encrypted update is queued behind the real validation worker.
        await h.dispatch_native(web_app=payload, drain=False)
        async def queued():
            while len(h.auth._shared().runtime.profiles[str(h.home)].jobs) < 2:
                await asyncio.sleep(0)
        await asyncio.wait_for(queued(), 3)
        # Wait only for actual gateway admission cleanup, not the deliberately
        # blocked plugin workers. Never clear/patch host guards or fabricate auth.
        await asyncio.wait_for(asyncio.gather(*list(h.adapter._background_tasks)), 3)
        await h.press("Cancel", drain=False)
        resource = h.profile_resource()
        # Wait for the real runtime's cancellation flag, not a sleep/race guess.
        async def revoked():
            while not any(job.cancelled.is_set() for job in h.auth._shared().runtime.profiles[str(h.home)].jobs):
                await asyncio.sleep(0)
        await asyncio.wait_for(revoked(), 3)
        release.set()
        await h.drain()
        result = await self.inspect(lambda r, o: {"accounts": len(r.host.load_settings().accounts),
                                                 "secrets": len(r.service._secrets),
                                                 "jobs": len(r.host.manage_cron("list", include_disabled=True)["jobs"])})
        self.assertEqual(result, {"accounts": 0, "secrets": 0, "jobs": 0})
        self.assertEqual(len(h.imap_probes), 1)
        self.assertFalse((h.home / "test-deliveries.jsonl").exists())
        self.assertNotIn(password, (h.home / ".env").read_text())
        # Validation's intent increments the revision. The overlapping old
        # Cancel revokes work, but must not bypass durable CAS to claim success.
        # Refresh and explicitly cancel the current revision before terminal
        # key disposal; stale encrypted input still cannot revive the attempt.
        await h.dispatch_native()
        await h.press("Cancel")
        await h.dispatch_native(web_app=payload)
        self.assertFalse(resource.sessions._sessions)
        with closing(resource.host.open_setup_store()) as store:
            self.assertEqual(store.db.execute("SELECT state FROM drafts").fetchall()[0][0], "cancelled")
        self.audit([password], [payload])

    async def test_bilingual_failed_delivery_retries_only_unfinished_step_without_reentering_secret(self):
        h = self.harness
        passwords, payloads = [], []
        for route, language, confirm, retry in ((None, "en", "Start with these settings", "Retry failed step"),
                                                ("beta", "ko", "이 설정으로 시작", "실패 단계 다시 시도")):
            with self.subTest(language=language):
                password = "test-only-SENTINEL-retry-" + language
                passwords.append(password)
                payloads.append(await h.secure_preview(route=route, language=language, password=password))
                h.send_failure = True
                await h.press(confirm, route=route)
                self.assertTrue(any(("설정을 완료하지" if language == "ko" else "Setup is not complete") in kwargs["text"]
                                    or "delivery_failed" in kwargs["text"] for _, kwargs in h.native_sent))
                resource = h.profile_resource(route or "default")
                self.assertFalse(resource.service._secrets)
                probes = len(h.imap_probes)
                h.send_failure = False
                await h.press(retry, route=route)
                self.assertEqual(len(h.imap_probes), probes)
                self.assertFalse(resource.service._secrets)
                home = h.beta_home if route else h.home
                self.assertEqual(len((home / "test-deliveries.jsonl").read_text().splitlines()), 1)
                self.assertEqual(await self.inspect(lambda r, o: {"jobs": len(r.host.manage_cron("list", include_disabled=True)["jobs"])},
                                                    route=route), {"jobs": 1})
        self.audit(passwords, payloads)

    async def test_concurrent_profile_commits_keep_real_credentials_cursors_and_single_crons_separate(self):
        h = self.harness
        passwords = ["test-only-SENTINEL-alpha", "test-only-SENTINEL-beta"]
        payloads = [await h.secure_preview(password=passwords[0])]
        alpha_button = h.button("Start with these settings")
        payloads.append(await h.secure_preview(route="beta", password=passwords[1]))
        beta_button = h.button("Start with these settings")
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        self.addCleanup(release.set)
        from hermes_constants import get_hermes_home
        def slow(account, value):
            index = int(get_hermes_home() == h.beta_home)
            entered[index].set()
            if not release.wait(5):
                raise AssertionError("both profile workers did not reach snapshot")
        h.mailbox_hook = slow
        # Tokens are owner+profile-bound. Capture each actual panel separately.
        from gateway.run import _profile_runtime_scope
        for route, (token, message), event in ((None, alpha_button, entered[0]), ("beta", beta_button, entered[1])):
            await h.dispatch_native(callback=token, reply=message, route=route, drain=False)
            self.assertTrue(await asyncio.to_thread(event.wait, 3))
            await asyncio.wait_for(asyncio.gather(*list(h.adapter._background_tasks)), 3)
        release.set()
        await h.drain()
        ids = []
        for route, password, other in ((None, passwords[0], passwords[1]), ("beta", passwords[1], passwords[0])):
            def inspect(r, o):
                account = r.host.load_settings().accounts[0]
                self.assertEqual(r.host.get_password(account), password)
                with closing(r.host.open_store()) as store:
                    self.assertEqual(store.cursor(account.id), (7, 100))
                self.assertEqual(len(r.host.manage_cron("list", include_disabled=True)["jobs"]), 1)
                return {"id": account.id}
            ids.append((await self.inspect(inspect, route=route))["id"])
            home = h.beta_home if route else h.home
            self.assertIn(password, (home / ".env").read_text())
            self.assertNotIn(other, (home / ".env").read_text())
        self.assertNotEqual(*ids)
        with _profile_runtime_scope(h.home):
            from mail_notifier.hermes_host import HermesHost, HostError
            from mail_notifier.config import Account, secret_name
            with self.assertRaises(HostError):
                HermesHost(h.profiles["default"].controller.ctx).get_password(
                    Account(ids[1], "Beta", "gmail", "me@example.invalid", secret_name(ids[1])))
        self.audit(passwords, payloads)

    async def test_paused_acknowledgement_is_durable_handoff_not_completion_or_resume(self):
        h = self.harness
        password = "test-only-SENTINEL-paused"
        payload = await h.secure_preview(password=password)
        await h.press("Start with these settings")
        def pause_and_change(r, owner):
            job = r.host.manage_cron("list", include_disabled=True)["jobs"][0]["job_id"]
            r.host.manage_cron("pause", job_id=job)
            draft = r.service.start(owner)
            draft = r.service.change(draft.id, owner, draft.revision, action="settings", values={"language": "fr"})
            return r.service.commit(draft.id, owner, draft.revision)
        result = await self.inspect(pause_and_change)
        self.assertEqual(result["state"], "scheduled")
        await h.dispatch_native()
        await h.press("Keep paused and continue")
        self.assertIn("not rolled back or completed", h.native_sent[-1][1]["text"])
        h.reload_profile()  # new controller/service reopens the same durable handoff
        await h.dispatch_native()
        def inspect(r, owner):
            with closing(r.host.open_setup_store()) as store:
                draft = store.get(result["draft_id"], owner)
                self.assertEqual(draft.state, "acknowledged")
                self.assertEqual(draft.steps["acknowledgement"]["reason"], "paused")
                self.assertTrue(draft.steps["test_delivery"]["message_id"])
            fresh = r.service.start(owner)
            self.assertNotEqual(fresh.id, draft.id)
            self.assertEqual(fresh.steps, {})
            self.assertEqual(fresh.original.language, "fr")
            self.assertFalse(r.host.manage_cron("list", include_disabled=True)["jobs"][0]["enabled"])
            return {"state": draft.state}
        self.assertEqual(await self.inspect(inspect), {"state": "acknowledged"})
        self.audit([password], [payload])
