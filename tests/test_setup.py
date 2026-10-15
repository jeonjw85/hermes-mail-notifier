import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from mail_notifier.imap_client import MailboxError
from tests.support import feature
from tests.test_cli import SetupHost
from tests.test_service import FakeMailbox


class SetupRecoveryTests(unittest.TestCase):
    def setUp(self):
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
        self.assertEqual(self.run_setup(["typo", "add", "gmial", "gmail", "Work", "bad\x01name", "me@example.com", "INBOX", "done"]), 0)
        self.assertEqual(self.host.settings.accounts[0].username, "me@example.com")
        self.assertEqual(self.host.settings.accounts[0].label, "Work")
        self.assertEqual(len(self.probes), 1)

    def test_password_retry_preserves_other_account_and_never_prints_passwords(self):
        answers = ["add", "gmail", "One", "one@example.com", "INBOX",
                   "add", "naver", "Two", "two@example.com", "INBOX", "password", "done"]
        self.assertEqual(self.run_setup(answers, ("first-secret", "wrong", "second-secret")), 0)
        self.assertEqual([a.label for a in self.host.settings.accounts], ["One", "Two"])
        self.assertEqual(set(self.host.passwords.values()), {"first-secret", "second-secret"})
        self.assertNotIn("second-secret", self.output.getvalue())
        self.assertEqual(len(self.probes), 3)

    def test_cancel_during_retry_has_no_published_side_effects(self):
        self.assertEqual(self.run_setup(["add", "gmail", "One", "one@example.com", "INBOX", "cancel"], ("wrong",)), 1)
        self.assertEqual(self.host.settings.accounts, ())
        self.assertEqual(self.host.passwords, {})
        self.assertEqual(self.host.schedule_calls, 0)

    def test_bad_schedule_is_reprompted_before_settings_are_saved(self):
        self.assertEqual(self.run_setup(["add", "gmail", "One", "one@example.com", "INBOX", "done",
                                        "", "evry five minutes", "every 10m"]), 0)
        self.assertEqual(self.host.settings.schedule, "every 10m")
        self.assertEqual(sum(prompt.startswith("확인 주기") for prompt in self.prompts), 2)

    def test_account_add_and_edit_skip_global_settings_and_preserve_them(self):
        self.host.settings = replace(self.host.settings, target="telegram:123", importance_instructions="work", max_fetches=3)
        self.assertEqual(self.run_setup(["gmail", "One", "one@example.com", "INBOX"], account_action="add"), 0)
        account = self.host.settings.accounts[0]
        self.assertEqual(self.run_setup(["", "Renamed", "", ""], account_action="edit", account_id=account.id), 0)
        self.assertEqual(self.host.settings.accounts[0].id, account.id)
        self.assertEqual(self.host.settings.target, "telegram:123")
        self.assertEqual(self.host.settings.max_fetches, 3)
        self.assertFalse(any("Telegram 목적지" in prompt for prompt in self.prompts))

    def test_setup_confirmation_uses_summary_language_and_preserves_ui_selection(self):
        self.host.settings = replace(self.host.settings, language="en", ui_language="ko")
        self.assertEqual(self.run_setup(["gmail", "One", "one@example.com", "INBOX"], account_action="add"), 0)
        self.assertEqual(self.host.settings.language, "en")
        self.assertEqual(self.host.settings.ui_language, "ko")
        self.assertEqual(self.host.deliveries[0][1],
                         "Mail notifications are configured. Important new emails arriving after connection will be summarized.")

    def test_accounts_cli_lists_disables_and_enables_without_reset(self):
        self.assertEqual(self.run_setup(["gmail", "One", "one@example.com", "INBOX"], account_action="add"), 0)
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
