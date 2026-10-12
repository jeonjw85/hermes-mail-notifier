import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from mail_notifier.config import Settings
from mail_notifier.hermes_host import HostError
from mail_notifier.imap_client import MailboxError
from tests.support import feature
from tests.test_service import FakeHost, FakeMailbox


class SetupHost(FakeHost):
    def __init__(self, path):
        super().__init__(path, [])
        self.passwords = {}
        self.schedule_calls = 0
        self.cron_failure = False

    def check_compatibility(self):
        pass

    def check_environment(self, settings):
        pass

    def save_settings(self, settings):
        self.settings = settings

    def save_password(self, account, password):
        self.passwords[account.id] = password

    def get_password(self, account):
        return self.passwords[account.id]

    def install_schedule(self, settings, store):
        self.schedule_calls += 1
        if self.cron_failure:
            raise HostError("cron_operation_failed")
        store.set_meta("cron_job_id", "job1")
        return "job1"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.cli")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SetupHost(self.temp.name)
        self.output = io.StringIO()
        self.probe_error = False
        self.highest = 100

    def mailbox(self, account, password):
        if self.probe_error:
            raise MailboxError("imap_authentication_failed")
        box = FakeMailbox(account)
        box.highest = self.highest
        return box

    def setup_with(self, answers, password="test-only-password"):
        answers = iter(answers)
        def read(prompt):
            try:
                return next(answers)
            except StopIteration:
                return ""
        with patch.object(self.module, "ImapMailbox", self.mailbox), redirect_stdout(self.output):
            return self.module.setup_plugin(self.host, read=read, read_secret=lambda prompt: password)

    def first_setup(self):
        self.assertEqual(self.setup_with(["add", "naver", "개인", "me@example.com", "INBOX", "done"]), 0)
        return self.host.settings.accounts[0]

    def test_setup_repeat_preserves_cursor_and_single_schedule_identity(self):
        account = self.first_setup()
        self.highest = 200
        self.assertEqual(self.setup_with(["done"]), 0)
        self.assertEqual(self.host.settings.accounts[0].id, account.id)
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(account.id), (7, 100))
            self.assertEqual(store.get_meta("cron_job_id"), "job1")
        finally:
            store.close()
        self.assertNotIn("test-only-password", self.output.getvalue())

    def test_rename_and_password_rotation_preserve_account_id(self):
        account = self.first_setup()
        self.assertEqual(self.setup_with(["edit", account.id, "", "업무", "", "", "done"], password="rotated-test-password"), 0)
        changed = self.host.settings.accounts[0]
        self.assertEqual((changed.id, changed.label), (account.id, "업무"))
        self.assertEqual(self.host.passwords[account.id], "rotated-test-password")

    def test_login_change_disables_old_and_initializes_new_account(self):
        account = self.first_setup()
        self.highest = 300
        self.assertEqual(self.setup_with(["edit", account.id, "gmail", "", "new@example.com", "", "done"]), 0)
        old, new = self.host.settings.accounts
        self.assertFalse(old.enabled)
        self.assertNotEqual(new.id, account.id)
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(new.id), (7, 300))
            self.assertEqual(store.cursor(account.id), (7, 100))
        finally:
            store.close()

    def test_cancel_preserves_existing_settings_passwords_and_cron(self):
        account = self.first_setup()
        settings = self.host.settings
        calls = self.host.schedule_calls
        with redirect_stdout(self.output):
            result = self.module.setup_plugin(self.host, read=lambda _: (_ for _ in ()).throw(EOFError()))
        self.assertNotEqual(result, 0)
        self.assertEqual(self.host.settings, settings)
        self.assertEqual(self.host.schedule_calls, calls)
        self.assertEqual(self.host.passwords[account.id], "test-only-password")

    def test_failed_probe_is_not_saved_or_enabled(self):
        self.probe_error = True
        self.assertNotEqual(self.setup_with(["add", "gmail", "개인", "me@example.com", "INBOX"]), 0)
        self.assertEqual(self.host.settings, Settings())
        self.assertEqual(self.host.passwords, {})
        self.assertEqual(self.host.schedule_calls, 0)

    def test_disable_and_enable_preserve_baseline(self):
        account = self.first_setup()
        self.assertEqual(self.setup_with(["disable", account.id, "done"]), 0)
        self.assertFalse(self.host.settings.accounts[0].enabled)
        self.highest = 200
        self.assertEqual(self.setup_with(["enable", account.id, "done"]), 0)
        self.assertTrue(self.host.settings.accounts[0].enabled)
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(account.id), (7, 100))
        finally:
            store.close()

    def test_cron_failure_reports_partial_setup_and_reexecution_recovers(self):
        self.host.cron_failure = True
        self.assertNotEqual(self.setup_with(["add", "gmail", "개인", "me@example.com", "INBOX", "done"]), 0)
        self.assertEqual(len(self.host.settings.accounts), 1)
        account_id = self.host.settings.accounts[0].id
        self.assertIn("설정 저장", self.output.getvalue())
        self.host.cron_failure = False
        self.assertEqual(self.setup_with(["done"]), 0)
        self.assertEqual(self.host.settings.accounts[0].id, account_id)

    def test_failed_settings_publish_reports_the_correct_stage(self):
        with patch.object(self.host, "save_settings", side_effect=HostError("settings_save_failed")):
            self.assertNotEqual(self.setup_with(["add", "gmail", "개인", "me@example.com", "INBOX", "done"]), 0)
        self.assertIn("단계: 설정 저장 중", self.output.getvalue())
        self.assertEqual(self.host.settings.accounts, ())

    def test_environment_only_credentials_fail_before_settings_or_schedule_changes(self):
        with patch.object(self.host, "check_environment", side_effect=HostError("credentials_not_reloadable")):
            self.assertNotEqual(self.setup_with(["add", "gmail", "개인", "me@example.com", "INBOX", "done"]), 0)
        self.assertEqual(self.host.settings.accounts, ())
        self.assertEqual(self.host.passwords, {})
        self.assertEqual(self.host.schedule_calls, 0)
        self.assertIn("credentials_not_reloadable", self.output.getvalue())

    def test_parser_and_cli_exit_codes_include_json_status_and_selected_reset(self):
        account = self.first_setup()
        parser = argparse.ArgumentParser()
        self.module.configure_parser(parser)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.module.run_cli(parser.parse_args(["status", "--json"]), self.host), 0)
        self.assertIn(account.id, out.getvalue())
        self.highest = 500
        with patch.object(self.module, "reset_account") as reset, redirect_stdout(self.output):
            reset.return_value = {"account_id": account.id, "superseded": 1, "uidvalidity": 7, "last_uid": 500}
            self.assertEqual(self.module.run_cli(parser.parse_args(["reset", account.id]), self.host), 0)
            reset.assert_called_once_with(self.host, account.id)
        with patch.object(self.module, "check_mail") as check, redirect_stdout(self.output):
            from mail_notifier.service import RunReport
            check.return_value = RunReport(errors={"a1": "delivery_failed"})
            self.assertEqual(self.module.run_cli(parser.parse_args(["check"]), self.host), 1)


if __name__ == "__main__":
    unittest.main()
