"""Bilingual real-host write/recovery with synthetic remote services only."""

import asyncio
import json
import os
import sys
import unittest
from contextlib import closing
from pathlib import Path

from tests.telegram_support import GatewayHarness

CASES = ((None, "en", "Start with these settings", "Retry failed step"),
         ("beta", "ko", "이 설정으로 시작", "실패 단계 다시 시도"))


async def smoke(root: Path, phase: str) -> None:
    case = unittest.TestCase()
    # Secure synthetic input files from creation. Hermes intentionally preserves
    # existing credential-file modes in containers; this does not test repairing
    # an operator's insecure or group-managed profile permissions.
    previous_umask = os.umask(0o077)
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    evidence_path = root / "container-evidence.json"
    evidence = {} if phase == "write" else json.loads(evidence_path.read_text())
    h = GatewayHarness(root=root)
    try:
        h.offline_mail_services()
        for route, language, confirm, retry in CASES:
            password = "test-only-CONTAINER-password-" + language
            if phase == "write":
                await h.secure_preview(route=route, language=language, password=password)
                h.send_failure = True
                await h.press(confirm, route=route)
            else:
                await h.dispatch_native(route=route, language_code=language)
                await h.press(retry, route=route)
            receipt = await h.submit_native(route=route)
            case.assertIsNotNone(receipt, "actual host admission required")
            resource = h.profile_resource(route or "default")
            def inspect():
                settings = resource.host.load_settings()
                case.assertEqual(len(settings.accounts), 1)
                account = settings.accounts[0]
                case.assertEqual(resource.host.get_password(account), password)
                case.assertEqual((settings.language, settings.ui_language), (language, language))
                case.assertEqual(settings.target, "telegram:111")
                with closing(resource.host.open_store()) as store:
                    case.assertEqual(store.cursor(account.id), (7, 100))
                    case.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 3)
                with closing(resource.host.open_setup_store()) as store:
                    case.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
                    draft_id, state = store.db.execute("SELECT id, state FROM drafts").fetchone()
                    case.assertEqual(state, "repair_required" if phase == "write" else "completed")
                case.assertEqual(len(resource.host.manage_cron("list", include_disabled=True)["jobs"]), 1)
                case.assertFalse(resource.service._secrets)
                return {"account_id": account.id, "draft_id": draft_id}
            record = await h.profiles[route or "default"].controller.runtime.submit(receipt, inspect)
            if phase == "write":
                evidence[language] = record
            else:
                case.assertEqual(record, evidence[language])
            home = h.beta_home if route else h.home
            data = home / "plugin-data/hermes-mail-notifier"
            for path, mode in ((data, 0o700), (data / "data.db", 0o600), (data / "setup.db", 0o600), (home / ".env", 0o600)):
                case.assertEqual(path.stat().st_mode & 0o777, mode, str(path))
            deliveries = home / "test-deliveries.jsonl"
            if phase == "write":
                case.assertFalse(deliveries.exists())
            else:
                case.assertEqual(len(deliveries.read_text().splitlines()), 1)
        if phase == "write":
            evidence_path.write_text(json.dumps(evidence))
        else:
            case.assertEqual(h.imap_probes, [], "recovery must not reset the baseline")
            case.assertFalse(any(kind == "model" for kind, *_ in h.external_requests))
        h.assert_secret_free(case, ["test-only-CONTAINER-password-" + language for _, language, *_ in CASES])
        h._assert_no_egress()
    finally:
        try:
            await h.close()
        finally:
            os.umask(previous_umask)
    print(json.dumps({"phase": phase, "languages": ["en", "ko"], "pid": os.getpid(),
                      "result": "passed", "remote_services": "fake", "live_verified": False}))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"write", "resume"}:
        raise SystemExit("usage: python -m tests.container.smoke write|resume")
    asyncio.run(smoke(Path("/profile"), sys.argv[1]))
