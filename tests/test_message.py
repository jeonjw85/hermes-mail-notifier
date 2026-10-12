import base64
import unittest
from email.message import EmailMessage
from unittest.mock import Mock

from tests.support import deeply_nested_message, feature


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.message")

    def test_plain_unicode_and_actual_sender(self):
        mail = EmailMessage()
        mail["From"] = '"Trusted boss" <actual@example.com>'
        mail["Subject"] = "계약 확인"
        mail.set_content("금요일까지 확인해 주세요.")
        result = self.module.parse_message(mail.as_bytes())
        self.assertEqual(result.sender_address, "actual@example.com")
        self.assertEqual(result.subject, "계약 확인")
        self.assertIn("금요일까지", result.text)
        self.assertFalse(result.needs_body_review)

    def test_encoded_display_address_cannot_override_actual_sender_rules(self):
        from mail_notifier.config import Settings
        from mail_notifier.triage import triage_message
        raw = (b"From: =?utf-8?b?Ym9zc0B0cnVzdGVkLmV4YW1wbGU=?= <attacker@evil.example>\r\n"
               b"Subject: Routine\r\n\r\nhello")
        mail = self.module.parse_message(raw)
        self.assertEqual(mail.sender_address, "attacker@evil.example")
        self.assertEqual(mail.sender_name, "boss@trusted.example")
        complete = Mock(return_value={"important": False, "reason": "", "summary": "", "action": "", "deadline": ""})
        self.assertFalse(triage_message(mail, Settings(important_senders=("@trusted.example",)), complete).important)
        complete.reset_mock()
        self.assertFalse(triage_message(mail, Settings(ignored_senders=("@evil.example",)), complete).important)
        complete.assert_not_called()

    def test_cp949_header_and_body(self):
        subject = base64.b64encode("계약 확인".encode("cp949"))
        raw = (b"Subject: =?cp949?B?" + subject + b"?=\r\n"
               b"Content-Type: text/plain; charset=cp949\r\n\r\n" + "내일 마감".encode("cp949"))
        result = self.module.parse_message(raw)
        self.assertEqual(result.subject, "계약 확인")
        self.assertEqual(result.text, "내일 마감")

    def test_html_removes_invisible_content(self):
        raw = (b"Content-Type: text/html; charset=utf-8\n\n"
               b"<style>hidden style</style><script>hidden script</script>"
               b"<p>Pay &amp; confirm</p><p>Friday</p>")
        result = self.module.parse_message(raw)
        self.assertIn("Pay & confirm", result.text)
        self.assertNotIn("hidden", result.text)
        self.assertIn("Friday", result.text)

    def test_alternative_and_attachments_not_duplicated(self):
        mail = EmailMessage()
        mail.set_content("body once")
        mail.add_alternative("<p>body twice</p>", subtype="html")
        mail.add_attachment(b"attachment secret", maintype="text", subtype="plain", filename="notes.txt")
        result = self.module.parse_message(mail.as_bytes())
        self.assertIn("body once", result.text)
        self.assertNotIn("body twice", result.text)
        self.assertNotIn("attachment secret", result.text)

    def test_attached_message_and_attachment_only_need_review(self):
        raw = b'Content-Type: text/plain\nContent-Disposition: attachment; filename="a.txt"\n\nprivate attachment'
        result = self.module.parse_message(raw)
        self.assertEqual(result.text, "")
        self.assertTrue(result.needs_body_review)
        outer = EmailMessage()
        inner = EmailMessage()
        inner.set_content("attached email")
        outer.set_content("real body")
        outer.add_attachment(inner)
        self.assertNotIn("attached email", self.module.parse_message(outer.as_bytes()).text)

    def test_broken_charset_and_truncated_mime_are_bounded(self):
        raw = b"Content-Type: text/plain; charset=unknown-codec\n\n" + "한글".encode("cp949")
        self.assertEqual(self.module.parse_message(raw).text, "한글")
        raw = b"Content-Type: text/plain\n\n" + b"x" * 14000
        result = self.module.parse_message(raw, truncated=True)
        self.assertEqual(len(result.text), 12000)
        self.assertTrue(result.truncated)
        self.assertTrue(result.needs_body_review)
        broken = b'Content-Type: multipart/mixed; boundary="part"\n\n--part\nContent-Type: text/plain\n\npartial'
        result = self.module.parse_message(broken, truncated=True)
        self.assertIn("partial", result.text)
        self.assertTrue(result.needs_body_review)

    def test_broken_base64_and_missing_body_do_not_crash(self):
        raw = b"Content-Type: text/plain\nContent-Transfer-Encoding: base64\n\nSGVsbG8"
        self.assertIn("Hello", self.module.parse_message(raw).text)
        self.assertTrue(self.module.parse_message(b"Subject: Hello\n\n").needs_body_review)

    def test_recursive_mime_parser_failure_recovers_outer_headers(self):
        mail = self.module.parse_message(deeply_nested_message())
        self.assertEqual(mail.subject, "Review this mail")
        self.assertEqual(mail.sender_address, "actual@example.com")
        self.assertEqual(mail.text, "")
        self.assertTrue(mail.needs_body_review)


if __name__ == "__main__":
    unittest.main()
