"""Every raised code must survive as recovery evidence.

A missing entry in OnboardingService's durable allowlist rewrites a
user-fixable cause into "unexpected_error", which is exactly the failure this
module exists to prevent. Keep the scan regex-based: it must notice a new
raise site without executing it.
"""

import pathlib
import re
import unittest

from tests.support import feature

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "mail_notifier"
RAISE = re.compile(
    r"(?:HostError|MailboxError|TriageError|StorageError|DeliveryError|ModelUnavailable|ConfigError)\(\s*\"([a-z][a-z0-9_]*)\""
)
CODE = re.compile(r"^\s*code = \"([a-z][a-z0-9_]*)\"", re.MULTILINE)


class ErrorCodeTests(unittest.TestCase):
    def setUp(self):
        self.onboarding = feature(self, "mail_notifier.onboarding")

    def raised_codes(self):
        codes = set()
        for path in SOURCE.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            codes |= set(RAISE.findall(text))
            codes |= set(CODE.findall(text))
        return codes

    def test_every_raised_code_is_durable_recovery_evidence(self):
        raised = self.raised_codes()
        self.assertTrue(raised, "the scan must not silently match nothing")
        missing = raised - self.onboarding._ERRORS
        self.assertEqual(missing, set(),
                         "register these codes in OnboardingService's _ERRORS set, "
                         "otherwise they are stored as unexpected_error")

    def test_unknown_codes_are_still_never_persisted_raw(self):
        self.assertNotIn("unexpected_error", self.onboarding._ERRORS)
        for value in ("private_secret_value", "", "UPPER", "with space", "x" * 200, None):
            self.assertNotIn(value, self.onboarding._ERRORS)

    def test_registered_codes_are_all_lowercase_machine_tokens(self):
        for code in self.onboarding._ERRORS:
            with self.subTest(code=code):
                self.assertRegex(code, r"^[a-z][a-z0-9_]{0,79}$")
