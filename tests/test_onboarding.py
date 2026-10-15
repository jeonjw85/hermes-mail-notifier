import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from mail_notifier.config import Settings
from mail_notifier.hermes_host import HostError
from mail_notifier.onboarding_auth import SetupOwner
from tests.support import feature
from tests.test_cli import SetupHost
from tests.test_service import FakeMailbox


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.onboarding")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SetupHost(self.temp.name)
        self.now = 1000
        self.highest = 100
        self.probes = []
        self.owner = SetupOwner(str(self.host.home()), "cli")
        self.service = self.new_service()

    def new_service(self):
        return self.module.OnboardingService(self.host, mailbox_factory=self.mailbox, clock=lambda: self.now)

    def mailbox(self, account, password):
        self.probes.append(account.id)
        if password == "wrong":
            raise HostError("imap_authentication_failed")
        box = FakeMailbox(account)
        box.highest = self.highest
        return box

    def change(self, draft, operation, **values):
        return self.service.change(draft.id, draft.owner, draft.revision, action=operation, values=values)

    def validated(self, draft=None, password="test-only-secret"):
        draft = draft or self.service.start(self.owner, detected_language="en_US.UTF-8")
        draft = self.change(draft, "add", provider="gmail", username="me@example.com", label="Work")
        return self.service.validate_password(draft.id, self.owner, draft.revision, password)

    def stored(self, draft):
        store = self.host.open_setup_store()
        try:
            return store.get(draft.id, draft.owner)
        finally:
            store.close()

    def commit(self, draft, **kwargs):
        return self.service.commit(draft.id, draft.owner, draft.revision, **kwargs)

    def test_confirmation_is_required_before_publication_and_snapshot_is_fresh(self):
        draft = self.validated()
        self.assertEqual(draft.state, "awaiting_confirmation")
        self.assertEqual(self.host.passwords, {})
        self.assertEqual(self.host.load_settings().accounts, ())
        preview = self.service.preview(draft.id, self.owner)
        self.assertEqual(preview["accounts"][0]["username"], "me@example.com")
        self.assertNotIn("test-only-secret", repr((preview, draft, self.service)))
        self.highest = 200
        result = self.commit(draft)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.host.schedule_calls, 1)
        self.assertEqual(self.host.events, ["password", "settings", "cron"])
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(draft.candidate.accounts[0].id), (7, 200))
        finally:
            store.close()
        self.assertEqual(self.host.model_calls, 1)
        self.assertEqual(self.host.model_timeouts, [45])
        self.assertNotIn("test-only-secret", json.dumps(self.stored(draft).to_dict()))
        self.assertEqual(self.commit(self.stored(draft)), result)

    def test_invalid_validation_and_cancel_have_no_external_writes(self):
        draft = self.validated(password="wrong")
        self.assertEqual(draft.state, "awaiting_secret")
        draft = self.service.cancel(draft.id, self.owner, draft.revision)
        self.assertEqual(draft.state, "cancelled")
        self.assertEqual(self.host.events, [])
        self.assertEqual(self.host.passwords, {})
        with self.assertRaises(HostError):
            self.commit(draft)

    def test_duplicate_connection_rejected_and_untrusted_fields_rejected(self):
        draft = self.validated()
        for action, values in (("add", {"provider": "gmail", "username": "me@example.com", "label": "Other"}),
                               ("edit", {"account_id": draft.candidate.accounts[0].id, "id": "chosen"}),
                               ("settings", {"host": "evil"}), ("form", {"password": "secret"})):
            with self.subTest(action=action), self.assertRaises(HostError):
                self.service.change(draft.id, self.owner, draft.revision, action=action, values=values)

    def test_rename_rotation_and_identity_changes_preserve_old_uid_and_work(self):
        draft = self.validated()
        self.commit(draft)
        account = self.host.load_settings().accounts[0]
        store = self.host.open_store()
        store.enqueue(account, 7, [101])
        store.close()
        draft = self.service.start(self.owner)
        draft = self.change(draft, "edit", account_id=account.id, label="Renamed")
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "replacement")
        self.highest = 400
        self.assertEqual(self.commit(draft)["state"], "completed")
        self.assertEqual(self.host.load_settings().accounts[0].id, account.id)
        self.assertEqual(self.host.passwords[account.id], "replacement")
        draft = self.service.start(self.owner)
        draft = self.change(draft, "edit", account_id=account.id, username="new@example.com")
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "new-secret")
        self.assertEqual(self.commit(draft)["state"], "completed")
        old, new = self.host.load_settings().accounts
        self.assertFalse(old.enabled)
        self.assertNotEqual(new.id, old.id)
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(old.id), (7, 101))
            self.assertEqual(store.cursor(new.id), (7, 400))
            self.assertEqual(store.status()["pending"], 1)
        finally:
            store.close()

    def test_saved_settings_preserved_and_locale_does_not_replace_summary_language(self):
        self.host.settings = Settings(schedule="advanced schedule", target="telegram:123:4", language="fr",
                                      max_fetches=3, important_keywords=("urgent",))
        draft = self.service.start(self.owner, detected_language="ko_KR")
        self.assertEqual(draft.candidate.language, "fr")
        self.assertEqual(draft.candidate.ui_language, "ko")
        self.assertEqual(draft.candidate.schedule, "advanced schedule")
        self.assertEqual(draft.candidate.target, "telegram:123:4")
        self.assertEqual(draft.candidate.max_fetches, 3)
        draft = self.change(draft, "settings", ui_language="en")
        self.assertEqual(draft.candidate.language, "fr")

    def test_busy_and_conflict_preserve_input_and_do_not_publish(self):
        draft = self.validated()
        store = self.host.open_store()
        with store.run_lock():
            result = self.commit(draft)
        store.close()
        self.assertEqual(result["error"], "already_running")
        self.assertEqual(self.stored(draft).candidate, draft.candidate)
        self.assertEqual(self.host.events, [])
        self.host.settings = replace(self.host.settings, language="fr")
        result = self.commit(self.stored(draft))
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(self.host.events, [])

    def test_revision_and_thread_checks_and_form_resume(self):
        draft = self.service.start(self.owner)
        changed = self.change(draft, "form", action="add", provider="gma", username="incomplete")
        self.assertEqual(self.service.start(self.owner), changed)
        with self.assertRaises(HostError):
            self.change(draft, "settings", language="en")
        with self.assertRaises(HostError):
            self.service.preview(changed.id, replace(self.owner, thread_id="other"))
        self.assertEqual(changed.candidate.accounts, ())

    def test_secret_expiry_restart_and_nonsecret_revision_rebinding(self):
        draft = self.validated()
        draft = self.change(draft, "settings", language="fr")
        self.assertEqual(self.commit(draft)["state"], "completed")
        for restart in (False, True):
            draft = self.service.start(self.owner)
            account = draft.candidate.accounts[0]
            draft = self.change(draft, "edit", account_id=account.id, label="Rotation")
            draft = self.service.validate_password(draft.id, self.owner, draft.revision, "replacement")
            if restart:
                self.service = self.new_service()
            else:
                self.now += 600
            result = self.commit(draft)
            self.assertEqual(result["state"], "needs_secret")
            self.assertEqual(self.host.passwords[account.id], "test-only-secret")
            draft = self.stored(draft)
            self.service.cancel(draft.id, self.owner, draft.revision)

    def test_connection_change_invalidates_secret_and_expiry_prunes_reads(self):
        draft = self.validated()
        draft = self.change(draft, "edit", account_id=draft.candidate.accounts[0].id, mailbox="Other")
        self.assertEqual(self.commit(draft)["state"], "needs_secret")
        self.now += 86400
        self.service.expire()
        self.assertEqual(self.service.preview(draft.id, self.owner)["state"], "expired")
        self.assertEqual(self.host.events, [])

    def test_preflight_failures_and_concurrent_cancel_prevent_writes(self):
        for method in ("check_environment", "complete_structured", "resolve_target"):
            draft = self.validated()
            with patch.object(self.host, method, side_effect=HostError("credentials_not_reloadable")):
                result = self.commit(draft)
            self.assertNotEqual(result["state"], "completed")
            self.assertEqual(self.host.events, [])
            current = self.stored(draft)
            self.service.cancel(current.id, self.owner, current.revision)
        draft = self.validated()
        original = self.host.check_environment
        def cancel(settings):
            self.service.cancel(draft.id, self.owner, draft.revision)
            original(settings)
        with patch.object(self.host, "check_environment", side_effect=cancel), self.assertRaises(HostError):
            self.commit(draft)
        self.assertEqual(self.host.events, [])

    def test_cron_and_send_repair_need_no_password_or_new_baseline(self):
        draft = self.validated()
        self.host.cron_failure = True
        result = self.commit(draft)
        self.assertEqual(result["state"], "repair_required")
        self.assertIn("settings", result["completed_steps"])
        self.host.cron_failure = False
        self.host.send_error = True
        self.service = self.new_service()
        draft = self.stored(draft)
        probes = len(self.probes)
        result = self.service.repair(draft.id, self.owner, draft.revision, step="cron")
        self.assertEqual(result["state"], "repair_required")
        self.assertEqual(len(self.probes), probes)
        self.host.send_error = False
        draft = self.stored(draft)
        result = self.service.repair(draft.id, self.owner, draft.revision, step="test_delivery")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.host.events.count("password"), 1)
        self.assertEqual(self.host.events.count("settings"), 1)

    def test_ambiguous_password_only_write_never_accepts_old_secret_as_success(self):
        self.commit(self.validated())
        draft = self.service.start(self.owner)
        account = draft.candidate.accounts[0]
        draft = self.change(draft, "edit", account_id=account.id)
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "replacement")
        with patch.object(self.host, "save_password", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.commit(draft)
        self.service = self.new_service()
        draft = self.stored(draft)
        result = self.service.repair(draft.id, self.owner, draft.revision, step="credentials")
        self.assertEqual(result["state"], "needs_secret")
        self.assertEqual(self.host.passwords[account.id], "test-only-secret")
        draft = self.stored(draft)
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "replacement")
        self.assertEqual(self.commit(draft)["state"], "completed")

    def test_crash_after_settings_publication_recovers_without_secret(self):
        draft = self.validated()
        save = self.host.save_settings
        def crash(settings):
            save(settings)
            raise SystemExit
        with patch.object(self.host, "save_settings", side_effect=crash), self.assertRaises(SystemExit):
            self.commit(draft)
        self.service = self.new_service()
        draft = self.stored(draft)
        result = self.service.repair(draft.id, self.owner, draft.revision, step="settings")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.host.events.count("password"), 1)

    def test_paused_cron_requires_explicit_resume_and_completion_is_honest(self):
        self.host.paused = True
        draft = self.validated()
        result = self.commit(draft)
        self.assertEqual(result["state"], "scheduled")
        self.assertEqual(result["next_action"], "resume_cron")
        self.assertTrue(self.host.paused)
        draft = self.stored(draft)
        result = self.service.repair(draft.id, self.owner, draft.revision, step="cron", resume_cron=True)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(self.host.paused)

    def test_repair_after_verified_credential_before_baseline_still_initializes_new_account(self):
        draft = self.validated()
        save = self.service._save
        def crash(current, **changes):
            if "baselines" in changes.get("steps", {}):
                raise SystemExit
            return save(current, **changes)
        with patch.object(self.service, "_save", side_effect=crash), self.assertRaises(SystemExit):
            self.commit(draft)
        self.service = self.new_service()
        draft = self.stored(draft)
        self.highest = 300
        result = self.service.repair(draft.id, self.owner, draft.revision, step="baselines")
        self.assertEqual(result["state"], "completed")
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(draft.candidate.accounts[0].id), (7, 300))
        finally:
            store.close()

    def test_multiple_pending_secrets_survive_nonsecret_changes_but_not_cancel(self):
        draft = self.validated()
        draft = self.change(draft, "add", provider="naver", username="second@example.com")
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "second-secret")
        draft = self.change(draft, "settings", target="telegram:222", ui_language="ko")
        self.assertEqual(self.commit(draft)["state"], "completed")
        self.assertEqual(set(self.host.passwords.values()), {"test-only-secret", "second-secret"})
        draft = self.service.start(self.owner)
        draft = self.change(draft, "edit", account_id=draft.candidate.accounts[0].id)
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "cancelled-secret")
        self.service.cancel(draft.id, self.owner, draft.revision)
        self.assertEqual(self.service._secrets, {})
        self.assertNotIn(b"cancelled-secret", (self.host.path / "setup.db").read_bytes())

    def test_readback_must_match_replacement_and_errors_are_not_persisted_raw(self):
        self.commit(self.validated())
        draft = self.service.start(self.owner)
        account = draft.candidate.accounts[0]
        draft = self.change(draft, "edit", account_id=account.id)
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "replacement")
        with patch.object(self.host, "save_password", return_value=None):
            result = self.commit(draft)
        self.assertEqual((result["state"], result["error"]), ("needs_secret", "credential_save_failed"))
        with self.assertRaises(HostError):
            current = self.stored(draft)
            self.service.cancel(current.id, self.owner, current.revision)
        self.assertNotIn(b"replacement", (self.host.path / "setup.db").read_bytes())
        current = self.stored(draft)
        with patch.object(self.host, "get_password", side_effect=HostError("private_secret_value")):
            current = self.service.validate_password(current.id, self.owner, current.revision, "replacement")
            result = self.commit(current)
        self.assertNotIn("private_secret_value", repr((result, self.stored(draft).to_dict())))

    def test_settings_changed_during_preflight_and_stale_validation_cannot_publish(self):
        draft = self.validated()
        def change(settings):
            self.host.settings = replace(self.host.settings, target="telegram:555")
        with patch.object(self.host, "check_environment", side_effect=change):
            result = self.commit(draft)
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(self.host.events, [])
        current = self.stored(draft)
        self.service.cancel(current.id, self.owner, current.revision)
        draft = self.service.start(self.owner)
        draft = self.change(draft, "add", provider="gmail", username="me@example.com")
        factory = self.service.mailbox_factory
        def cancel(account, password):
            current = self.stored(draft)
            self.service.cancel(current.id, self.owner, current.revision)
            return factory(account, password)
        self.service.mailbox_factory = cancel
        with self.assertRaises(HostError):
            self.service.validate_password(draft.id, self.owner, draft.revision, "not-stored")
        self.assertEqual(self.service._secrets, {})

    def test_crash_while_revalidating_existing_account_requires_new_secret(self):
        self.commit(self.validated())
        draft = self.service.start(self.owner)
        account = draft.candidate.accounts[0]
        draft = self.change(draft, "edit", account_id=account.id)
        draft = self.service.validate_password(draft.id, self.owner, draft.revision, "")
        with patch.object(self.service, "mailbox_factory", side_effect=SystemExit), self.assertRaises(SystemExit):
            self.service.validate_password(draft.id, self.owner, draft.revision, "interrupted-replacement")
        self.service = self.new_service()
        draft = self.service.start(self.owner)
        self.assertEqual(draft.state, "needs_secret")
        self.assertEqual(self.commit(draft)["state"], "needs_secret")
        self.assertEqual(self.host.passwords[account.id], "test-only-secret")

    def test_secret_expiring_between_write_intent_and_actual_write_is_not_written(self):
        draft = self.validated()
        save = self.service._save
        def expire_at_intent(current, **changes):
            if any(m.get("write_started") and not m.get("verified") for m in changes.get("steps", {}).get("credentials", {}).values()):
                self.now = 1600
            return save(current, **changes)
        with patch.object(self.service, "_save", side_effect=expire_at_intent):
            result = self.commit(draft)
        self.assertEqual(result["state"], "needs_secret")
        self.assertEqual(self.host.passwords, {})

    def test_cron_repair_reconciles_an_external_resume_without_resuming_itself(self):
        self.host.paused = True
        draft = self.validated()
        result = self.commit(draft)
        self.assertEqual(result["state"], "scheduled")
        self.host.paused = False
        draft = self.stored(draft)
        with patch.object(self.host, "resume_schedule", side_effect=AssertionError("implicit resume")):
            result = self.service.repair(draft.id, self.owner, draft.revision, step="cron")
        self.assertEqual(result["state"], "completed")

    def test_expired_published_draft_cannot_run_repair_side_effects(self):
        draft = self.validated()
        self.host.cron_failure = True
        self.commit(draft)
        events = list(self.host.events)
        calls = self.host.schedule_calls
        self.now += 86400
        draft = self.service.start(self.owner)
        # start creates a fresh draft after expiry; retrieve the expired attempt
        # through its original ID to ensure it cannot authorize recovery.
        store = self.host.open_setup_store()
        expired_id = store.db.execute("SELECT id FROM drafts WHERE state='expired'").fetchone()[0]
        expired = store.get(expired_id, self.owner)
        store.close()
        self.host.cron_failure = False
        with self.assertRaises(HostError):
            self.service.repair(expired.id, self.owner, expired.revision, step="cron")
        self.assertEqual(self.host.events, events)
        self.assertEqual(self.host.schedule_calls, calls)
