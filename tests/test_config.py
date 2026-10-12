import unittest

from tests.support import account_data, feature


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = feature(self, "mail_notifier.config")

    def test_defaults_allow_initial_setup(self):
        settings = self.config.load_settings({})
        self.assertEqual(settings.accounts, ())
        self.assertEqual(settings.schedule, "every 5m")
        self.assertEqual(settings.target, "telegram")
        self.assertEqual(settings.language, "ko")

    def test_multiple_accounts_round_trip_without_credentials(self):
        raw = {
            "accounts": [
                account_data(),
                account_data("b2", "daum", enabled=False),
                account_data("c3", "gmail"),
            ],
            "target": "telegram:-100123:456",
            "important_senders": ["boss@example.com", "@company.example"],
            "ignored_senders": ["news@example.com"],
            "important_keywords": ["계약"],
            "importance_instructions": "마감일이 있는 메일을 알려줘",
        }
        settings = self.config.load_settings(raw)
        saved = self.config.dump_settings(settings)
        self.assertEqual([a.provider for a in settings.accounts], ["naver", "daum", "gmail"])
        self.assertFalse(settings.accounts[1].enabled)
        self.assertEqual(saved["accounts"], raw["accounts"])
        self.assertEqual(saved["importance_instructions"], raw["importance_instructions"])
        self.assertEqual(self.config.load_settings(saved), settings)
        self.assertNotIn("password", saved["accounts"][0])

    def test_duplicate_ids_and_secret_refs_rejected(self):
        for accounts in [
            [account_data(), account_data(provider="gmail")],
            [account_data(), account_data("b2", password_secret="HERMES_MAIL_A1_APP_PASSWORD")],
        ]:
            with self.subTest(accounts=accounts), self.assertRaises(self.config.ConfigError):
                self.config.load_settings({"accounts": accounts})

    def test_bad_account_types_and_unknown_provider_rejected(self):
        for change in [
            {"enabled": "false"}, {"id": "../other"}, {"id": "A1"},
            {"provider": "unknown"}, {"username": ""}, {"username": "a\r\nLOGIN"},
            {"mailbox": ""}, {"password_secret": "TELEGRAM_BOT_TOKEN"},
            {"password": "test-only-password"},
        ]:
            with self.subTest(field=list(change)), self.assertRaises(self.config.ConfigError):
                self.config.load_settings({"accounts": [account_data(**change)]})

    def test_bad_settings_types_and_targets_rejected(self):
        for raw in [
            None, [], {"accounts": "mail"}, {"accounts": [None]},
            {"target": "discord:123"}, {"target": "telegram:123;echo bad"},
            {"target": "telegram:0"}, {"target": "telegram:123:0"},
            {"schedule": ""}, {"schedule": True}, {"language": ""},
            {"important_senders": "boss@example.com"},
            {"important_senders": ["not-an-address"]},
            {"important_keywords": [False]},
            {"importance_instructions": []},
        ]:
            with self.subTest(raw=raw), self.assertRaises(self.config.ConfigError):
                self.config.load_settings(raw)

    def test_secret_reference_cannot_escape_own_namespace(self):
        self.assertEqual(self.config.secret_name("a1"), "HERMES_MAIL_A1_APP_PASSWORD")
        for invalid in ["", "a-b", "A", "a" * 65, "../x"]:
            with self.subTest(invalid=invalid), self.assertRaises(self.config.ConfigError):
                self.config.secret_name(invalid)


if __name__ == "__main__":
    unittest.main()
