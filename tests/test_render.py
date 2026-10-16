import unittest
from dataclasses import replace

from mail_notifier.config import Account
from mail_notifier.message import MailContent
from tests.support import account_data, feature


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.render")
        triage = feature(self, "mail_notifier.triage")
        self.account = Account(**account_data())
        self.mail = MailContent("boss@example.com", "Boss", "계약 확인", "Fri", "본문", False, False)
        self.result = triage.TriageResult("work", "notify", "normal", "대응 필요", "계약 확인 요청", "회신", "")

    def test_render_includes_reason_priority_and_stable_id(self):
        text = self.module.render_notification(self.account, self.mail, self.result, mail_id="abc123")
        for value in ("대응 필요", "보통", "abc123"):
            self.assertIn(value, text)

    def test_notification_has_all_user_facing_fields(self):
        text = self.module.render_notification(self.account, self.mail, self.result)
        for expected in ["개인 메일", "boss@example.com", "계약 확인", "요약", "할 일: 회신", "기한: 명시 없음"]:
            self.assertIn(expected, text)

    def test_truncated_or_empty_body_is_explicit(self):
        text = self.module.render_notification(self.account, replace(self.mail, needs_body_review=True), self.result)
        self.assertIn("본문 확인 필요", text)

    def test_english_notification_labels_fallbacks_and_review_warning(self):
        mail = replace(self.mail, sender_name="", sender_address="", subject="", needs_body_review=True)
        result = replace(self.result, decision="review", summary="", action="", deadline="")
        text = self.module.render_notification(self.account, mail, result, mail_id="abc123", language="en")
        for expected in ("[Review email ·", "Priority: Normal", "Category: Work", "From: Unknown",
                         "Subject: (No subject)", "Reason: 대응 필요", "Summary: Check the email body",
                         "Action: Not specified", "Deadline: Not specified", "Mail ID: abc123",
                         "Only part of the email was available."):
            self.assertIn(expected, text)
        self.assertNotIn("명시 없음", text)

    def test_arbitrary_summary_language_uses_english_labels_without_translating_content(self):
        result = replace(self.result, summary="Résumé français", reason="À lire", action="Répondre")
        text = self.module.render_notification(self.account, self.mail, result, language="fr")
        for expected in ("[Important email ·", "Summary: Résumé français", "Reason: À lire", "Action: Répondre"):
            self.assertIn(expected, text)

    def test_all_priorities_and_categories_are_bilingual(self):
        for language, priorities, categories in (
            ("en", ("High", "Normal", "Low"), ("Work", "Personal", "Transactional", "Promotion", "Newsletter", "Unknown")),
            ("ko", ("높음", "보통", "낮음"), ("업무", "개인", "거래·서비스", "홍보", "뉴스레터", "판단 불충분")),
        ):
            for priority, label in zip(("high", "normal", "low"), priorities):
                text = self.module.render_notification(self.account, self.mail, replace(self.result, priority=priority), language=language)
                self.assertIn(("Priority: " if language == "en" else "우선순위: ") + label, text)
            for category, label in zip(("work", "personal", "transactional", "promotion", "newsletter", "unknown"), categories):
                text = self.module.render_notification(self.account, self.mail, replace(self.result, category=category), language=language)
                self.assertIn(("Category: " if language == "en" else "분류: ") + label, text)

    def test_unicode_limit_preserves_deadline_and_emoji(self):
        mail = replace(self.mail, subject="🦄" * 3000, sender_name="🦄" * 3000)
        result = replace(self.result, summary="🦄" * 2000, action="🦄" * 1000, deadline="2026-12-31")
        text = self.module.render_notification(self.account, mail, result)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
        self.assertIn("기한: 2026-12-31", text)
        self.assertNotIn("\ufffd", text)

    def test_all_external_fields_neutralize_media_and_local_paths(self):
        hostile = "MEDIA:/etc/secret.pdf [[audio_as_voice]] ![x](file:///tmp/a.png) C:\\secret.txt ~/data.pdf\x00\u202e"
        account = replace(self.account, label="MEDIA:/tmp/private.pdf")
        mail = replace(self.mail, subject=hostile, sender_name=hostile)
        result = replace(self.result, summary=hostile, action=hostile, deadline=hostile)
        text = self.module.render_notification(account, mail, result)
        for token in ["MEDIA:", "[[", "/etc/", "file:///", "C:\\", "~/", "\x00", "\u202e"]:
            self.assertNotIn(token, text)

    def test_both_languages_bound_worst_case_utf16_and_neutralize_every_external_field(self):
        hostile = "MEDIA:/etc/secret.pdf [[audio_as_voice]] ![x](file:///tmp/a.png) C:\\secret.txt ~/data.pdf\x00\u202e"
        long_field = hostile + "🦄" * 4000
        account = replace(self.account, label=long_field.replace("\x00", ""))
        mail = replace(self.mail, sender_name=long_field, sender_address=long_field, subject=long_field, truncated=True)
        result = replace(self.result, reason=long_field, summary=long_field, action=long_field, deadline=long_field, decision="review")
        for language in ("ko", "en", "fr"):
            with self.subTest(language=language):
                text = self.module.render_notification(account, mail, result, mail_id=long_field, language=language)
                self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
                for token in ("MEDIA:", "[[", "/etc/", "file:///", "C:\\", "~/", "\x00", "\u202e", "\ufffd"):
                    self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
