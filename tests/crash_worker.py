"""Subprocess-only fixture: crash at real durable-processing boundaries."""

import json
import os
import sys
from pathlib import Path

from mail_notifier.config import Account
from mail_notifier.service import check_mail
from mail_notifier.store import Store
from tests.support import account_data
from tests.test_service import FakeHost, FakeMailbox


def main(path, point):
    account = Account(**account_data())
    host = FakeHost(path, [account])
    box = FakeMailbox(account)
    box.add(101)
    store = host.open_store()
    store.initialize(account, 7, 100)
    store.close()
    def intercept(name, before, after):
        original = getattr(Store, name)
        def call(self, *args, **kwargs):
            if point == before:
                os._exit(77)
            result = original(self, *args, **kwargs)
            if point == after:
                os._exit(77)
            return result
        setattr(Store, name, call)
    intercept("cache_message", "before_cache", "after_cache")
    intercept("save_decision", "before_decision", "after_decision")
    intercept("finish", "unused", "after_record")
    def send(target, text, **kwargs):
        with (Path(path) / "transport.jsonl").open("a") as file:
            file.write(json.dumps({"target": target, "text": text}, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        if point == "after_send":
            os._exit(77)
        return {"success": True, "message_id": "1"}
    host.send = send
    report = check_mail(host, mailbox_factory=lambda *a, **kw: box, clock=lambda: 1000)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
