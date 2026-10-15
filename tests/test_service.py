import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mail_notifier.config import Account, Settings
from mail_notifier.hermes_host import DeliveryError, ModelUnavailable
from mail_notifier.imap_client import Discovery, MailboxError
from mail_notifier.message import MailContent, parse_message
from mail_notifier.store import Store
from tests.support import account_data, classification_data, deeply_nested_message, feature


class FakeMailbox:
    def __init__(self, account):
        self.account = account
        self.epoch = 7
        self.highest = 100
        self.messages = {}
        self.error = False
        self.fetches = 0
        self.timeouts = []

    def set_timeout(self, seconds):
        self.timeouts.append(seconds)

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

    def discover(self, after_uid, limit, upper_uid):
        end = min(after_uid + 1000, upper_uid)
        found = sorted(uid for uid in self.messages if after_uid < uid <= end)
        selected = found[:limit]
        return Discovery(selected, selected[-1] if len(found) > limit else max(end, after_uid))

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
        self.model_timeouts = []
        self.send_timeouts = []
        self.home_target = "telegram:111"
        self.classification = classification_data()

    def load_settings(self):
        return self.settings

    def open_store(self):
        return Store(sqlite3.connect(self.path / "state.db"), self.path / "run.lock",
                     migration_target=self.resolve_target(self.settings.target))

    def resolve_target(self, target):
        return self.home_target if target == "telegram" else target

    def get_password(self, account):
        return "test-only-password"

    def complete_structured(self, payload, *, timeout=45):
        self.model_timeouts.append(timeout)
        self.model_calls += 1
        if self.model_error:
            raise ModelUnavailable()
        return self.classification

    def send(self, target, text, *, timeout=30):
        self.send_timeouts.append(timeout)
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
        return self.module.check_mail(self.host, mailbox_factory=lambda a, p, **kw: self.boxes[a.id], clock=lambda: self.now, **kwargs)

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

    def test_new_notification_uses_summary_language_independently_of_ui(self):
        self.host.settings = replace(self.host.settings, language="en", ui_language="ko")
        self.boxes["a1"].add()
        self.assertEqual(self.run_check().sent, 1)
        text = self.host.deliveries[0][1]
        self.assertIn("Priority: Normal", text)
        self.assertIn("Summary: 계약 회신 요청", text)
        self.assertIn("Mail ID:", text)

    def test_ready_notification_and_destination_survive_both_language_changes(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        store = self.host.open_store()
        try:
            ready = store.due("a1", self.now + 300, 1, status="ready")[0]
            saved, identifier = ready.notification, ready.mail_id
        finally:
            store.close()
        self.host.settings = replace(self.host.settings, language="en", ui_language="en", target="telegram:999")
        self.host.send_error = False
        self.now += 300
        self.assertEqual(self.run_check().sent, 1)
        self.assertEqual(self.host.deliveries, [("telegram:111", saved)])
        self.assertIn("우선순위: 보통", saved)
        self.assertIn(identifier, saved)
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

    def test_new_epoch_blocks_discovery_but_not_cached_work(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        self.host.send_error = False
        self.now = 1300
        self.boxes["a1"].epoch = 8
        self.boxes["b2"].add()
        report = self.run_check()
        self.assertEqual(report.errors["a1"], "uidvalidity_changed")
        self.assertEqual(report.sent, 2)
        self.assertEqual(self.status()["ready"], 0)
        result = self.module.reset_account(self.host, "a1", mailbox_factory=lambda a, p: self.boxes[a.id], clock=lambda: self.now)
        self.assertEqual(result["superseded"], 0)
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
        def complete(payload, **kwargs):
            self.host.model_calls += 1
            subject = json.loads(payload["input"][0]["text"])["subject"]
            return {"important": "false"} if subject == "invalid-output" else self.host.classification
        self.host.complete_structured = complete
        self.assertEqual(self.run_check().sent, 0)
        self.now += 300
        self.assertEqual(self.run_check().sent, 1)
        self.assertIn("later-valid-mail", self.host.deliveries[0][1])
        self.assertEqual(self.status()["pending"], 20)

    def test_ready_delivers_with_imap_down(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        self.host.send_error = False
        self.boxes["a1"].error = True
        self.now += 300
        self.assertEqual(self.run_check().sent, 1)
        self.assertEqual(self.host.model_calls, 1)

    def test_connection_failures_have_durable_account_backoff(self):
        self.host.settings = replace(self.host.settings, accounts=(self.accounts[0],))
        calls = []
        def fail(account, password, **kwargs):
            calls.append(account.id)
            raise MailboxError("imap_connection_failed")
        for now in (1000, 1001, 1002):
            self.module.check_mail(self.host, mailbox_factory=fail, clock=lambda: now)
        self.assertEqual(calls, ["a1"])
        self.module.check_mail(self.host, mailbox_factory=fail, clock=lambda: 1300)
        self.assertEqual(calls, ["a1", "a1"])
        self.module.check_mail(self.host, mailbox_factory=fail, clock=lambda: 1600)
        self.assertEqual(calls, ["a1", "a1"])

    def test_authentication_failure_holds_source_until_explicit_recovery(self):
        self.host.settings = replace(self.host.settings, accounts=(self.accounts[0],))
        calls = []
        def fail(account, password, **kwargs):
            calls.append(account.id)
            raise MailboxError("imap_authentication_failed")
        for now in (1000, 1001, 100000):
            self.module.check_mail(self.host, mailbox_factory=fail, clock=lambda: now)
        self.assertEqual(calls, ["a1"])
        store = self.host.open_store()
        store.resume_source("a1", 100001)
        store.close()
        self.module.check_mail(self.host, mailbox_factory=fail, clock=lambda: 100001)
        self.assertEqual(calls, ["a1", "a1"])

    def test_queued_home_alias_is_resolved_before_retry(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        self.host.home_target = "telegram:222"
        self.host.send_error = False
        self.now += 300
        self.assertEqual(self.run_check().sent, 1)
        self.assertEqual(self.host.deliveries[0][0], "telegram:111")

    def test_cached_mail_classifies_with_imap_down(self):
        self.boxes["a1"].add()
        self.host.model_error = True
        self.run_check()
        self.host.model_error = False
        self.boxes["a1"].error = True
        self.now += 300
        self.assertEqual(self.run_check().sent, 1)
        self.assertEqual(self.boxes["a1"].fetches, 1)

    def test_model_outage_does_not_block_delivery(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        self.host.send_error = False
        self.host.model_error = True
        self.boxes["b2"].add()
        self.now += 300
        report = self.run_check()
        self.assertEqual(report.sent, 1)
        self.assertEqual(self.status()["fetched"], 1)

    def test_telegram_outage_preserves_classification(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        self.run_check()
        store = self.host.open_store()
        try:
            self.assertEqual(store.history()[0]["decision"]["category"], "work")
        finally:
            store.close()

    def test_invalid_output_fifth_attempt_sends_review(self):
        self.boxes["a1"].add()
        self.host.classification = {"invalid": True}
        for attempt in range(5):
            report = self.run_check()
            self.assertEqual(report.sent, 1 if attempt == 4 else 0)
            self.now += 3600
        self.assertEqual(self.boxes["a1"].fetches, 1)
        self.assertIn("원문 확인", self.host.deliveries[0][1])

    def test_five_invalid_outputs_produce_english_review_reason_and_empty_subject_fallback(self):
        self.host.settings = replace(self.host.settings, language="en", ui_language="ko")
        self.boxes["a1"].add(subject="")
        self.host.classification = {"invalid": True}
        for attempt in range(5):
            self.assertEqual(self.run_check().sent, 1 if attempt == 4 else 0)
            self.now += 3600
        self.assertEqual(self.host.model_calls, 5)
        self.assertEqual(self.boxes["a1"].fetches, 1)
        text = self.host.deliveries[0][1]
        self.assertIn("Reason: Model output failed validation 5 times. Check the original email.", text)
        self.assertIn("Summary: Check the email body", text)
        self.assertIn("[Review email ·", text)

    def test_delivery_tenth_failure_holds(self):
        self.boxes["a1"].add()
        self.host.send_error = True
        for attempt in range(10):
            self.run_check()
            self.now += 3600
        self.assertEqual(self.status()["held"], 1)
        self.assertEqual(self.host.model_calls, 1)
        self.host.send_error = False
        self.assertEqual(self.run_check().sent, 0)

    def test_time_budget_caps_every_external_call(self):
        self.boxes["a1"].add()
        self.host.settings = replace(self.host.settings, max_run_seconds=30)
        ticks = iter([0])
        self.run_check(monotonic=lambda: next(ticks, 28))
        self.assertTrue(self.boxes["a1"].timeouts)
        self.assertTrue(self.host.model_timeouts)
        self.assertTrue(self.host.send_timeouts)
        self.assertTrue(all(0 < value <= 2 for value in [*self.boxes["a1"].timeouts,
                                                        *self.host.model_timeouts, *self.host.send_timeouts]))

    def test_duration_includes_connection_cleanup(self):
        from contextlib import contextmanager
        elapsed = [0]
        @contextmanager
        def factory(account, password, **kwargs):
            yield self.boxes[account.id]
            elapsed[0] += 1
        report = self.module.check_mail(self.host, mailbox_factory=factory,
                                       monotonic=lambda: elapsed[0], clock=lambda: self.now)
        self.assertEqual(report.duration_seconds, 2)
        self.assertEqual(self.status()["last_run"]["report"]["duration_seconds"], 2)

    def test_full_queue_applies_backpressure_without_uid_loss(self):
        self.host.settings = replace(self.host.settings, max_pending=20, max_cached_bodies=20)
        self.host.model_error = True
        for uid in range(101, 126):
            self.boxes["a1"].add(uid)
        self.run_check()
        self.now += 300
        self.run_check()
        self.assertEqual(self.status()["pending"], 20)
        self.host.model_error = False
        for _ in range(3):
            self.now += 3600
            self.run_check()
        self.assertEqual(len(self.host.deliveries), 25)

    def test_persistent_stage_rotation_prevents_starvation(self):
        self.host.settings = replace(self.host.settings, max_fetches=1, max_model_calls=1, max_deliveries=1)
        for box in self.boxes.values():
            for uid in range(101, 105):
                box.add(uid, subject=box.account.id)
        for _ in range(10):
            self.run_check()
        self.assertEqual(len(self.host.deliveries), 8)
        self.assertTrue(any("제목: b2" in text for _, text in self.host.deliveries))

    def test_elapsed_delivery_budget_does_not_starve_other_stages(self):
        self.host.settings = replace(self.host.settings, max_run_seconds=30)
        store = self.host.open_store()
        for account in self.accounts:
            box = self.boxes[account.id]
            for uid in range(101, 111):
                box.add(uid)
                store.enqueue(account, 7, [uid])
                store.cache_message((account.id, "INBOX", 7, uid), box.messages[uid], self.now)
                store.save_decision((account.id, "INBOX", 7, uid), classification_data(),
                                    notification="ready", target="telegram", fingerprint="test", now=self.now)
            box.add(111)
            store.enqueue(account, 7, [111])
            store.cache_message((account.id, "INBOX", 7, 111), box.messages[111], self.now)
            box.add(112)
        store.close()
        elapsed = [0]
        send = self.host.send
        def slow_send(*args, **kwargs):
            elapsed[0] += 30
            return send(*args, **kwargs)
        self.host.send = slow_send
        for _ in range(8):
            elapsed[0] = 0
            self.run_check(monotonic=lambda: elapsed[0])
        self.assertGreater(self.host.model_calls, 0)
        self.assertGreater(sum(box.fetches for box in self.boxes.values()), 0)

    def test_disabled_account_never_sends_cached_work(self):
        self.boxes["a1"].add()
        self.host.model_error = True
        self.run_check()
        self.host.settings = replace(self.host.settings, accounts=(replace(self.accounts[0], enabled=False),))
        self.host.model_error = False
        self.now += 300
        self.assertEqual(self.run_check().sent, 0)
        self.assertEqual(self.status()["fetched"], 1)

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

    def test_reset_supersedes_all_new_unfinished_states(self):
        account = self.accounts[0]
        store = self.host.open_store()
        store.enqueue(account, 7, [101, 102, 103, 104])
        mail = MailContent("boss@example.com", "Boss", "계약", "", "회신 필요", False, False)
        for uid in (102, 103, 104):
            store.cache_message((account.id, "INBOX", 7, uid), mail, self.now)
        store.save_decision((account.id, "INBOX", 7, 103), classification_data(), notification="summary",
                            target="telegram", fingerprint="test", now=self.now)
        store.retry((account.id, "INBOX", 7, 104), "model_unavailable", self.now, permanent=True)
        store.close()
        result = self.module.reset_account(self.host, account.id, mailbox_factory=lambda a, p: self.boxes[a.id])
        self.assertEqual(result["superseded"], 4)
        self.assertEqual(result.get("superseded_by_stage"), {"discovered": 1, "fetched": 1, "ready": 1, "held": 1})
        self.assertEqual(self.status()["pending"], 0)

    def test_legacy_ready_notification_delivers_after_upgrade(self):
        from tests.test_migrations import create_v1
        with tempfile.TemporaryDirectory() as path:
            db = sqlite3.connect(Path(path) / "state.db")
            create_v1(db)
            db.close()
            host = FakeHost(path, self.accounts[:1])
            self.boxes["a1"].error = True
            report = self.module.check_mail(host, mailbox_factory=lambda a, p, **kw: self.boxes[a.id], clock=lambda: 2000)
            self.assertEqual(report.sent, 1)
            self.assertEqual(host.deliveries, [("telegram:111", "saved summary")])


if __name__ == "__main__":
    unittest.main()
