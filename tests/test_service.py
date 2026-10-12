import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mail_notifier.config import Account, Settings
from mail_notifier.hermes_host import DeliveryError, ModelUnavailable
from mail_notifier.imap_client import MailboxError
from mail_notifier.message import MailContent, parse_message
from mail_notifier.store import Store
from tests.support import account_data, deeply_nested_message, feature


class FakeMailbox:
    def __init__(self, account):
        self.account = account
        self.epoch = 7
        self.highest = 100
        self.messages = {}
        self.error = False
        self.fetches = 0

    def __enter__(self):
        if self.error:
            raise MailboxError("imap_connection_failed")
        return self

    def __exit__(self, *args):
        pass

    def snapshot(self):
        return self.epoch, max([self.highest, *self.messages])

    def new_uids(self, after_uid, limit):
        return sorted(uid for uid in self.messages if uid > after_uid)[:limit]

    def fetch(self, uid):
        self.fetches += 1
        return self.messages.get(uid)

    def add(self, uid=101, subject="계약", sender="boss@example.com"):
        self.messages[uid] = MailContent(sender, "Boss", subject, "Fri", "내일 회신해 주세요.", False, False)


class FakeHost:
    def __init__(self, path, accounts):
        self.path = Path(path)
        self.settings = Settings(accounts=tuple(accounts))
        self.model_calls = 0
        self.deliveries = []
        self.model_error = False
        self.send_error = False
        self.classification = {"important": True, "reason": "회신 필요", "summary": "계약 회신 요청", "action": "회신", "deadline": ""}

    def load_settings(self):
        return self.settings

    def open_store(self):
        return Store(sqlite3.connect(self.path / "state.db"), self.path / "run.lock")

    def get_password(self, account):
        return "test-only-password"

    def complete_structured(self, payload):
        self.model_calls += 1
        if self.model_error:
            raise ModelUnavailable()
        return self.classification

    def send(self, target, text):
        if self.send_error:
            raise DeliveryError()
        self.deliveries.append((target, text))
        return {"success": True, "message_id": str(len(self.deliveries))}

    def manage_cron(self, *args, **kwargs):
        return {"success": True, "jobs": []}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.service")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.accounts = [Account(**account_data("a1")), Account(**account_data("b2", "daum"))]
        self.host = FakeHost(self.temp.name, self.accounts)
        self.boxes = {a.id: FakeMailbox(a) for a in self.accounts}
        self.now = 1000
        store = self.host.open_store()
        for account in self.accounts:
            store.initialize(account, 7, 100)
        store.close()

    def run_check(self, **kwargs):
        return self.module.check_mail(self.host, mailbox_factory=lambda a, p: self.boxes[a.id], clock=lambda: self.now, **kwargs)

    def status(self):
        store = self.host.open_store()
        try:
            return store.status()
        finally:
            store.close()

    def test_empty_tick_costs_zero_model_and_send_calls(self):
        report = self.run_check()
        self.assertEqual(report.processed, 0)
        self.assertEqual(self.host.model_calls, 0)
        self.assertEqual(self.host.deliveries, [])

    def test_repeated_scan_sends_once_across_reopened_store(self):
        self.boxes["a1"].add()
        self.assertEqual(self.run_check().sent, 1)
        self.assertEqual(self.run_check().sent, 0)
        self.assertEqual(len(self.host.deliveries), 1)
        self.assertEqual(self.host.model_calls, 1)
        self.assertEqual(self.status()["pending"], 0)

    def test_recursive_mime_message_does_not_block_following_mail(self):
        box = self.boxes["a1"]
        box.add(101)
        box.add(102, subject="Normal following mail")
        fetch = box.fetch
        box.fetch = lambda uid: parse_message(deeply_nested_message()) if uid == 101 else fetch(uid)
        report = self.run_check()
        self.assertEqual(report.errors, {})
        self.assertEqual(report.sent, 2)
        self.assertEqual(self.status()["pending"], 0)
        self.assertTrue(any("본문 확인 필요" in text for _, text in self.host.deliveries))

    def test_retry_reuses_saved_summary_without_fetch_or_model(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.assertIn("a1", self.run_check().errors)
        self.assertEqual(self.status()["ready"], 1)
        self.host.send_error = False
        self.now = 1299
        self.assertEqual(self.run_check().sent, 0)
        self.now = 1300
        self.assertEqual(self.run_check().sent, 1)
        self.assertEqual(self.host.model_calls, 1)
        self.assertEqual(self.boxes["a1"].fetches, 1)

    def test_delivery_error_remains_visible_during_backoff_until_success(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        self.assertEqual(self.status()["accounts"][0]["last_error"], "delivery_failed")
        self.now = 1001
        self.run_check()
        self.assertEqual(self.status()["accounts"][0]["last_error"], "delivery_failed")
        self.host.send_error = False
        self.now = 1300
        self.run_check()
        self.assertIsNone(self.status()["accounts"][0]["last_error"])

    def test_failed_account_does_not_block_others_or_move_its_cursor(self):
        self.boxes["a1"].error = True
        self.boxes["b2"].add()
        report = self.run_check()
        self.assertEqual(report.sent, 1)
        self.assertEqual(report.errors["a1"], "imap_connection_failed")
        self.assertEqual(self.status()["accounts"][0]["last_uid"], 100)

    def test_epoch_change_holds_ready_payload_until_explicit_reset(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        self.host.send_error = False
        self.now = 1300
        self.boxes["a1"].epoch = 8
        self.boxes["b2"].add()
        report = self.run_check()
        self.assertEqual(report.errors["a1"], "uidvalidity_changed")
        self.assertEqual(report.sent, 1)
        self.assertEqual(self.status()["ready"], 1)
        result = self.module.reset_account(self.host, "a1", mailbox_factory=lambda a, p: self.boxes[a.id], clock=lambda: self.now)
        self.assertEqual(result["superseded"], 1)
        self.assertEqual(result["uidvalidity"], 8)

    def test_global_model_failure_stops_further_model_calls(self):
        for box in self.boxes.values():
            box.add()
        self.host.model_error = True
        report = self.run_check()
        self.assertEqual(self.host.model_calls, 1)
        self.assertTrue(report.errors)
        self.assertEqual(self.status()["pending"], 2)

    def test_invalid_model_output_remains_pending_and_other_mail_continues(self):
        self.boxes["a1"].add()
        self.host.classification = {"important": "false"}
        report = self.run_check()
        self.assertEqual(report.errors["a1"], "invalid_model_output")
        self.assertEqual(self.status()["pending"], 1)
        self.assertEqual(self.host.deliveries, [])

    def test_failed_classifications_share_capacity_with_later_uids(self):
        for uid in range(101, 121):
            self.boxes["a1"].add(uid, subject="invalid-output")
        self.boxes["a1"].add(121, subject="later-valid-mail")
        def complete(payload):
            self.host.model_calls += 1
            subject = json.loads(payload["input"][0]["text"])["subject"]
            return {"important": "false"} if subject == "invalid-output" else self.host.classification
        self.host.complete_structured = complete
        self.assertEqual(self.run_check().sent, 0)
        self.now += 300
        self.assertEqual(self.run_check().sent, 1)
        self.assertIn("later-valid-mail", self.host.deliveries[0][1])
        self.assertEqual(self.status()["pending"], 20)

    def test_fairness_and_twenty_item_budget(self):
        for uid in range(101, 131):
            self.boxes["a1"].add(uid)
        self.boxes["b2"].add(subject="다음 계정")
        report = self.run_check()
        self.assertEqual(report.processed, 20)
        self.assertTrue(any("다음 계정" in text for _, text in self.host.deliveries))

    def test_rotation_serves_more_than_twenty_accounts(self):
        accounts = [Account(**account_data(f"a{i}")) for i in range(21)]
        self.host.settings = Settings(accounts=tuple(accounts))
        store = self.host.open_store()
        for account in accounts:
            store.initialize(account, 7, 100)
            box = self.boxes[account.id] = FakeMailbox(account)
            for uid in range(101, 125):
                box.add(uid, subject=account.id)
        store.close()
        self.run_check()
        self.run_check()
        self.assertTrue(any("제목: a20\n" in text for _, text in self.host.deliveries))

    def test_time_limit_stops_starting_more_work(self):
        self.boxes["a1"].add()
        self.boxes["b2"].add()
        ticks = iter([0, 241])
        report = self.run_check(monotonic=lambda: next(ticks, 241))
        self.assertEqual(report.processed, 0)
        self.assertEqual(self.host.model_calls, 0)

    def test_disabled_account_keeps_work_and_reenable_resumes(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        self.host.settings = Settings(accounts=(replace(self.accounts[0], enabled=False),))
        self.host.send_error = False
        self.now = 1300
        self.assertEqual(self.run_check().sent, 0)
        self.assertEqual(self.status()["ready"], 1)
        self.host.settings = Settings(accounts=(self.accounts[0],))
        self.assertEqual(self.run_check().sent, 1)

    def test_first_contact_is_baseline_and_expunged_pending_finishes(self):
        new = Account(**account_data("new"))
        self.host.settings = Settings(accounts=(new,))
        self.boxes["new"] = FakeMailbox(new)
        self.boxes["new"].add()
        self.assertEqual(self.run_check().processed, 0)
        store = self.host.open_store()
        store.enqueue(new, 7, [102])
        store.close()
        report = self.run_check()
        self.assertEqual(report.processed, 1)
        self.assertEqual(self.status()["pending"], 0)
        self.assertEqual(self.host.model_calls, 0)

    def test_concurrent_run_is_reported_and_status_does_not_contact_mail(self):
        store = self.host.open_store()
        try:
            with store.run_lock():
                self.assertTrue(self.run_check().already_running)
        finally:
            store.close()
        self.boxes["a1"].error = True
        self.module.get_status(self.host)
        self.assertEqual(self.host.model_calls, 0)


if __name__ == "__main__":
    unittest.main()
