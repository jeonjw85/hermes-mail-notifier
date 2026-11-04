import tempfile
import unittest
from dataclasses import replace

from mail_notifier.config import Account
from mail_notifier.hermes_host import HostError
from tests.support import account_data, feature
from tests.test_cli import SetupHost
from tests.test_service import FakeMailbox


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.diagnostics")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SetupHost(self.temp.name)
        self.account = Account(**account_data())
        self.host.settings = replace(self.host.settings, accounts=(self.account,), ui_language="ko", language="ko", max_pending=20, max_cached_bodies=20)
        self.host.passwords[self.account.id] = "test-only-secret"
        self.store = self.host.open_store()
        self.addCleanup(self.store.close)
        self.store.initialize(self.account, 7, 100)

    def test_default_diagnosis_uses_no_external_mail_model_or_send(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("external call")
        self.host.send = self.host.complete_structured = forbidden
        result = self.module.diagnose(self.host, mailbox_factory=forbidden)
        self.assertFalse(any(item["name"].startswith("live") for item in result["checks"]))
        self.assertNotIn("test-only-secret", str(result))
        self.assertIn("schema", result["versions"])

    def test_missing_cron_credentials_full_queue_and_hold_are_actionable(self):
        self.store.set_meta("cron_job_id", "missing")
        self.host.passwords.clear()
        self.store.enqueue(self.account, 7, list(range(101, 121)))
        self.store.retry(("a1", "INBOX", 7, 101), "imap_authentication_failed", 1000, permanent=True)
        result = self.module.diagnose(self.host)
        codes = {item["code"] for item in result["checks"]}
        self.assertTrue({"cron_missing", "credential_unavailable", "queue_full", "work_held"} <= codes)
        self.assertFalse(result["success"])
        self.assertTrue(all(item["next_action"] for item in result["checks"] if item["status"] != "ok"))

    def test_live_failures_do_not_hide_other_results_or_fetch_user_mail(self):
        box = FakeMailbox(self.account)
        box.fetch = lambda *a: self.fail("doctor fetched user mail")
        self.host.complete_structured = lambda *a, **kw: (_ for _ in ()).throw(HostError("model_unavailable"))
        result = self.module.diagnose(self.host, live=True, mailbox_factory=lambda *a, **kw: box)
        checks = {row["name"]: row for row in result["checks"]}
        self.assertEqual(checks["live_imap:a1"]["status"], "ok")
        self.assertEqual(checks["live_model"]["code"], "model_unavailable")
        self.assertEqual(checks["live_delivery"]["status"], "ok")
        self.assertEqual(len(self.host.deliveries), 1)
        self.assertIn("진단", self.host.deliveries[0][1])

    def test_scheduled_cron_is_healthy_but_missing_enabled_job_fails(self):
        self.store.set_meta("cron_job_id", "job1")
        self.host.manage_cron = lambda *a, **kw: {"jobs": [{"job_id": "job1", "state": "scheduled", "enabled": True}]}
        result = self.module.diagnose(self.host)
        cron = next(row for row in result["checks"] if row["name"] == "cron")
        self.assertEqual(cron["status"], "ok")
        self.host.manage_cron = lambda *a, **kw: {"jobs": []}
        self.assertFalse(self.module.diagnose(self.host)["success"])

    def test_live_model_probe_bypasses_sender_exclusions(self):
        self.host.settings = replace(self.host.settings, ignored_senders=("@example.invalid",))
        self.host.model_error = True
        result = self.module.diagnose(self.host, live=True, mailbox_factory=lambda *a, **kw: FakeMailbox(self.account))
        self.assertEqual(self.host.model_calls, 1)
        model = next(row for row in result["checks"] if row["name"] == "live_model")
        self.assertEqual(model["status"], "error")

    def test_secure_input_page_is_checked_for_configuration_and_reachability(self):
        self.host.credential_page_url = lambda: ""
        row = next(item for item in self.module.diagnose(self.host)["checks"] if item["name"] == "credential_page")
        self.assertEqual((row["status"], row["code"]), ("warning", "credential_page_unavailable"))
        self.assertIn("credential_page_url", row["next_action"])
        for value, code in (("http://operator.example/", "credential_page_insecure"),
                            ("https://user:pass@operator.example/", "credential_page_insecure")):
            self.host.credential_page_url = lambda value=value: value
            row = next(item for item in self.module.diagnose(self.host)["checks"] if item["name"] == "credential_page")
            self.assertEqual(row["code"], code)
        self.host.credential_page_url = lambda: "https://operator.example/credential/"
        row = next(item for item in self.module.diagnose(self.host)["checks"] if item["name"] == "credential_page")
        self.assertEqual((row["status"], row["code"]), ("ok", "ok"))

    def test_live_secure_input_page_reports_unreachable_https_endpoint(self):
        self.host.credential_page_url = lambda: "https://dead.example/credential/"
        probes = []

        def unreachable(url, timeout):
            probes.append((url, timeout))
            raise OSError("name resolution failed")

        result = self.module.diagnose(self.host, live=True, mailbox_factory=lambda *a, **kw: FakeMailbox(self.account),
                                      page_probe=unreachable)
        row = next(item for item in result["checks"] if item["name"] == "live_credential_page")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["code"], "credential_page_unreachable")
        self.assertEqual(probes[0][0], "https://dead.example/credential/")
        self.assertNotIn("dead.example", row["next_action"])

    def test_live_model_failure_is_reported_with_a_model_specific_next_action(self):
        self.host.complete_structured = lambda *a, **kw: (_ for _ in ()).throw(HostError("model_unavailable"))
        result = self.module.diagnose(self.host, live=True, mailbox_factory=lambda *a, **kw: FakeMailbox(self.account))
        row = next(item for item in result["checks"] if item["name"] == "live_model")
        self.assertEqual(row["code"], "model_unavailable")
        self.assertIn("model", row["next_action"])
        self.assertIn("doctor --live", row["next_action"])
        self.assertFalse(result["success"])

    def test_every_registered_code_has_specific_guidance(self):
        from mail_notifier.onboarding import _ERRORS
        generic = self.module.next_action("__unknown__", language="en")
        for code in sorted(_ERRORS):
            with self.subTest(code=code):
                guidance = self.module.next_action(code, language="en")
                self.assertTrue(guidance)
                self.assertNotEqual(guidance, generic, f"{code} has no specific guidance")

    def test_guidance_groups_are_bilingual_and_keep_commands(self):
        cases = (
            ("invalid_app_password", "password", "hermes mail-notifier setup"),
            ("interactive_terminal_required", "terminal", "hermes mail-notifier setup"),
            ("imap_timeout", "IMAP", "doctor --live"),
            ("storage_unavailable", "storage", "hermes mail-notifier doctor"),
            ("setup_changed_retry", "current", "/mail"),
            ("setup_already_committing", "current", "/mail"),
            ("incompatible_hermes_onboarding", "Hermes", "update"),
            ("mail_not_found", "mail ID", "history"),
        )
        for code, english, command in cases:
            with self.subTest(code=code):
                en = self.module.next_action(code, language="en")
                ko = self.module.next_action(code, language="ko")
                self.assertIn(english.casefold(), en.casefold())
                self.assertIn(command.casefold(), en.casefold())
                self.assertNotEqual(en, ko)

    def test_page_check_matches_the_url_the_secure_button_would_build(self):
        # doctor must never report ok for a page the secure-input button rejects.
        self.host.credential_page_url = lambda: "https://exa mple.com/x"
        row = next(item for item in self.module.diagnose(self.host)["checks"] if item["name"] == "credential_page")
        self.assertEqual(row["status"], "warning")
        self.assertEqual(row["code"], "credential_page_insecure")
        self.host.credential_page_url = lambda: "https://example.com:99999/x"
        row = next(item for item in self.module.diagnose(self.host)["checks"] if item["name"] == "credential_page")
        self.assertEqual(row["code"], "credential_page_insecure")

    def test_next_actions_are_bilingual_without_translating_commands(self):
        cases = (("credential_unavailable", "Check the account", "hermes mail-notifier setup"),
                 ("model_unavailable", "model", "hermes mail-notifier doctor --live --json"),
                 ("invalid_model_output", "model", "hermes mail-notifier doctor --live --json"),
                 ("destination_unavailable", "Save the model", "Hermes"),
                 ("cron_paused", "Repair cron", "hermes cron resume JOB_ID"),
                 ("work_held", "Inspect pending work", "queue retry MAIL_ID"),
                 ("uidvalidity_changed", "Check the mail server", "hermes mail-notifier reset ACCOUNT_ID"),
                 ("already_running", "Wait for", ""),
                 ("no_accounts", "Connect an account", "hermes mail-notifier accounts add"),
                 ("not_run_yet", "Run the first check", "hermes mail-notifier check"),
                 ("unknown_failure", "Check the status", "hermes mail-notifier doctor --json"))
        for code, english, command in cases:
            with self.subTest(code=code):
                en = self.module.next_action(code, language="en")
                ko = self.module.next_action(code, language="ko")
                self.assertIn(english, en)
                self.assertNotEqual(en, ko)
                self.assertIn(command, en)
                self.assertIn(command, ko)
                self.assertEqual(self.module.next_action(code, language="fr"), en)

    def test_next_action_defaults_to_english(self):
        # Callers that omit the language must not get Korean guidance.
        self.assertEqual(self.module.next_action("no_accounts"), self.module.next_action("no_accounts", language="en"))

    def test_guidance_uses_ui_language_but_test_notification_uses_summary_language(self):
        self.host.settings = replace(self.host.settings, ui_language="en", language="ko")
        result = self.module.diagnose(self.host, live=True, mailbox_factory=lambda *a, **kw: FakeMailbox(self.account))
        row = next(row for row in result["checks"] if row["code"] == "not_run_yet")
        self.assertIn("Run the first check", row["next_action"])
        self.assertIn("진단", self.host.deliveries[0][1])
        self.host.settings = replace(self.host.settings, ui_language="ko", language="en")
        captured = []
        def complete(payload):
            captured.append(payload)
            return self.host.classification
        self.host.complete_structured = complete
        result = self.module.diagnose(self.host, live=True, mailbox_factory=lambda *a, **kw: FakeMailbox(self.account))
        row = next(row for row in result["checks"] if row["code"] == "not_run_yet")
        self.assertIn("최초 확인", row["next_action"])
        self.assertEqual(self.host.deliveries[-1][1], "Mail notification diagnostic test · No actual email content is included.")
        self.assertIn("Please reply", captured[0]["input"][0]["text"])
        self.assertIn("in this language: en.", captured[0]["instructions"])

    def test_automatic_or_unavailable_ui_language_falls_back_to_english_guidance(self):
        self.host.settings = replace(self.host.settings, ui_language="")
        result = self.module.diagnose(self.host)
        row = next(row for row in result["checks"] if row["code"] == "not_run_yet")
        self.assertIn("Run the first check", row["next_action"])
        self.host.load_settings = lambda: (_ for _ in ()).throw(HostError("invalid_settings"))
        result = self.module.diagnose(self.host)
        row = next(row for row in result["checks"] if row["name"] == "settings")
        self.assertEqual(row["code"], "invalid_settings")
        self.assertIn("Check the account", row["next_action"])
