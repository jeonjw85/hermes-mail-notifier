"""Subprocess fixture: os._exit bypasses cleanup at actual publication boundaries.

All profile files are supplied by the parent test. No live profiles or services.
"""

import asyncio
import io
import json
import logging
import os
import sys
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.telegram_support import GatewayHarness

PASSWORD = "test-only-CRASH-SENTINEL-password"
OLD_PASSWORD = "test-only-CRASH-SENTINEL-old-password"


async def run(path, point, rotation=False):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logging.getLogger().addHandler(handler)
    with redirect_stdout(output), redirect_stderr(output):
        h = GatewayHarness(root=path)
        try:
            h.offline_mail_services()
            def audit():
                h._assert_no_egress()
                h.assert_secret_free(unittest.TestCase(), [PASSWORD, OLD_PASSWORD], captured=output)
            if point == "resume":
                # The new process opens actual existing config/SQLite/secret/cron
                # files. Never reconstruct pending passwords from fixture values.
                await h.dispatch_native(route="beta")
                receipt = await h.submit_native(route="beta")
                if receipt is None:
                    raise AssertionError("resume not admitted by actual host")
                resource = h.profile_resource("beta")
                h.highest = 900  # detects accidental baseline re-snapshot/reset
                attempt = json.loads((h.root / "attempt.json").read_text())
                def recover():
                    with closing(resource.host.open_store()) as store, store.run_lock() as acquired:
                        if not acquired:
                            raise AssertionError("crashed worker retained mail lock")
                    with closing(resource.host.open_setup_store()) as store:
                        draft = store.get(attempt["draft_id"], receipt.owner)
                    if draft.state in {"awaiting_confirmation", "validating", "needs_secret", "awaiting_secret"}:
                        result = resource.service.preview(draft.id, receipt.owner)
                    elif draft.state == "completed":
                        result = resource.service.commit(draft.id, receipt.owner, draft.revision)
                    else:
                        result = resource.service.repair(draft.id, receipt.owner, draft.revision, step="settings")
                    with closing(resource.host.open_setup_store()) as store:
                        saved = store.get(draft.id, receipt.owner)
                        version = store.db.execute("PRAGMA user_version").fetchone()[0]
                    with closing(resource.host.open_store()) as store:
                        cursor = store.cursor(attempt["account_id"])
                    accounts = [{"account_id": a.id, "enabled": a.enabled} for a in resource.host.load_settings().accounts]
                    expected = ([{"account_id": attempt["account_id"], "enabled": attempt["enabled"]}]
                                if result["state"] == "completed" else attempt["original_accounts"])
                    if accounts != expected:
                        raise AssertionError("resumed settings changed account identity/enabled state; cursor alone is insufficient")
                    return {**result, "steps": saved.steps, "setup_version": version, "cursor": cursor,
                            "settings_accounts": accounts,
                            "cron_count": len(resource.host.manage_cron("list", include_disabled=True)["jobs"]),
                            "pending_secrets": len(resource.service._secrets), "lock_acquired": True}
                result = await h.profiles["beta"].controller.runtime.submit(receipt, recover)
                audit()
                return result

            if rotation:
                await h.secure_preview(route="beta", password=OLD_PASSWORD)
                await h.press("Start with these settings", route="beta")
                await h.dispatch_native(route="beta")
                markup = h.native_sent[-1][1]["reply_markup"]
                selector = next(button.text for row in markup.inline_keyboard for button in row
                                if "me@example.invalid" in button.text)
                await h.press(selector, route="beta")
                await h.press("Replace app password", route="beta")
                await h.press("Secure app password", route="beta")
                await h.dispatch_native(web_app=h.encrypted_payload(PASSWORD, name="beta"), route="beta")
                # Disregard the initial test; only the rotation's delivery counts.
                (h.beta_home / "test-deliveries.jsonl").unlink()
            else:
                await h.secure_preview(route="beta", password=PASSWORD)
            resource = h.profile_resource("beta")
            receipt = await h.submit_native(route="beta")
            if receipt is None:
                raise AssertionError("worker not admitted by actual host")
            def attempt_info():
                draft = resource.service.start(receipt.owner)
                return {"draft_id": draft.id, "account_id": draft.candidate.accounts[0].id,
                        "enabled": draft.candidate.accounts[0].enabled,
                        "original_accounts": [{"account_id": a.id, "enabled": a.enabled}
                                              for a in resource.host.load_settings().accounts]}
            attempt = await h.profiles["beta"].controller.runtime.submit(receipt, attempt_info)
            (h.root / "attempt.json").write_text(json.dumps(attempt))

            def crash(boundary):
                if point == boundary:
                    audit()
                    # Reached evidence contains no secret/ciphertext/private key.
                    (h.root / "crash-evidence.json").write_text(json.dumps({"boundary": boundary}))
                    os._exit(77)

            def intercept(obj, name, before, after):
                original = getattr(obj, name)
                def call(*args, **kwargs):
                    crash(before)
                    result = original(*args, **kwargs)
                    crash(after)
                    return result
                h.patch(obj, name, call)

            crash("validated_before_confirmation")
            host = resource.host
            intercept(host, "save_password", "before_credential_write", "credential_written_before_verified_marker")
            intercept(host, "save_settings", "before_settings_publication", "settings_written_before_marker")
            intercept(host, "install_schedule", "before_cron_write", "cron_written_before_marker")
            def storage_types():
                with closing(host.open_store()) as store, closing(host.open_setup_store()) as setup:
                    return type(store), type(setup)
            store_cls, setup_cls = receipt.context.copy().run(storage_types)
            intercept(store_cls, "initialize", "before_baseline_initialization", "baseline_initialized_before_settings")
            original_cas = setup_cls.cas
            def cas(store, draft, **kwargs):
                previous = store.get(draft.id, draft.owner)
                baseline = "baselines" in draft.steps and "baselines" not in previous.steps
                if baseline:
                    crash("before_baseline_intent")
                result = original_cas(store, draft, **kwargs)
                if (any(m.get("verified") for m in draft.steps.get("credentials", {}).values())
                        and not any(m.get("verified") for m in previous.steps.get("credentials", {}).values())):
                    crash("credential_verified_before_baseline_intent")
                if baseline:
                    crash("baseline_intent_written_before_initialization")
                for key, boundary in (("settings_fingerprint", "settings_marker_written"), ("cron", "cron_marker_written")):
                    if key in draft.steps and key not in previous.steps:
                        crash(boundary)
                if draft.steps.get("test_delivery", {}).get("started") and "test_delivery" not in previous.steps:
                    crash("before_test_send")
                if (draft.steps.get("test_delivery", {}).get("message_id")
                        and not previous.steps.get("test_delivery", {}).get("message_id")):
                    crash("test_recorded_before_completion")
                if draft.state == "completed":
                    crash("completed")
                return result
            with patch.object(setup_cls, "cas", cas):
                h.send_hook = lambda: crash("test_sent_before_marker")
                await h.press("Start with these settings", route="beta")
            raise AssertionError("requested crash boundary was not reached: " + point)
        finally:
            await h.close()
            logging.getLogger().removeHandler(handler)
            (Path(path) / "worker-finally-ran").touch()


if __name__ == "__main__":
    result = asyncio.run(run(sys.argv[1], sys.argv[2], "--rotation" in sys.argv[3:]))
    print(json.dumps(result))
