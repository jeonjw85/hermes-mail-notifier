"""Repeatable SQLite/pipeline load simulation; no external service calls."""

import argparse
import json
import re
import sqlite3
import tempfile
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

from mail_notifier.config import Account
from mail_notifier.evaluation import write_report
from mail_notifier.service import check_mail
from mail_notifier.store import Store
from tests.support import account_data
from tests.test_service import FakeHost, FakeMailbox


def simulate(*, accounts=50, messages=200):
    started = time.monotonic()
    tracemalloc.start()
    peaks = {"pending": 0, "cached": 0}
    class ObservedStore(Store):
        def enqueue(self, *args, **kwargs):
            count = super().enqueue(*args, **kwargs)
            pending = self.db.execute("SELECT count(*) FROM work WHERE status IN ('discovered','fetched','ready','held')").fetchone()[0]
            peaks["pending"] = max(peaks["pending"], pending)
            return count
        def cache_message(self, *args, **kwargs):
            super().cache_message(*args, **kwargs)
            peaks["cached"] = max(peaks["cached"], self.cached_count())
    try:
        with tempfile.TemporaryDirectory() as path:
            definitions = [Account(**account_data(f"a{i}")) for i in range(accounts)]
            host = FakeHost(path, definitions)
            host.settings = replace(host.settings, max_fetches=100, max_model_calls=100, max_deliveries=100)
            host.open_store = lambda: ObservedStore(sqlite3.connect(Path(path) / "state.db"), Path(path) / "lock", migration_target="telegram:111")
            boxes = {account.id: FakeMailbox(account) for account in definitions}
            store = host.open_store()
            for account in definitions:
                store.initialize(account, 7, 100)
                for uid in range(101, 101 + messages):
                    boxes[account.id].add(uid, subject=f"{account.id} request {uid}")
            store.close()
            runs = []
            for index in range(max(20, accounts * messages // 10)):
                # Two model outages fill caches/queues before a later process resumes.
                host.model_error = index < 2
                report = check_mail(host, mailbox_factory=lambda a, p, **kw: boxes[a.id], clock=lambda: 1000 + index * 4000)
                runs.append(report.to_dict())
                if len(host.deliveries) >= accounts * messages:
                    break
            store = host.open_store()
            try:
                status = store.status()
                completed = sum(row["sent"] == messages for row in status["accounts"])
            finally:
                store.close()
            identifiers = [re.search(r"메일 ID: ([a-f0-9]{32})", text).group(1) for _, text in host.deliveries]
            unique = len(set(identifiers))
            count = accounts * messages
            return {"mode": "simulation", "success": unique == len(identifiers) == count and completed == accounts
                    and status["pending"] == 0 and peaks["pending"] <= 2000 and peaks["cached"] <= 200,
                    "accounts": accounts, "events": count, "runs": len(runs), "run_reports": runs,
                    "unique_deliveries": unique, "completed_accounts": completed, "unfinished": status["pending"],
                    "peak_pending": peaks["pending"], "peak_cached": peaks["cached"],
                    "database_bytes": (Path(path) / "state.db").stat().st_size,
                    "duration_seconds": time.monotonic() - started, "peak_python_bytes": tracemalloc.get_traced_memory()[1]}
    finally:
        tracemalloc.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = simulate()
    if args.output:
        write_report(args.output, result)
    print(json.dumps({key: value for key, value in result.items() if key != "run_reports"}, indent=2))
    raise SystemExit(0 if result["success"] else 1)
