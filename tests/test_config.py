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

    def test_legacy_settings_load_as_v2_without_losing_accounts(self):
        raw = {"accounts": [account_data()], "target": "telegram:123",
               "important_keywords": ["계약"], "important_senders": ["boss@example.com"]}
        settings = self.config.load_settings(raw)
        self.assertEqual(getattr(settings, "schema_version", None), 2)
        self.assertEqual(settings.accounts[0].username, "a1@example.com")
        self.assertEqual(settings.target, "telegram:123")
        self.assertEqual(settings.important_keywords, ("계약",))
        self.assertEqual(self.config.load_settings(self.config.dump_settings(settings)), settings)

    def test_limits_reject_bools_zero_and_unbounded_values(self):
        limits = {"max_run_seconds": (30, 240), "max_fetches": (1, 100),
                  "max_model_calls": (1, 100), "max_deliveries": (1, 100),
                  "max_pending": (200, 10000), "max_cached_bodies": (1, 1000)}
        for name, (low, high) in limits.items():
            for value in (low, high):
                with self.subTest(name=name, valid=value):
                    settings = self.config.load_settings({name: value})
                    self.assertEqual(getattr(settings, name), value)
            for value in (True, 0, -1, high + 1, "20"):
                with self.subTest(name=name, invalid=value), self.assertRaises(self.config.ConfigError):
                    self.config.load_settings({name: value})
        with self.assertRaises(self.config.ConfigError):
            self.config.load_settings({"max_pending": 20, "max_cached_bodies": 21})

    def test_future_schema_and_invalid_explicit_rules_are_rejected(self):
        for raw in ({"schema_version": 3}, {"schema_version": True},
                    {"always_notify_senders": ["invalid-address"]}):
            with self.subTest(raw=raw), self.assertRaises(self.config.ConfigError):
                self.config.load_settings(raw)
        self.assertEqual(self.config.load_settings({"always_notify_senders": ["boss@example.com"]}).always_notify_senders,
                         ("boss@example.com",))

    def test_fingerprint_tracks_effective_settings_not_mapping_order(self):
        fingerprint = getattr(self.config, "settings_fingerprint", None)
        self.assertTrue(callable(fingerprint), "settings fingerprint is not implemented")
        a = self.config.load_settings({"target": "telegram:123", "language": "ko"})
        b = self.config.load_settings({"language": "ko", "target": "telegram:123"})
        c = self.config.load_settings({"target": "telegram:456", "language": "ko"})
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertNotEqual(fingerprint(a), fingerprint(c))

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
