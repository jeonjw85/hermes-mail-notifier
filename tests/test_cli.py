import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from mail_notifier.config import Settings, dump_settings, load_settings
from mail_notifier.onboarding_store import SetupStore
from mail_notifier.hermes_host import HostError
from mail_notifier.imap_client import MailboxError
from mail_notifier.message import MailContent
from tests.support import feature
from tests.test_service import FakeHost, FakeMailbox


class SetupHost(FakeHost):
    @property
    def settings(self):
        return self._settings

    @settings.setter
    def settings(self, value):
        self._settings = value
        if (self.path / "settings.json").exists():
            (self.path / "settings.json").write_text(json.dumps(dump_settings(value)))

    def __init__(self, path):
        super().__init__(path, [])
        self.passwords = {}
        self.schedule_calls = 0
        self.cron_failure = False
        self.paused = False
        self.events = []

    def home(self):
        return self.path.resolve()

    def has_saved_settings(self):
        return (self.path / "settings.json").exists() or self.settings != Settings()

    def open_setup_store(self):
        return SetupStore(self.path / "setup.db")

    def load_settings(self):
        path = self.path / "settings.json"
        return load_settings(json.loads(path.read_text())) if path.exists() else self.settings

    def check_compatibility(self):
        pass

    def check_environment(self, settings):
        pass

    def validate_schedule(self, schedule):
        if schedule not in ("every 5m", "every 10m"):
            raise HostError("invalid_schedule")

    def save_settings(self, settings):
        self.settings = settings
        (self.path / "settings.json").write_text(json.dumps(dump_settings(settings)))
        self.events.append("settings")

    def save_password(self, account, password):
        self.passwords[account.id] = password
        self.events.append("password")

    def get_password(self, account):
        return self.passwords[account.id]

    def install_schedule(self, settings, store):
        self.schedule_calls += 1
        if self.cron_failure:
            raise HostError("cron_operation_failed")
        if not any(a.enabled for a in settings.accounts):
            job_id = store.get_meta("cron_job_id") or ""
            self.paused = bool(job_id)
            store.set_meta("cron_paused", self.paused)
            return job_id
        store.set_meta("cron_job_id", "job1")
        store.set_meta("cron_paused", self.paused)
        self.events.append("cron")
        return "job1"

    def resume_schedule(self, job_id, store):
        self.paused = False
        store.set_meta("cron_paused", False)


