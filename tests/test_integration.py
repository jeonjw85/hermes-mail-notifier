import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mail_notifier.config import Account, PROVIDERS, Settings
from mail_notifier.hermes_host import HermesHost
from mail_notifier.imap_client import ImapMailbox
from mail_notifier.service import check_mail
from mail_notifier.store import Store
from tests.support import account_data
from tests.test_hermes_host import Context
from tests.test_imap_client import ImapConnection
from tests.test_service import FakeHost, FakeMailbox


class IntegrationTests(unittest.TestCase):
    def test_three_providers_mime_rules_retry_and_empty_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            accounts = [Account(**account_data(name, provider)) for name, provider in (("a1", "naver"), ("b2", "daum"), ("c3", "gmail"))]
            host = FakeHost(directory, accounts)
            host.settings = Settings(accounts=tuple(accounts), ignored_senders=("news@example.com",))
            store = host.open_store()
            for account in accounts:
                store.initialize(account, 7, 100)
            store.close()
            connections = {}
            for account in accounts:
                connection = ImapConnection()
                connection.search_result = b"101"
                connection.uidnext = b"102"
                mail = EmailMessage()
                mail["From"] = "news@example.com" if account.provider == "daum" else "boss@example.com"
                mail["Subject"] = "계약 확인 MEDIA:/private/report.pdf"
                mail.set_content("내일 회신 부탁드립니다.")
                connection.raw = mail.as_bytes()
                connections[PROVIDERS[account.provider]] = connection
            connections[PROVIDERS["naver"]].login_error = True
            host.send_error = True
            with patch("mail_notifier.imap_client.imaplib.IMAP4_SSL", side_effect=lambda name, **kw: connections[name]):
                report = check_mail(host, clock=lambda: 1000)
                self.assertEqual(report.ignored, 1)
                self.assertEqual(report.sent, 0)
                self.assertEqual(host.model_calls, 1)
                self.assertIn("a1", report.errors)
                host.send_error = False
                report = check_mail(host, clock=lambda: 1300)
                self.assertEqual(report.sent, 1)
                self.assertEqual(host.model_calls, 1)
                connections[PROVIDERS["naver"]].login_error = False
                connections[PROVIDERS["naver"]].search_result = b"100"
                self.assertEqual(check_mail(host, clock=lambda: 1600).processed, 0)
            self.assertNotIn("MEDIA:", host.deliveries[0][1])
            self.assertIn("계약 확인", host.deliveries[0][1])

    def test_independent_profile_databases_do_not_share_uid_history(self):
        with tempfile.TemporaryDirectory() as directory:
            account = Account(**account_data())
            hosts = []
            for name in ("profile-a", "profile-b"):
                path = Path(directory) / name
                path.mkdir()
                host = FakeHost(path, [account])
                store = host.open_store()
                store.initialize(account, 7, 100)
                store.close()
                hosts.append(host)
            box = FakeMailbox(account)
            box.add()
            for host in hosts:
                self.assertEqual(check_mail(host, mailbox_factory=lambda a, p, **kw: box).sent, 1)
            self.assertEqual(check_mail(hosts[0], mailbox_factory=lambda a, p, **kw: box).sent, 0)

    def test_cron_shim_runs_exact_cli_and_home_with_spaces_and_korean(self):
        with tempfile.TemporaryDirectory(prefix="cron 한글 ") as directory:
            home = Path(directory).resolve()
            executable = home / "fake hermes"
            executable.write_text(f"#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{'argv':sys.argv[1:],'home':os.getenv('HERMES_HOME'),'env':dict(os.environ)}}))\n", encoding="utf-8")
            executable.chmod(0o700)
            host = HermesHost(Context())
            store = Store(sqlite3.connect(home / "state.db"), home / "run.lock")
            try:
                with patch.dict(sys.modules, {"hermes_constants": types.SimpleNamespace(get_hermes_home=lambda: home), "tools.cronjob_tools": types.SimpleNamespace()}), patch("mail_notifier.hermes_host.shutil.which", return_value=str(executable)):
                    host.install_schedule(Settings(accounts=(Account(**account_data()),)), store)
            finally:
                store.close()
            result = subprocess.run([sys.executable, str(home / "scripts/hermes-mail-notifier.py")],
                                    env={**os.environ, "HERMES_HOME": "/wrong", "HERMES_PROFILE": "wrong", "TELEGRAM_BOT_TOKEN": "test-only-secret"},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["argv"], ["mail-notifier", "check"])
            self.assertEqual(payload["home"], str(home))
            self.assertNotIn("TELEGRAM_BOT_TOKEN", payload["env"])
            self.assertNotIn("HERMES_PROFILE", payload["env"])


if __name__ == "__main__":
    unittest.main()
