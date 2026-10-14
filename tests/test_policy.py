import unittest

from tests.support import feature


class PolicyTests(unittest.TestCase):
    def test_feedback_selects_only_recent_matching_sender_with_bounded_text(self):
        policy = feature(self, "mail_notifier.policy")
        examples = [dict(sender_address="BOSS@example.com", created_at=i, subject=f"mail-{i}",
                         decision="suppress", note="n" * 400, private="not input") for i in range(8)]
        examples.append(dict(sender_address="other@example.com", created_at=100, subject="other"))
        result = policy.select_feedback("boss@example.com", examples)
        self.assertEqual([item["subject"] for item in result], ["mail-3", "mail-4", "mail-5", "mail-6", "mail-7"])
        self.assertTrue(all(len(item["note"]) <= 300 for item in result))
        self.assertTrue(all("private" not in item for item in result))
