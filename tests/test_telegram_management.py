"""Management through the real controller/service; only external services are fake."""

import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from mail_notifier.config import Account, Settings, secret_name
from mail_notifier.hermes_host import HostError
from mail_notifier.onboarding_auth import AdmissionBroker, NativeInput
from tests import test_telegram_onboarding as first_run


class TelegramManagementTests(unittest.IsolatedAsyncioTestCase):
    setUp = first_run.TelegramOnboardingTests.setUp
    send = first_run.TelegramOnboardingTests.send
    press = first_run.TelegramOnboardingTests.press
    password = first_run.TelegramOnboardingTests.password
    address = first_run.TelegramOnboardingTests.address

    def seed(self, count=1, **settings):
        accounts = tuple(Account(f"a{i}", "Same label", "gmail", f"me{i}@example.com", secret_name(f"a{i}"))
                         for i in range(count))
        self.host.settings = Settings(accounts=accounts, target="telegram:-222:3", language="fr", ui_language="en", **settings)
        self.host.credential_page_url = lambda: ""
        self.host.passwords = {a.id: "old-test-secret" for a in accounts}
        with closing(self.host.open_store()) as store:
            for account in accounts:
                store.initialize(account, 7, 100)
            store.enqueue(accounts[0], 7, [101])
            work = store.due(accounts[0].id, 0, 1)[0]
            store.prepare(work.key, "Already prepared · 변경 금지")
            store.retarget(work.mail_id, "telegram:111")
        return accounts

    def actions(self, view, action):
        return [(b, self.module.views.peek_action(b["callback_data"], self.owner))
                for b in view["buttons"]
                if self.module.views.peek_action(b["callback_data"], self.owner)["action"] == action]

    async def act(self, view, action, **values):
        matches = [(b, record) for b, record in self.actions(view, action)
                   if all(record["values"].get(k) == v for k, v in values.items())]
        self.assertEqual(len(matches), 1, f"missing/ambiguous {action} {values}: {view}")
        return await self.send("callback", matches[0][0]["callback_data"])

    async def edit_text(self, view, value):
        await self.controller.bind_prompt(self.receipt, view, str(500 + self.update))
        return await self.send("reply", json.dumps({"prompt_message_id": str(500 + self.update), "text": value}, ensure_ascii=False))

    def ready(self):
        with closing(self.host.open_store()) as store:
            return store.queue_items()[0], store.cursor("a0")

    async def test_five_account_pages_and_opaque_duplicate_label_selection(self):
        for count in (6, 50):
            self.seed(count)
            view = await self.send("start")
            selected = []
            for page in range((count + 4) // 5):
                accounts = self.actions(view, "account")
                self.assertEqual(len(accounts), min(5, count - page * 5))
                for b, record in accounts:
                    identifier = record["values"]["account_id"]
                    self.assertRegex(b["callback_data"], r"^mn:[A-Za-z0-9_-]{32}$")
                    self.assertNotIn("@example.com", b["callback_data"])
                    self.assertIn("Same label", b["text"])
                    selected.append(identifier)
                self.assertLessEqual(len(view["text"].encode("utf-16-le")) // 2, 3500)
                if page * 5 + 5 < count:
                    view = await self.act(view, "page", page=page + 1)
            self.assertEqual(selected, [f"a{i}" for i in range(count)])
            detail = await self.act(view, "account", account_id=f"a{count - 1}")
            self.assertIn(f"me{count - 1}@example.com", detail["text"])
            self.assertEqual(self.host.events, [])
            await self.act(detail, "cancel")

    async def test_ui_language_change_preserves_summary_ready_text_and_destination(self):
        self.seed()
        before = self.ready()
        view = await self.act(await self.send("start"), "menu", menu="ui_language")
        preview = await self.act(view, "setting", field="ui_language", value="ko")
        self.assertEqual(preview["state"], "awaiting_confirmation")
        self.assertEqual(self.host.settings.ui_language, "en")
        self.assertEqual(preview["language"], "ko")
        await self.act(preview, "confirm")
        self.assertEqual(self.host.settings.ui_language, "ko")
        self.assertEqual(self.host.settings.language, "fr")
        self.assertEqual(self.ready(), before)

    async def test_target_and_summary_changes_require_preview_and_preserve_prepared_work(self):
        self.seed()
        before = self.ready()
        view = await self.act(await self.send("start"), "menu", menu="settings")
        preview = await self.act(view, "current_target")
        self.assertIn("telegram:-222:3", preview["text"])
        self.assertIn("telegram:111:9", preview["text"])
        self.assertEqual(self.host.settings.target, "telegram:-222:3")
        await self.act(preview, "confirm")
        self.assertEqual(self.host.settings.target, "telegram:111:9")
        view = await self.act(await self.send("start"), "menu", menu="language")
        await self.act(await self.act(view, "setting", field="language", value="en"), "confirm")
        self.assertEqual(self.host.settings.language, "en")
        self.assertEqual(self.host.settings.ui_language, "en")
        self.assertEqual(self.ready(), before)

    async def test_schedule_choices_preserve_existing_advanced_value_until_confirmed(self):
        self.seed(schedule="0 8 * * 1-5", max_fetches=3)
        # Use the real host's parser rather than the older two-choice test fake.
        from mail_notifier.hermes_host import HermesHost
        self.host.validate_schedule = lambda value: HermesHost.validate_schedule(self.host, value)
        for minutes in (5, 10, 15, 30, 60):
            before = self.host.settings.schedule
            view = await self.act(await self.send("start"), "menu", menu="schedule")
            self.assertIn(before, view["text"])
            choices = self.actions(view, "setting")
            self.assertEqual([r["values"]["value"] for _, r in choices],
                             ["every 5m", "every 10m", "every 15m", "every 30m", "every 60m"])
            preview = await self.act(view, "setting", field="schedule", value=f"every {minutes}m")
            self.assertEqual(self.host.settings.schedule, before)
            await self.act(preview, "confirm")
            self.assertEqual(self.host.settings.schedule, f"every {minutes}m")
            self.assertEqual(self.host.settings.max_fetches, 3)

    async def test_label_disable_enable_and_rotate_preserve_identity_uid_and_work(self):
        self.seed(2)
        before = self.ready()
        view = await self.act(await self.send("start"), "account", account_id="a0")
        prompt = await self.act(view, "input", field="label")
        preview = await self.edit_text(prompt, "<b>renamed</b> https://example.invalid/picture.jpg")
        await self.act(preview, "confirm")
        self.assertEqual(self.host.settings.accounts[0].id, "a0")
        self.assertEqual(self.host.settings.accounts[0].label, "<b>renamed</b> https://example.invalid/picture.jpg")
        for action, enabled in (("disable", False), ("enable", True)):
            view = await self.act(await self.send("start"), "account", account_id="a0")
            preview = await self.act(view, action)
            self.assertEqual(self.host.settings.accounts[0].enabled, not enabled)
            await self.act(preview, "confirm")
            self.assertEqual(self.host.settings.accounts[0].enabled, enabled)
        self.host.credential_page_url = lambda: "https://operator.example/credential/"
        view = await self.act(await self.send("start"), "account", account_id="a0")
        secret = await self.act(view, "rotate")
        preview, payload = await self.password(secret, "replacement-test-secret")
        self.assertEqual(self.host.passwords["a0"], "old-test-secret")
        await self.act(preview, "confirm")
        self.assertEqual(self.host.passwords["a0"], "replacement-test-secret")
        self.assertEqual(self.ready(), before)
        self.assertNotIn(payload, repr(self.host.settings))

    async def test_connection_changes_disable_old_source_and_require_new_challenge(self):
        for field, value in (("username", "other@example.com"), ("mailbox", "Archive"), ("provider", "naver")):
            self.seed(2)
            before = self.ready()
            view = await self.act(await self.send("start"), "account", account_id="a0")
            if field == "provider":
                choices = await self.act(view, "menu", menu="connection_provider")
                secret = await self.act(choices, "provider", provider=value)
            else:
                prompt = await self.act(view, "input", field=field)
                secret = await self.edit_text(prompt, value)
            self.assertEqual(secret["state"], "awaiting_secret")
            self.assertEqual(len(self.host.settings.accounts), 2)
            self.host.credential_page_url = lambda: "https://operator.example/credential/"
            preview, _ = await self.password(secret)
            self.assertIn("old connection", preview["text"])
            self.assertIn("Naver" if field == "provider" else "Gmail", preview["text"])
            await self.act(preview, "confirm")
            old, _, new = self.host.settings.accounts
            self.assertFalse(old.enabled)
            self.assertNotEqual(new.id, "a0")
            self.assertEqual(getattr(new, field), value)
            self.assertEqual(self.ready(), before)

    async def test_policy_sender_domain_clear_keyword_and_unicode_instructions(self):
        self.seed()
        for field, value, expected in (
            ("important_senders", "boss@example.com, @example.org", ("boss@example.com", "@example.org")),
            ("ignored_senders", "@ads.example", ("@ads.example",)),
            ("always_notify_senders", "@family.example", ("@family.example",)),
            ("important_keywords", "계약, schedule change", ("계약", "schedule change")),
            ("importance_instructions", "  🙂중요\n" * 1000, "  🙂중요\n" * 1000),
        ):
            view = await self.act(await self.send("start"), "menu", menu="policy")
            prompt = await self.act(view, "input", field=field)
            preview = await self.edit_text(prompt, value)
            self.assertEqual(preview["state"], "awaiting_confirmation", preview)
            self.assertNotEqual(getattr(self.host.settings, field), expected)
            await self.act(preview, "confirm")
            self.assertEqual(getattr(self.host.settings, field), expected)
            view = await self.act(await self.send("start"), "menu", menu="policy")
            prompt = await self.act(view, "input", field=field)
            preview = await self.act(prompt, "clear", field=field)
            self.assertEqual(getattr(self.host.settings, field), expected)
            await self.act(preview, "confirm")
            self.assertEqual(getattr(self.host.settings, field), "" if field == "importance_instructions" else ())

    async def test_oversized_input_is_not_truncated_or_saved_and_prompt_can_be_retried(self):
        self.seed(importance_instructions="existing" * 10000)
        existing = self.host.settings.importance_instructions
        view = await self.act(await self.send("start"), "menu", menu="policy")
        prompt = await self.act(view, "input", field="importance_instructions")
        rejected = await self.edit_text(prompt, "🙂" * 4097)
        self.assertEqual(rejected["input_field"], "importance_instructions")
        self.assertIn("16 KiB", rejected["text"])
        self.assertEqual(self.host.settings.importance_instructions, existing)
        preview = await self.edit_text(rejected, "🙂" * 4096)
        await self.act(preview, "confirm")
        self.assertEqual(self.host.settings.importance_instructions, "🙂" * 4096)

    async def test_unrelated_edit_preserves_existing_values_larger_than_new_input_bound(self):
        self.seed(importance_instructions="기존 설정🙂\n" * 10000, important_keywords=("long" * 10000,))
        original = self.host.settings
        view = await self.act(await self.send("start"), "menu", menu="ui_language")
        await self.act(await self.act(view, "setting", field="ui_language", value="ko"), "confirm")
        self.assertEqual(self.host.settings.importance_instructions, original.importance_instructions)
        self.assertEqual(self.host.settings.important_keywords, original.important_keywords)

    async def test_add_after_selecting_an_existing_account_does_not_overwrite_it(self):
        self.seed()
        original = self.host.settings.accounts[0]
        view = await self.act(await self.send("start"), "account", account_id="a0")
        root = await self.act(view, "back")
        provider = await self.act(root, "add")
        prompt = await self.act(provider, "provider", provider="naver")
        secret = await self.edit_text(prompt, "added@example.com")
        self.host.credential_page_url = lambda: "https://operator.example/credential/"
        preview, _ = await self.password(secret)
        await self.act(preview, "confirm")
        self.assertEqual(self.host.settings.accounts[0], original)
        self.assertEqual(len(self.host.settings.accounts), 2)
        self.assertEqual(self.host.settings.accounts[1].username, "added@example.com")

    async def test_resume_requires_explicit_confirmation_even_after_management_handoff(self):
        self.seed()
        self.host.paused = True
        view = await self.act(await self.send("start"), "menu", menu="recovery")
        self.assertTrue(self.host.paused)
        preview = await self.act(view, "resume")
        self.assertTrue(self.host.paused)
        paused = await self.act(preview, "back")
        self.assertTrue(self.host.paused)
        preview = await self.act(await self.act(paused, "menu", menu="recovery"), "resume")
        completed = await self.act(preview, "confirm_resume")
        self.assertEqual(completed["state"], "completed")
        self.assertFalse(self.host.paused)

    async def test_paused_saved_attempt_requires_resume_preview_or_explicit_handoff(self):
        self.seed()
        self.host.paused = True
        view = await self.act(await self.send("start"), "menu", menu="schedule")
        paused = await self.act(await self.act(view, "setting", field="schedule", value="every 10m"), "confirm")
        self.assertEqual(paused["state"], "scheduled")
        preview = await self.act(paused, "resume")
        self.assertTrue(self.host.paused)
        self.assertIn("confirm_resume", [r["action"] for _, r in self.actions(preview, "confirm_resume")])
        paused = await self.act(preview, "recovery_back")
        await self.act(paused, "acknowledge")
        self.assertTrue(self.host.paused)
        root = await self.send("start")
        self.assertEqual(len(self.actions(root, "account")), 1)

    async def test_cron_failure_repair_does_not_repeat_model_credentials_settings_or_baseline(self):
        self.seed()
        before = self.ready()
        self.host.cron_failure = True
        view = await self.act(await self.send("start"), "menu", menu="schedule")
        partial = await self.act(await self.act(view, "setting", field="schedule", value="every 10m"), "confirm")
        self.assertEqual(partial["state"], "repair_required")
        self.assertEqual(self.actions(partial, "repair")[0][1]["values"]["step"], "cron")
        self.host.cron_failure = False
        await self.act(partial, "repair")
        self.assertEqual(self.host.events.count("settings"), 1)
        self.assertEqual(self.host.model_calls, 1)
        self.assertEqual(self.host.events.count("password"), 0)
        self.assertEqual(self.ready(), before)

    async def test_failed_group_delivery_can_handoff_then_confirm_current_private_destination(self):
        self.seed()
        before = self.ready()
        self.host.paused = True
        self.host.send_error = True
        view = await self.act(await self.send("start"), "menu", menu="recovery")
        partial = await self.act(await self.act(view, "test"), "confirm")
        self.assertEqual(partial["state"], "repair_required")
        self.assertIn("telegram:-222:3", partial["text"])
        handoff = self.actions(partial, "acknowledge")
        self.assertEqual(len(handoff), 1, "failed delivery must not trap non-secret management")
        self.assertTrue(handoff[0][1]["values"]["allow_unfinished_delivery"])
        self.assertIn("unfinished", partial["text"])
        acknowledged = await self.act(partial, "acknowledge")
        self.assertEqual(acknowledged["state"], "acknowledged")
        root = await self.send("start")
        self.assertNotEqual(root["draft_id"], partial["draft_id"])
        preview = await self.act(await self.act(root, "menu", menu="settings"), "current_target")
        self.assertEqual(self.host.settings.target, "telegram:-222:3")
        self.host.send_error = False
        saved = await self.act(preview, "confirm")
        self.assertEqual(saved["state"], "scheduled")
        self.assertEqual(self.host.settings.target, "telegram:111:9")
        self.assertTrue(self.host.paused)
        self.assertEqual(self.ready(), before)
        with closing(self.host.open_setup_store()) as store:
            old = store.get(partial["draft_id"], self.owner)
            self.assertEqual(old.steps["test_delivery"], {"started": True})
            self.assertEqual(old.steps["error_code"], "delivery_failed")

    async def test_long_existing_labels_and_values_are_bounded_for_output_only(self):
        accounts = self.seed(50, schedule="🙂" * 5000, importance_instructions="기존" * 20000)
        self.host.settings = replace(self.host.settings, accounts=tuple(replace(a, label="<img src=x>🙂" * 1000) for a in accounts))
        original = self.host.settings
        root = await self.send("start")
        self.assertLessEqual(len(root["text"].encode("utf-16-le")) // 2, 3500)
        self.assertEqual(len(self.actions(root, "account")), 5)
        for b in root["buttons"]:
            self.assertLessEqual(len(b["text"].encode("utf-16-le")) // 2, 180)
        self.assertEqual(self.host.settings, original)

    async def test_back_keeps_pending_edits_reviewable_and_cancel_does_not_publish(self):
        self.seed()
        original = self.host.settings
        view = await self.act(await self.send("start"), "account", account_id="a0")
        preview = await self.edit_text(await self.act(view, "input", field="label"), "Pending rename")
        root = await self.act(preview, "back")
        self.assertIn("Pending rename", root["text"])
        preview = await self.act(root, "review")
        self.assertIn("Same label", preview["text"])
        self.assertIn("Pending rename", preview["text"])
        await self.act(preview, "cancel")
        self.assertEqual(self.host.settings, original)
        self.assertEqual(self.host.events, [])

    async def test_disabling_all_accounts_does_not_claim_automatic_checks_are_running(self):
        self.seed()
        view = await self.act(await self.send("start"), "account", account_id="a0")
        complete = await self.act(await self.act(view, "disable"), "confirm")
        self.assertIn("All accounts are disabled", complete["text"])
        self.assertNotIn("model check, schedule and Telegram test verified", complete["text"])
        self.assertEqual(self.host.deliveries, [])

    async def test_disable_one_account_during_outage_needs_confirm_but_no_activation_probes(self):
        self.seed(2)
        self.host.paused = True
        before = self.ready()
        view = await self.act(await self.send("start"), "account", account_id="a0")
        preview = await self.act(view, "disable")
        self.assertTrue(self.host.settings.accounts[0].enabled)
        self.assertIn("not run", preview["text"])
        with patch.object(self.host, "check_environment", side_effect=AssertionError("reload outage")), \
                patch.object(self.host, "complete_structured", side_effect=AssertionError("model outage")), \
                patch.object(self.host, "send", side_effect=AssertionError("delivery outage")):
            saved = await self.act(preview, "confirm")
        self.assertEqual(saved["state"], "scheduled")
        self.assertFalse(self.host.settings.accounts[0].enabled)
        self.assertTrue(self.host.settings.accounts[1].enabled)
        self.assertTrue(self.host.paused)
        self.assertEqual(self.ready(), before)
        self.assertIn("not run", saved["text"])
        self.assertNotIn("Telegram test verified", saved["text"])
        self.assertFalse(self.actions(saved, "resume"))
        await self.act(saved, "acknowledge")

    async def test_disable_with_unrelated_draft_target_change_still_requires_activation_preflight(self):
        self.seed(2)
        root = await self.send("start")
        preview = await self.act(await self.act(root, "menu", menu="settings"), "current_target")
        root = await self.act(preview, "back")
        preview = await self.act(await self.act(root, "account", account_id="a0"), "disable")
        with patch.object(self.host, "check_environment", side_effect=HostError("model_unavailable")):
            failed = await self.act(preview, "confirm")
        self.assertEqual(failed["state"], "awaiting_confirmation")
        self.assertTrue(self.host.settings.accounts[0].enabled)
        self.assertEqual(self.host.settings.target, "telegram:-222:3")

    async def test_korean_policy_settings_and_test_panel_use_same_confirmed_path(self):
        self.seed()
        self.host.settings = replace(self.host.settings, ui_language="ko", language="en")
        root = await self.send("start", language="en")
        self.assertEqual(root["language"], "ko")
        view = await self.act(root, "menu", menu="policy")
        preview = await self.edit_text(await self.act(view, "input", field="important_keywords"), "일정,계약")
        self.assertIn("변경", preview["text"])
        await self.act(preview, "confirm")
        self.assertEqual(self.host.settings.important_keywords, ("일정", "계약"))
        view = await self.act(await self.send("start"), "menu", menu="recovery")
        deliveries = len(self.host.deliveries)
        preview = await self.act(view, "test")
        self.assertEqual(len(self.host.deliveries), deliveries)
        await self.act(preview, "confirm")
        self.assertEqual(len(self.host.deliveries), deliveries + 1)
        self.assertEqual(self.host.settings.language, "en")

    async def test_conflicting_review_handoff_rejects_stale_current_fingerprint(self):
        self.seed()
        view = await self.act(await self.send("start"), "menu", menu="language")
        preview = await self.act(view, "setting", field="language", value="en")
        self.host.settings = replace(self.host.settings, target="telegram:333")
        conflict = await self.act(preview, "confirm")
        self.assertEqual(conflict["state"], "needs_review")
        self.host.settings = replace(self.host.settings, target="telegram:444")
        rejected = await self.act(conflict, "acknowledge")
        self.assertEqual(rejected["error"], "setup_changed_retry")
        self.assertEqual(self.host.events, [])

    async def test_conflict_review_shows_current_and_candidate_policy_values(self):
        self.seed(importance_instructions="original criteria")
        view = await self.act(await self.send("start"), "menu", menu="policy")
        preview = await self.edit_text(await self.act(view, "input", field="importance_instructions"), "candidate criteria")
        self.host.settings = replace(self.host.settings, importance_instructions="external criteria")
        conflict = await self.act(preview, "confirm")
        self.assertEqual(conflict["state"], "needs_review")
        self.assertIn("external criteria", conflict["text"])
        self.assertIn("candidate criteria", conflict["text"])
        self.assertLessEqual(len(conflict["text"].encode("utf-16-le")) // 2, 3500)

    async def test_conflict_paginates_current_accounts_and_compares_union_by_identity(self):
        for candidate_count, current_count in ((1, 8), (8, 1)):
            with self.subTest(candidate=candidate_count, current=current_count):
                accounts = self.seed(8)
                self.host.settings = replace(self.host.settings, accounts=accounts[:candidate_count])
                preview = await self.act(await self.act(await self.send("start"), "menu", menu="language"),
                                         "setting", field="language", value="en")
                current_accounts = tuple(reversed(accounts[:current_count]))
                self.host.settings = replace(self.host.settings, accounts=current_accounts)
                conflict = await self.act(preview, "confirm")
                self.assertEqual(conflict["state"], "needs_review")
                for page in range((current_count + 4) // 5):
                    for account in current_accounts[page * 5:page * 5 + 5]:
                        self.assertIn(account.username, conflict["text"])
                    if page * 5 + 5 < current_count:
                        conflict = await self.act(conflict, "page", page=page + 1)
                self.assertFalse(any(r["values"]["page"] > page for _, r in self.actions(conflict, "page")))
                pages = await self.change_pages(conflict)
                comparisons = "\n".join(p["text"].split("Changes · Page", 1)[1] for p in pages)
                for account in accounts[1:]:
                    self.assertIn(account.username, comparisons)
                self.assertEqual(comparisons.count("Current only:"), max(0, current_count - candidate_count))
                self.assertEqual(comparisons.count("Candidate only:"), max(0, candidate_count - current_count))
                self.assertNotIn("Account state:", comparisons, "reordering must not invent identity changes")
                for p in pages:
                    self.assertLessEqual(len(p["text"].encode("utf-16-le")) // 2, 3500)
                    for button in p["buttons"]:
                        self.assertRegex(button["callback_data"], r"^mn:[A-Za-z0-9_-]{32}$")
                resource = self.module.views._shared().runtime.profiles[self.owner.profile_home].resource
                draft = resource.service.start(self.owner)
                for selected, expected in ((-100, 1), (100, (current_count + 4) // 5)):
                    bounded = self.module.views.panel(draft, self.host.settings, language="en", page=selected)
                    self.assertIn(f"Page {expected}/{(current_count + 4) // 5}", bounded["text"])
                current = self.host.settings
                self.host.settings = replace(current, target="telegram:999")
                rejected = await self.act(pages[-1], "acknowledge")
                self.assertEqual(rejected["error"], "setup_changed_retry")
                fresh_review = await self.send("start")
                await self.act(fresh_review, "acknowledge")
                self.assertEqual(self.host.settings.accounts, current_accounts)
                self.assertEqual(self.host.events, [])

    async def pending_policy_preview(self, count=1, language="en"):
        fields = ("important_senders", "ignored_senders", "always_notify_senders", "important_keywords")
        old = {field: (f"OLD-{field}-" + "x" * 150 + "@example.com",) for field in fields}
        new = {field: (f"NEW-{field}-" + "y" * 150 + "@example.org",) for field in fields}
        old["importance_instructions"] = "OLD-INSTRUCTION-MARKER"
        new["importance_instructions"] = "NEW-INSTRUCTION-MARKER"
        self.seed(count, **old)
        self.host.settings = replace(self.host.settings, ui_language=language)
        root = await self.send("start")
        for index, (field, value) in enumerate(new.items()):
            policy = await self.act(root, "menu", menu="policy")
            prompt = await self.act(policy, "input", field=field)
            preview = await self.edit_text(prompt, value if isinstance(value, str) else value[0])
            if index < len(new) - 1:
                root = await self.act(preview, "back")
        return preview, old, new

    async def change_pages(self, view):
        pages = [view]
        for page in range(1, 100):
            next_buttons = [(b, record) for b, record in self.actions(view, "changes_page") if record["values"]["page"] == page]
            if not next_buttons:
                return pages
            self.assertEqual(len(next_buttons), 1)
            view = await self.send("callback", next_buttons[0][0]["callback_data"])
            self.assertNotIn("error", view)
            pages.append(view)
        self.fail("change pagination did not terminate")

    async def test_all_pending_policy_changes_remain_reviewable_across_change_pages(self):
        preview, old, new = await self.pending_policy_preview()
        pages = await self.change_pages(preview)
        self.assertGreater(len(pages), 1, "long comparison must expose further change pages")
        combined = "\n".join(page["text"] for page in pages)
        for field in new:
            self.assertIn("OLD-" + field if field != "importance_instructions" else old[field], combined)
            self.assertIn("NEW-" + field if field != "importance_instructions" else new[field], combined)
        for page in pages:
            self.assertLessEqual(len(page["text"].encode("utf-16-le")) // 2, 3500)
            self.assertIn("long values abbreviated", page["text"])
        self.assertEqual(self.host.settings.importance_instructions, "OLD-INSTRUCTION-MARKER")
        self.assertEqual(self.host.events, [])
        await self.act(pages[-1], "confirm")
        for field, value in new.items():
            self.assertEqual(getattr(self.host.settings, field), value)

    async def test_conflict_comparison_keeps_all_current_and_candidate_changes_reachable(self):
        preview, _, new = await self.pending_policy_preview()
        external = {field: (f"EXTERNAL-{field}-" + "z" * 150 + "@external.example",) for field in new if field != "importance_instructions"}
        external["importance_instructions"] = "EXTERNAL-INSTRUCTION-MARKER"
        self.host.settings = replace(self.host.settings, **external)
        conflict = await self.act(preview, "confirm")
        self.assertEqual(conflict["state"], "needs_review")
        pages = await self.change_pages(conflict)
        self.assertGreater(len(pages), 1)
        combined = "\n".join(page["text"] for page in pages)
        for field in new:
            self.assertIn("EXTERNAL-" + field if field != "importance_instructions" else external[field], combined)
            self.assertIn("NEW-" + field if field != "importance_instructions" else new[field], combined)
        for page in pages:
            self.assertLessEqual(len(page["text"].encode("utf-16-le")) // 2, 3500)
        self.assertEqual(self.host.settings.importance_instructions, "EXTERNAL-INSTRUCTION-MARKER")
        self.assertEqual(self.host.events, [])

    async def test_change_page_tokens_bind_owner_revision_and_selected_account_page(self):
        preview, _, _ = await self.pending_policy_preview(count=6)
        preview = await self.act(preview, "page", page=1)
        self.assertIn("me5@example.com", preview["text"])
        buttons = self.actions(preview, "changes_page")
        self.assertTrue(buttons, "change pages must be separate from account pages")
        token = next(b["callback_data"] for b, record in buttons if record["values"]["page"] == 1)
        self.assertRegex(token, r"^mn:[A-Za-z0-9_-]{32}$")
        foreign = replace(self.receipt, owner=replace(self.owner, user_id="222", chat_id="222"), message_id="777")
        denied = await self.controller.handle(foreign, NativeInput("callback", "700", "222", "222", "9", "777", "777", "en", token))
        self.assertEqual(denied["error"], "setup_changed_retry")
        second = await self.send("callback", token)
        self.assertEqual(second["revision"], preview["revision"])
        self.assertIn("me5@example.com", second["text"])
        first_accounts = await self.act(second, "page", page=0)
        self.assertIn("me0@example.com", first_accounts["text"])
        self.assertIn("NEW-INSTRUCTION-MARKER", first_accounts["text"])
        second = first_accounts
        back_token = self.actions(second, "changes_page")[0][0]["callback_data"]
        await self.act(second, "back")
        stale = await self.send("callback", back_token)
        self.assertEqual(stale["error"], "setup_changed_retry")
        self.assertEqual(self.host.events, [])

    async def test_korean_conflict_with_fifty_long_accounts_keeps_every_comparison_row(self):
        preview, _, _ = await self.pending_policy_preview(count=50, language="ko")
        self.host.settings = replace(self.host.settings, accounts=tuple(
            replace(account, label=f"EXTERNAL-ALIAS-{index}-" + "🙂" * 200)
            for index, account in enumerate(self.host.settings.accounts)))
        conflict = await self.act(preview, "confirm")
        self.assertEqual(conflict["state"], "needs_review")
        pages = await self.change_pages(conflict)
        self.assertGreater(len(pages), 5)
        combined = "\n".join(page["text"] for page in pages)
        for index in range(50):
            self.assertIn(f"EXTERNAL-ALIAS-{index}-", combined)
        self.assertIn("OLD-INSTRUCTION-MARKER", combined)
        self.assertIn("NEW-INSTRUCTION-MARKER", combined)
        for page in pages:
            self.assertLessEqual(len(page["text"].encode("utf-16-le")) // 2, 3500)
            self.assertIn("표시만 요약", page["text"])
            self.assertIn("설정 발행 여부: 아니요", page["text"])
        self.assertEqual(self.host.events, [])

    async def test_sdk_plain_text_explicitly_disables_parse_and_link_previews(self):
        self.seed()
        from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
        sdk = ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
        sent = []
        async def send_message(**kwargs):
            sent.append(kwargs)
            return SimpleNamespace(message_id=900)
        await self.controller._send_view(self.receipt, await self.send("start"), SimpleNamespace(send_message=send_message), sdk)
        self.assertTrue(sent)
        for message in sent:
            self.assertIsNone(message["parse_mode"])
            self.assertTrue(message["disable_web_page_preview"])


class NativeManagementBoundsTests(unittest.TestCase):
    def test_only_text_replies_allow_sixteen_kib_without_widening_encrypted_wire(self):
        with tempfile.TemporaryDirectory() as home:
            broker = AdmissionBroker(home)
            self.addCleanup(broker.close)
            def item(kind, value):
                return NativeInput(kind, "700", "111", "111", "", "42", "u1", "en", value)
            wire = json.dumps({"prompt_message_id": "500", "text": "🙂" * 4096}, ensure_ascii=False)
            reference = broker.offer(item("reply", wire), now=1)
            self.assertTrue(reference)
            escaped = json.dumps({"prompt_message_id": "500", "text": "\u0001" * 16384})
            self.assertTrue(broker.offer(replace(item("reply", escaped), update_key="u2"), now=1))
            for kind, value in (("reply", json.dumps({"prompt_message_id": "500", "text": "🙂" * 4097}, ensure_ascii=False)),
                                ("web_app", "x" * 4097), ("callback", "x" * 4097),
                                ("reply", '{"text":"hello","password":"unexpected"}'),
                                ("reply", json.dumps({"prompt_message_id": "500", "text": ["bad"]}))):
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    broker.offer(item(kind, value), now=1)