class CliTests(unittest.TestCase):
    def setUp(self):
        locale = patch.dict("os.environ", {"LC_ALL": "en_US.UTF-8"})
        locale.start()
        self.addCleanup(locale.stop)
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
        self.assertEqual(self.setup_with(["naver", "me@example.com", "no", "no", "yes"]), 0)
        return self.host.settings.accounts[0]

    def test_setup_repeat_preserves_cursor_and_single_schedule_identity(self):
        account = self.first_setup()
        self.highest = 200
        self.assertEqual(self.setup_with(["done", "no", "yes"]), 0)
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
        self.assertEqual(self.setup_with(["edit", account.id, "", "", "업무", "", "done", "no", "yes"], password="rotated-test-password"), 0)
        changed = self.host.settings.accounts[0]
        self.assertEqual((changed.id, changed.label), (account.id, "업무"))
        self.assertEqual(self.host.passwords[account.id], "rotated-test-password")

    def test_login_change_disables_old_and_initializes_new_account(self):
        account = self.first_setup()
        self.highest = 300
        self.assertEqual(self.setup_with(["edit", account.id, "gmail", "new@example.com", "", "", "done", "no", "yes"]), 0)
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
        self.assertNotEqual(self.setup_with(["gmail", "me@example.com", "no"]), 0)
        self.assertEqual(self.host.settings, Settings())
        self.assertEqual(self.host.passwords, {})
        self.assertEqual(self.host.schedule_calls, 0)

    def test_disable_and_enable_preserve_baseline(self):
        account = self.first_setup()
        self.assertEqual(self.setup_with(["disable", account.id, "done", "no", "yes"]), 0)
        self.assertFalse(self.host.settings.accounts[0].enabled)
        self.highest = 200
        self.assertEqual(self.setup_with(["enable", account.id, "done", "no", "yes"]), 1)
        self.assertTrue(self.host.settings.accounts[0].enabled)
        self.assertTrue(self.host.paused)
        self.assertEqual(self.setup_with(["yes"]), 0)
        self.assertFalse(self.host.paused)
        store = self.host.open_store()
        try:
            self.assertEqual(store.cursor(account.id), (7, 100))
        finally:
            store.close()

    def test_cron_failure_reports_partial_setup_and_reexecution_recovers(self):
        self.host.cron_failure = True
        self.assertNotEqual(self.setup_with(["gmail", "me@example.com", "no", "no", "yes"]), 0)
        self.assertEqual(len(self.host.settings.accounts), 1)
        account_id = self.host.settings.accounts[0].id
        self.assertIn("settings", self.output.getvalue())
        self.host.cron_failure = False
        self.assertEqual(self.setup_with(["yes"]), 0)
        self.assertEqual(self.host.settings.accounts[0].id, account_id)

    def test_failed_settings_publish_reports_the_correct_stage(self):
        with patch.object(self.host, "save_settings", side_effect=HostError("settings_save_failed")):
            self.assertNotEqual(self.setup_with(["gmail", "me@example.com", "no", "no", "yes"]), 0)
        self.assertIn("settings_save_failed", self.output.getvalue())
        self.assertEqual(self.host.settings.accounts, ())

    def test_environment_only_credentials_fail_before_settings_or_schedule_changes(self):
        with patch.object(self.host, "check_environment", side_effect=HostError("credentials_not_reloadable")):
            self.assertNotEqual(self.setup_with(["gmail", "me@example.com", "no", "no", "yes"]), 0)
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

    def test_status_explains_stored_decision_and_held_counts_without_content(self):
        account = self.first_setup()
        store = self.host.open_store()
        key = (account.id, "INBOX", 7, 101)
        store.enqueue(account, 7, [101])
        store.cache_message(key, MailContent("boss@example.com", "Boss", "private subject", "", "private body", False, False), 1000)
        store.retry(key, "model_unavailable", 1000, permanent=True)
        store.record_run({"sent": 0}, 1000, 1001)
        store.close()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.module.run_cli(argparse.Namespace(mail_command="status", json=False), self.host), 0)
        text = out.getvalue()
        self.assertIn("Held 1", text)
        self.assertIn("work-personal-v1", text)
        self.assertIn("Last run", text)
        self.assertNotIn("private", text)

    def test_migration_reports_changed_sender_preference_semantics(self):
        self.first_setup()
        self.assertIn("Senders and keywords", self.output.getvalue())
        self.assertIn("Pending notifications keep", self.output.getvalue())

    def test_setup_preserves_operational_limits_and_clears_verified_source_hold(self):
        account = self.first_setup()
        self.host.settings = replace(self.host.settings, max_fetches=3, always_notify_senders=("boss@example.com",))
        store = self.host.open_store()
        store.set_meta(f"source_hold:{account.id}", {"code": "credential_unavailable"})
        store.close()
        self.assertEqual(self.setup_with(["edit", account.id, "", "", "", "", "done", "no", "yes"]), 0)
        self.assertEqual(self.host.settings.max_fetches, 3)
        self.assertEqual(self.host.settings.always_notify_senders, ("boss@example.com",))
        store = self.host.open_store()
        try:
            self.assertIsNone(store.get_meta(f"source_hold:{account.id}"))
        finally:
            store.close()

    def test_operations_cli_json_roundtrip_and_failure_exit(self):
        account = self.first_setup()
        parser = argparse.ArgumentParser()
        self.module.configure_parser(parser)
        for command in (["history", "list", "--account", account.id], ["queue", "list"],
                        ["feedback", "list"], ["policy", "show"],
                        ["policy", "set", "--instructions", "Prioritize personal mail"]):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.run_cli(parser.parse_args([*command, "--json"]), self.host), 0)
            self.assertIsInstance(json.loads(output.getvalue()), dict)
        self.assertEqual(self.host.settings.importance_instructions, "Prioritize personal mail")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.module.run_cli(parser.parse_args(["queue", "retry", "--json"]), self.host), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error"], "queue_scope_required")
        self.assertTrue(result["next_action"])

    def test_setup_cli_language_options_are_forwarded_and_help_is_bilingual(self):
        parser = argparse.ArgumentParser()
        self.module.configure_parser(parser)
        self.assertIn("설정", parser.format_help())
        self.assertIn("setup", parser.format_help().lower())
        for command in (["setup"], ["accounts", "add"], ["accounts", "edit", "a1"]):
            args = parser.parse_args([*command, "--ui-language", "en"])
            with patch.object(self.module, "setup_plugin", return_value=0) as setup:
                self.assertEqual(self.module.run_cli(args, self.host), 0)
                self.assertEqual(setup.call_args.kwargs["ui_language"], "en")

    def test_new_user_must_confirm_and_defaults_come_from_locale(self):
        with patch.dict("os.environ", {"LC_ALL": "en_US.UTF-8", "LC_MESSAGES": "ko_KR", "LANG": "ko_KR"}):
            self.assertEqual(self.setup_with(["gmail", "me@example.com", "no", "no", "no"]), 1)
            self.assertEqual(self.host.load_settings().accounts, ())
            self.assertEqual(self.host.passwords, {})
            self.assertEqual(self.setup_with(["gmail", "me@example.com", "no", "no", "yes"]), 0)
        account = self.host.load_settings().accounts[0]
        self.assertEqual((account.label, account.mailbox), ("me@example.com", "INBOX"))
        self.assertEqual(self.host.load_settings().language, "en")
        self.assertIn("app password", self.output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
