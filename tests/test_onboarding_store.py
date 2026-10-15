import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path

from mail_notifier.config import Account, Settings
from mail_notifier.hermes_host import HostError
from mail_notifier.onboarding_auth import SetupOwner
from tests.support import account_data, feature


class SetupStoreTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.onboarding_store")
        self.temp = tempfile.TemporaryDirectory(prefix="mail setup ")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "plugin-data" / "setup.db"
        self.owner = SetupOwner(str(Path(self.temp.name) / "profile"), "telegram", "10", "20", "20", "30")
        self.store = self.open_store()
        self.other = self.open_store()

    def open_store(self):
        store = self.module.SetupStore(self.path)
        self.addCleanup(store.close)
        return store

    def create(self, **kwargs):
        return self.store.create(self.owner, Settings(), original_saved=False, now=1000, **kwargs)

    def test_reopen_preserves_incomplete_form_settings_and_frozen_draft(self):
        settings = Settings(accounts=(Account(**account_data()),), language="fr", ui_language="en")
        draft = self.store.create(self.owner, settings, original_saved=True, now=1000)
        form = {"action": "add", "account_id": "a2", "provider": "", "username": "",
                "label": "Work", "mailbox": "INBOX", "input_field": "provider", "prompt_message_id": "41"}
        changed = self.store.cas(replace(draft, form=form, candidate=replace(settings, schedule="every 10m")),
                                 expected_revision=0, now=1001)
        reopened = self.open_store().get(draft.id, self.owner)
        self.assertEqual(reopened, changed)
        self.assertEqual(reopened.original, settings)
        self.assertTrue(reopened.original_saved)
        self.assertEqual(reopened.form, form)
        self.assertEqual(reopened.state, "draft")
        self.assertEqual(reopened.updated_at, 1001)
        with self.assertRaises(FrozenInstanceError):
            reopened.state = "cancelled"

    def test_two_connections_reject_stale_revision(self):
        draft = self.create()
        changed = self.store.cas(replace(draft, state="awaiting_secret"), expected_revision=0, now=1001)
        self.assertEqual(changed.revision, 1)
        with self.assertRaisesRegex(HostError, "setup_changed_retry"):
            self.other.cas(replace(draft, state="cancelled"), expected_revision=0, now=1002)
        self.assertEqual(self.other.get(draft.id, self.owner), changed)

    def test_simultaneous_connections_only_one_cas_wins(self):
        draft = self.create()
        barrier = threading.Barrier(2)
        results = []

        def update(state):
            store = self.module.SetupStore(self.path)
            try:
                barrier.wait(timeout=5)
                results.append(store.cas(replace(draft, state=state), expected_revision=0, now=1001).revision)
            except HostError as exc:
                results.append(exc.code)
            finally:
                store.close()

        threads = [threading.Thread(target=update, args=(state,)) for state in ("awaiting_secret", "cancelled")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, [1, "setup_changed_retry"])

    def test_each_owner_field_is_required_for_get_and_cas(self):
        draft = self.create()
        changes = {"profile_home": str(Path(self.temp.name) / "other"), "surface": "cli", "bot_id": "11",
                   "user_id": "21", "chat_id": "21", "thread_id": "31"}
        for field, value in changes.items():
            owner = replace(self.owner, **{field: value})
            with self.subTest(field=field):
                with self.assertRaisesRegex(HostError, "setup_not_found"):
                    self.other.get(draft.id, owner)
                with self.assertRaisesRegex(HostError, "setup_not_found"):
                    self.other.cas(replace(draft, owner=owner), expected_revision=0, now=1001)
        self.assertEqual(self.store.get(draft.id, self.owner), draft)

    def test_create_resumes_one_active_draft_without_overwriting_input(self):
        draft = self.create()
        changed = self.store.cas(replace(draft, form={"username": "unfinished"}), expected_revision=0, now=1001)
        resumed = self.other.create(self.owner, Settings(target="telegram:99"), original_saved=True, now=1002)
        self.assertEqual(resumed, changed)

    def test_same_chat_different_thread_cannot_create_second_active_draft(self):
        self.create()
        with self.assertRaisesRegex(HostError, "setup_changed_retry"):
            self.other.create(replace(self.owner, thread_id="31"), Settings(), original_saved=False, now=1001)

    def test_profile_cap_is_50_and_completed_drafts_do_not_count(self):
        first = self.create()
        for index in range(1, 50):
            owner = replace(self.owner, user_id=str(100 + index), chat_id=str(100 + index))
            self.other.create(owner, Settings(), original_saved=False, now=1000)
        overflow = replace(self.owner, user_id="999", chat_id="999")
        with self.assertRaisesRegex(HostError, "setup_limit_reached"):
            self.other.create(overflow, Settings(), original_saved=False, now=1001)
        self.assertEqual(self.other.create(self.owner, Settings(), original_saved=False, now=1001), first)
        separate_profile = replace(overflow, profile_home=str(Path(self.temp.name) / "other"))
        self.other.create(separate_profile, Settings(), original_saved=False, now=1001)
        self.store.cas(replace(first, state="completed"), expected_revision=0, now=1001)
        self.other.create(overflow, Settings(), original_saved=False, now=1001)

    def test_expiry_boundary_is_24_hours_since_last_update(self):
        draft = self.create()
        draft = self.store.cas(replace(draft, form={"username": "incomplete"}), expected_revision=0, now=1001)
        self.other.prune(now=87400.999)
        self.assertEqual(self.store.get(draft.id, self.owner), draft)
        self.other.prune(now=87401)
        expired = self.store.get(draft.id, self.owner)
        self.assertEqual(expired.state, "expired")
        self.assertEqual(expired.revision, 2)
        self.assertEqual(expired.form, {})
        with self.assertRaisesRegex(HostError, "setup_changed_retry"):
            self.store.cas(replace(draft, state="completed"), expected_revision=1, now=87401)
        new = self.create_at(87401)
        self.assertNotEqual(new.id, draft.id)

    def create_at(self, now):
        return self.store.create(self.owner, Settings(), original_saved=False, now=now)

    def test_create_and_cas_prune_expired_input(self):
        draft = self.create()
        with self.assertRaisesRegex(HostError, "setup_changed_retry"):
            self.other.cas(replace(draft, state="completed"), expected_revision=0, now=87400)
        self.assertEqual(self.store.get(draft.id, self.owner).state, "expired")
        self.assertNotEqual(self.create_at(87400).id, draft.id)

    def test_terminal_records_cannot_be_changed_and_are_pruned_at_24_hours(self):
        for index, state in enumerate(("completed", "cancelled", "acknowledged")):
            owner = replace(self.owner, bot_id=str(100 + index))
            draft = self.store.create(owner, Settings(), original_saved=False, now=1000)
            terminal = self.store.cas(replace(draft, state=state), expected_revision=0, now=1001)
            with self.assertRaisesRegex(HostError, "setup_changed_retry"):
                self.other.cas(replace(terminal, state="draft"), expected_revision=1, now=1002)
            self.other.prune(now=87400.999)
            self.assertEqual(self.store.get(draft.id, owner).state, state)
            self.other.prune(now=87401)
            with self.assertRaisesRegex(HostError, "setup_not_found"):
                self.store.get(draft.id, owner)

    def test_late_prune_does_not_extend_expired_record_retention(self):
        draft = self.create()
        self.other.prune(now=173800)
        with self.assertRaisesRegex(HostError, "setup_not_found"):
            self.store.get(draft.id, self.owner)

    def test_original_snapshot_and_saved_flag_cannot_be_rewritten(self):
        draft = self.create()
        for changes in ({"original": Settings(language="en")}, {"original_saved": True}, {"revision": 1}):
            with self.subTest(changes=changes), self.assertRaisesRegex(HostError, "setup_changed_retry"):
                self.other.cas(replace(draft, **changes), expected_revision=0, now=1001)
        self.assertEqual(self.store.get(draft.id, self.owner), draft)

    def test_recovery_steps_round_trip_only_typed_non_secret_fields(self):
        draft = self.create()
        steps = {"credentials": {"a1": {"write_started": True, "verified": True}},
                 "baselines": {"a1": {"uidvalidity": 7, "last_uid": 100}},
                 "settings_fingerprint": "a" * 64, "cron": {"job_id": "job-1", "paused": True},
                 "test_delivery": {"started": True, "message_id": "42"}, "error_code": "cron_operation_failed"}
        changed = self.store.cas(replace(draft, steps=steps), expected_revision=0, now=1001)
        self.assertEqual(self.other.get(draft.id, self.owner).steps, steps)
        self.assertEqual(json.loads(json.dumps(changed.to_dict())), json.loads(json.dumps(asdict(changed))))

    def test_secrets_and_untyped_nested_payloads_are_rejected_before_storage(self):
        draft = self.create()
        secret = "DO-NOT-PERSIST-test-only"
        for name in ("password", "private_key", "ciphertext", "unexpected"):
            cases = [{"form": {name: secret}}, {"steps": {name: secret}},
                     {"steps": {"credentials": {"a1": {name: secret}}}},
                     {"steps": {"baselines": {"a1": {name: secret}}}},
                     {"steps": {"cron": {name: secret}}}, {"steps": {"test_delivery": {name: secret}}}]
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaisesRegex(HostError, "invalid_setup_draft"):
                    self.store.cas(replace(draft, **changes), expected_revision=0, now=1001)
        for changes in ({"form": {"username": {"password": secret}}},
                        {"form": {"input_field": "password"}}, {"steps": {"credentials": {"a1": secret}}},
                        {"steps": {"credentials": {"a1": {"verified": secret}}}},
                        {"steps": {"baselines": {"a1": {"uidvalidity": secret}}}},
                        {"steps": {"cron": {"job_id": {"password": secret}}}},
                        {"steps": {"test_delivery": {"started": secret}}},
                        {"steps": {"settings_fingerprint": secret}}, {"steps": {"error_code": secret}},
                        {"state": secret}):
            with self.subTest(changes=changes), self.assertRaisesRegex(HostError, "invalid_setup_draft"):
                self.store.cas(replace(draft, **changes), expected_revision=0, now=1001)
        self.assertNotIn(secret.encode(), self.path.read_bytes())
        self.assertEqual(self.store.get(draft.id, self.owner), draft)

    def test_mutating_nested_data_cannot_bypass_write_or_serialization_validation(self):
        draft = self.create()
        draft.steps["password"] = "test-only-secret"
        self.assertNotIn("test-only-secret", repr(draft))
        with self.assertRaisesRegex(HostError, "invalid_setup_draft"):
            draft.to_dict()
        with self.assertRaisesRegex(HostError, "invalid_setup_draft"):
            self.store.cas(draft, expected_revision=0, now=1001)
        self.assertEqual(self.other.get(draft.id, self.owner).steps, {})

    def test_unknown_settings_fields_cannot_enter_persisted_snapshot(self):
        draft = self.create()
        for field in ("password", "private_key", "ciphertext"):
            with self.subTest(field=field), self.assertRaises(TypeError):
                replace(draft, **{field: "test-only-secret"})
        self.assertNotIn("test-only-secret", repr(draft))

    def test_private_permissions_are_applied_to_new_and_existing_files(self):
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.parent.chmod(0o755)
        self.path.chmod(0o644)
        self.open_store()
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_unknown_schema_is_rejected_without_changing_it(self):
        path = self.path.parent / "future.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA user_version=99")
            db.execute("CREATE TABLE future(value TEXT)")
            db.execute("INSERT INTO future VALUES('preserve')")
        with self.assertRaisesRegex(HostError, "unsupported_setup_schema"):
            self.module.SetupStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(db.execute("SELECT * FROM future").fetchall(), [("preserve",)])

    def test_unversioned_existing_database_is_not_adopted(self):
        path = self.path.parent / "unrelated.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
        with self.assertRaisesRegex(HostError, "unsupported_setup_schema"):
            self.module.SetupStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("unrelated",)])

    def test_v1_upgrade_preserves_evidence_and_acknowledged_attempt_releases_owner(self):
        draft = self.create()
        steps = {"credentials": {"a1": {"write_started": True, "verified": False}}}
        draft = self.store.cas(replace(draft, state="needs_review", steps=steps), expected_revision=0, now=1001)
        # Reproduce the exact v1 partial index, including on already-upgraded test DBs.
        self.store.db.execute("DROP INDEX one_active_owner")
        self.store.db.execute("""CREATE UNIQUE INDEX one_active_owner ON drafts(profile_home,surface,bot_id,user_id,chat_id)
            WHERE state NOT IN ('completed','cancelled','expired')""")
        self.store.db.execute("PRAGMA user_version=1")
        upgraded = self.open_store()
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(upgraded.get(draft.id, self.owner), draft)
        steps = {**steps, "acknowledgement": {"reason": "conflict", "settings_fingerprint": "a" * 64}}
        closed = upgraded.cas(replace(draft, state="acknowledged", steps=steps), expected_revision=1, now=1002)
        fresh = upgraded.create(self.owner, Settings(language="fr"), original_saved=True, now=1003)
        self.assertNotEqual(fresh.id, closed.id)
        self.assertEqual(upgraded.get(closed.id, self.owner), closed)
        self.assertEqual(closed.original, draft.original)
        self.assertEqual(closed.steps["credentials"], draft.steps["credentials"])
        self.assertEqual(closed.form, {})

    def test_acknowledgement_accepts_only_a_typed_reason_and_reviewed_fingerprint(self):
        draft = self.create()
        for entry in ({"reason": "rollback", "settings_fingerprint": "a" * 64},
                      {"reason": "conflict", "settings_fingerprint": "secret"},
                      {"reason": "paused", "settings_fingerprint": "a" * 64, "password": "secret"},
                      {"reason": "paused"}, {"settings_fingerprint": "a" * 64}):
            with self.subTest(entry=entry), self.assertRaisesRegex(HostError, "invalid_setup_draft"):
                self.store.cas(replace(draft, steps={"acknowledgement": entry}), expected_revision=0, now=1001)


if __name__ == "__main__":
    unittest.main()
