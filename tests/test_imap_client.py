import imaplib
import ssl
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from mail_notifier.config import Account
from tests.support import account_data, feature


class ImapConnection:
    def __init__(self):
        self.calls = []
        self.search_result = b"100 101 102"
        self.epoch = b"7"
        self.uidnext = b"103"
        self.raw = b"From: sender@example.com\nSubject: New\n\nBody"
        self.fetch_result = None
        self.raise_search = False
        self.login_error = False
        self.logged_out = False

    def login(self, user, password):
        if self.login_error:
            raise imaplib.IMAP4.error("test-only-password rejected")
        return "OK", [b"Logged in"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"3"]

    def response(self, name):
        return name, [{"UIDVALIDITY": self.epoch, "UIDNEXT": self.uidnext}[name]]

    def uid(self, command, *args):
        self.calls.append((command.upper(), *args))
        if command.upper() == "SEARCH":
            if self.raise_search:
                raise OSError("connection closed")
            return "OK", [self.search_result]
        if self.fetch_result is not None:
            return self.fetch_result
        return "OK", [(f"1 (UID {args[0]} RFC822.SIZE {len(self.raw)} BODY[] {{{len(self.raw)}}}".encode(), self.raw), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]


class ImapTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.imap_client")
        self.connection = ImapConnection()
        self.patcher = patch.object(self.module.imaplib, "IMAP4_SSL", return_value=self.connection)
        self.factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.account = Account(**account_data())

    def test_peek_readonly_tls_and_provider(self):
        with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
            content = mailbox.fetch(101)
        self.assertEqual(content.subject, "New")
        self.assertEqual(self.connection.calls[0], ("select", "INBOX", True))
        fetch_call = next(c for c in self.connection.calls if c[0] == "FETCH")
        self.assertIn("BODY.PEEK[]<0.262144>", fetch_call[2])
        self.assertEqual(self.factory.call_args.args[0], "imap.naver.com")
        context = self.factory.call_args.kwargs["ssl_context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(self.connection.logged_out)

    def test_custom_mailboxes_have_valid_quoted_modified_utf7_wire_names(self):
        cases = [
            ("[Gmail]/All Mail", b'A0 EXAMINE "[Gmail]/All Mail"\r\n'),
            ("업무", b'A0 EXAMINE "&xcW7NA-"\r\n'),
            ('A & B "quoted"', b'A0 EXAMINE "A &- B \\"quoted\\""\r\n'),
        ]
        for name, expected in cases:
            with self.subTest(mailbox=name):
                wire = imaplib.IMAP4.__new__(imaplib.IMAP4)
                wire.state, wire._encoding, wire.debug = "AUTH", "ascii", 0
                wire.untagged_responses, wire.tagged_commands = {}, {}
                wire.tagpre, wire.tagnum, wire.literal = b"A", 0, None
                wire.send, wire._log = Mock(), Mock()
                def select(argument, readonly=False):
                    self.assertTrue(readonly)
                    wire._command("EXAMINE", argument)
                    return "OK", [b"0"]
                with patch.object(self.connection, "select", side_effect=select):
                    with self.module.ImapMailbox(replace(self.account, mailbox=name), "test-only-password"):
                        pass
                wire.send.assert_called_once_with(expected)

    def test_snapshot_and_new_uids_include_already_read_mail(self):
        with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
            self.assertEqual(mailbox.snapshot(), (7, 102))
            self.assertEqual(mailbox.new_uids(100, 1), [101])
        search = next(c for c in self.connection.calls if c[0] == "SEARCH")
        self.assertNotIn("UNSEEN", str(search))

    def test_uid_star_does_not_replay_and_empty_mailbox(self):
        self.connection.search_result = b"100"
        with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
            self.assertEqual(mailbox.new_uids(100, 20), [])
        self.connection.uidnext = b"1"
        self.connection.search_result = b""
        with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
            self.assertEqual(mailbox.snapshot(), (7, 0))
            self.assertEqual(mailbox.new_uids(0, 20), [])

    def test_expunge_differs_from_failure(self):
        self.connection.fetch_result = ("OK", [None])
        with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
            self.assertIsNone(mailbox.fetch(101))
            self.connection.fetch_result = ("NO", [b"backend failed"])
            with self.assertRaises(self.module.MailboxError):
                mailbox.fetch(101)

    def test_connection_error_and_bad_epoch_are_safe(self):
        self.connection.raise_search = True
        with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
            with self.assertRaises(self.module.MailboxError):
                mailbox.new_uids(100, 20)
        self.connection.epoch = b"invalid"
        with self.assertRaises(self.module.MailboxError):
            with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
                mailbox.snapshot()

    def test_failed_login_closes_connection_without_leaking_password(self):
        self.connection.login_error = True
        with self.assertRaises(self.module.MailboxError) as caught:
            with self.module.ImapMailbox(self.account, "test-only-password"):
                pass
        self.assertNotIn("test-only-password", str(caught.exception))
        self.assertTrue(self.connection.logged_out)

    def test_partial_body_is_flagged_and_unexpected_uid_rejected(self):
        self.connection.fetch_result = ("OK", [(b"1 (UID 101 RFC822.SIZE 999999 BODY[] {10}", self.connection.raw), b")"])
        with self.module.ImapMailbox(self.account, "test-only-password") as mailbox:
            self.assertTrue(mailbox.fetch(101).truncated)
            with self.assertRaises(self.module.MailboxError):
                mailbox.fetch(102)


if __name__ == "__main__":
    unittest.main()
