import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from mail_notifier.config import Account
from mail_notifier.imap_client import MailboxError
from tests.support import account_data, feature
from tests.test_cli import SetupHost
from tests.test_service import FakeMailbox


class SetupRecoveryTests(unittest.TestCase):
    def setUp(self):
        locale = patch.dict("os.environ", {"LC_ALL": "en_US.UTF-8"})
        locale.start()
        self.addCleanup(locale.stop)
        self.module = feature(self, "mail_notifier.setup")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SetupHost(self.temp.name)
        self.output = io.StringIO()
        self.prompts, self.probes = [], []

    def run_setup(self, answers, passwords=("test-only",), **kwargs):
        answers, passwords = iter(answers), iter(passwords)
        def read(prompt):
            self.prompts.append(prompt)
            return next(answers, "")
        def mailbox(account, password):
            self.probes.append((account.username, password))
            if password == "wrong":
                raise MailboxError("imap_authentication_failed")
            return FakeMailbox(account)
        with redirect_stdout(self.output):
            return self.module.setup_plugin(self.host, read=read, read_secret=lambda _: next(passwords),
                                            mailbox_factory=mailbox, **kwargs)

    def test_invalid_fields_are_reentered_without_losing_the_draft(self):
        self.assertEqual(self.run_setup(["gmial", "gmail", "bad\x01name", "me@example.com", "yes", "Work", "INBOX", "no", "yes"]), 0)
        self.assertEqual(self.host.settings.accounts[0].username, "me@example.com")
        self.assertEqual(self.host.settings.accounts[0].label, "Work")
        self.assertEqual(len(self.probes), 2)

    def test_password_retry_preserves_other_account_and_never_prints_passwords(self):
        old = replace(Account(**account_data("z1")), enabled=False)
        self.host.settings = replace(self.host.settings, accounts=(old,))
        answers = ["add", "gmail", "one@example.com", "no",
                   "add", "naver", "two@example.com", "no", "password", "done", "no", "yes"]
        self.assertEqual(self.run_setup(answers, ("first-secret", "wrong", "second-secret")), 0)
        self.assertEqual(self.host.settings.accounts[0], old)
        self.assertEqual([a.label for a in self.host.settings.accounts[1:]], ["one@example.com", "two@example.com"])
        self.assertEqual(set(self.host.passwords.values()), {"first-secret", "second-secret"})
        self.assertNotIn("second-secret", self.output.getvalue())
        self.assertEqual(len(self.probes), 5)

    def test_cancel_during_retry_has_no_published_side_effects(self):
        self.assertEqual(self.run_setup(["gmail", "one@example.com", "no", "cancel"], ("wrong",)), 1)
        self.assertEqual(self.host.settings.accounts, ())
        self.assertEqual(self.host.passwords, {})
        self.assertEqual(self.host.schedule_calls, 0)

    def test_bad_schedule_is_reprompted_before_settings_are_saved(self):
        self.assertEqual(self.run_setup(["gmail", "one@example.com", "no", "yes",
                                        "", "evry five minutes", "every 10m", "", "", "", "", "", "", "", "yes"], ui_language="ko"), 0)
        self.assertEqual(self.host.settings.schedule, "every 10m")
        self.assertEqual(sum(prompt.startswith("확인 주기") for prompt in self.prompts), 2)

    def test_account_add_and_edit_skip_global_settings_and_preserve_them(self):
        self.host.settings = replace(self.host.settings, target="telegram:123", importance_instructions="work", max_fetches=3)
        self.assertEqual(self.run_setup(["gmail", "one@example.com", "no", "yes"], account_action="add"), 0)
        account = self.host.settings.accounts[0]
        self.assertEqual(self.run_setup(["", "", "Renamed", "", "yes"], account_action="edit", account_id=account.id), 0)
        self.assertEqual(self.host.settings.accounts[0].id, account.id)
        self.assertEqual(self.host.settings.target, "telegram:123")
        self.assertEqual(self.host.settings.max_fetches, 3)
        self.assertFalse(any("Telegram 목적지" in prompt for prompt in self.prompts))

    def test_setup_confirmation_uses_summary_language_and_preserves_ui_selection(self):
        self.host.settings = replace(self.host.settings, language="en", ui_language="ko")
        self.assertEqual(self.run_setup(["gmail", "one@example.com", "no", "yes"], account_action="add"), 0)
        self.assertEqual(self.host.settings.language, "en")
        self.assertEqual(self.host.settings.ui_language, "ko")
        self.assertTrue(self.host.deliveries[0][1].startswith(
                         "Mail notifications are configured. Important new emails arriving after connection will be summarized."))

    def test_accounts_cli_lists_disables_and_enables_without_reset(self):
        self.assertEqual(self.run_setup(["gmail", "one@example.com", "no", "yes"], account_action="add"), 0)
        account = self.host.settings.accounts[0]
        cli = feature(self, "mail_notifier.cli")
        parser = argparse.ArgumentParser()
        cli.configure_parser(parser)
        with redirect_stdout(self.output), patch.object(cli, "ImapMailbox", lambda *a: FakeMailbox(account)):
            for action in ("disable", "enable"):
                self.assertEqual(cli.run_cli(parser.parse_args(["accounts", action, account.id, "--json"]), self.host), 0)
            self.assertEqual(cli.run_cli(parser.parse_args(["accounts", "list", "--json"]), self.host), 0)
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(account.id), (7, 100))
        finally:
            store.close()

    def test_passwords_require_a_real_terminal_and_getpass_never_falls_back(self):
        import getpass
        import warnings
        from mail_notifier.hermes_host import HostError
        with patch.object(self.module.sys.stdin, "isatty", return_value=False), self.assertRaises(HostError):
            self.module._password(getpass.getpass)
        def fallback(prompt):
            warnings.warn("unsafe terminal", getpass.GetPassWarning)
            return "must-not-read"
        with self.assertRaises(getpass.GetPassWarning):
            self.module._password(fallback)

    def test_restart_reenters_each_pending_account_secret_not_just_last_form(self):
        from mail_notifier.onboarding import OnboardingService
        from mail_notifier.onboarding_auth import SetupOwner
        service = OnboardingService(self.host, mailbox_factory=lambda a, p: FakeMailbox(a))
        owner = SetupOwner(str(self.host.home()), "cli")
        draft = service.start(owner)
        for provider, username in (("gmail", "one@example.com"), ("naver", "two@example.com")):
            draft = service.change(draft.id, owner, draft.revision, action="add", values={"provider": provider, "username": username})
            draft = service.validate_password(draft.id, owner, draft.revision, "lost-at-restart")
        self.assertEqual(self.run_setup(["no", "yes"], ("first-fresh", "second-fresh")), 0)
        self.assertEqual(set(self.host.passwords.values()), {"first-fresh", "second-fresh"})

    def test_settings_conflict_can_be_explicitly_restarted_without_overwriting_new_settings(self):
        from mail_notifier.onboarding import OnboardingService
        from mail_notifier.onboarding_auth import SetupOwner
        service = OnboardingService(self.host, mailbox_factory=lambda a, p: FakeMailbox(a))
        owner = SetupOwner(str(self.host.home()), "cli")
        draft = service.start(owner)
        draft = service.change(draft.id, owner, draft.revision, action="settings", values={"target": "telegram:999"})
        self.host.settings = replace(self.host.settings, language="fr")
        self.assertEqual(service.commit(draft.id, owner, draft.revision)["state"], "needs_review")
        self.assertEqual(self.run_setup(["yes", "gmail", "me@example.com", "no", "no", "yes"]), 0)
        self.assertEqual(self.host.load_settings().language, "fr")
        self.assertEqual(self.host.load_settings().target, "telegram")

    def test_paused_setup_can_continue_account_management_without_resuming(self):
        self.host.paused = True
        self.assertEqual(self.run_setup(["gmail", "me@example.com", "no", "no", "yes"]), 1)
        account = self.host.settings.accounts[0]
        self.assertEqual(self.run_setup(["no", "yes", "edit", account.id, "", "", "Renamed", "", "done", "no", "yes"],
                                        ("replacement",)), 1)
        self.assertEqual(self.host.load_settings().accounts[0].label, "Renamed")
        self.assertEqual(self.host.passwords[account.id], "replacement")
        self.assertTrue(self.host.paused)
        self.assertIn("Automatic checks remain paused", self.output.getvalue())
        self.assertNotIn("Setup complete", self.output.getvalue())

    def test_interrupted_scheduled_delivery_can_be_retried_without_resuming_cron(self):
        from mail_notifier.onboarding_auth import SetupOwner
        self.host.paused = True
        with patch.object(self.host, "send", side_effect=SystemExit), self.assertRaises(SystemExit):
            self.run_setup(["gmail", "me@example.com", "no", "no", "yes"])
        owner = SetupOwner(str(self.host.home()), "cli")
        service = self.module.OnboardingService(self.host)
        draft = service.start(owner)
        self.assertEqual(draft.state, "scheduled")
        self.assertEqual(draft.steps["test_delivery"], {"started": True})
        settings, passwords = self.host.load_settings(), dict(self.host.passwords)
        events, probes = list(self.host.events), len(self.probes)
        account = settings.accounts[0]
        store = self.host.open_store()
        self.addCleanup(store.close)
        store.enqueue(account, 7, [101])
        self.prompts.clear()

        # Approve retrying only the recorded delivery step, never unpausing cron.
        self.assertEqual(self.run_setup(["yes"], passwords=()), 1)
        repaired = service.start(owner)
        self.assertEqual((repaired.id, repaired.state), (draft.id, "scheduled"))
        self.assertEqual(repaired.steps["test_delivery"]["message_id"], "1")
        self.assertNotIn("acknowledgement", repaired.steps)
        self.assertEqual(self.host.load_settings(), settings)
        self.assertEqual(self.host.passwords, passwords)
        self.assertEqual(self.host.events, events)
        self.assertEqual(len(self.probes), probes)
        self.assertEqual(store.cursor(account.id), (7, 101))
        self.assertEqual(store.status()["pending"], 1)
        self.assertTrue(self.host.paused)
        self.assertTrue(store.get_meta("cron_paused"))
        self.assertTrue(self.host.deliveries[0][1].endswith(draft.id))
        self.assertIn("Retry the recorded failed step?", self.prompts[0])
        self.assertNotIn("setup_changed_retry", self.output.getvalue())
        self.assertNotIn("Setup complete", self.output.getvalue())

    def test_post_publication_conflict_can_be_reviewed_acknowledged_and_restarted(self):
        from mail_notifier.onboarding import OnboardingService
        from mail_notifier.onboarding_auth import SetupOwner
        self.host.cron_failure = True
        self.assertEqual(self.run_setup(["gmail", "me@example.com", "no", "no", "yes"]), 1)
        self.host.cron_failure = False
        self.host.settings = replace(self.host.settings, language="fr", target="telegram:555")
        service = OnboardingService(self.host)
        owner = SetupOwner(str(self.host.home()), "cli")
        draft = service.start(owner)
        self.assertEqual(self.run_setup(["yes", "done", "no", "no"]), 1)
        self.assertEqual(self.host.load_settings().language, "fr")
        self.assertEqual(self.host.load_settings().target, "telegram:555")
        self.assertIn('"current_settings"', self.output.getvalue())
        self.assertIn("not rolled back", self.output.getvalue())
        setup = self.host.open_setup_store()
        try:
            old = setup.get(draft.id, owner)
            self.assertEqual(old.state, "acknowledged")
            self.assertEqual(old.steps["credentials"], draft.steps["credentials"])
            self.assertIn("settings_fingerprint", old.steps)
        finally:
            setup.close()
