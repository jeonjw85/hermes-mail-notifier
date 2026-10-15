"""Optional tests against an installed Hermes; all state is temporary, no live calls."""

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import venv
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mail_notifier.config import Account, PLUGIN_ID, Settings
from mail_notifier.hermes_host import HermesHost, HostError
from mail_notifier.message import MailContent
from mail_notifier.triage import triage_message
from tests.support import account_data, classification_data

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("HERMES_CONTRACT") == "1", "set HERMES_CONTRACT=1 in an installed Hermes environment")
class HermesContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hermes-contract ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "profile-a"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {
            "HOME": str(self.root), "HERMES_HOME": str(self.home),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "LANG": "en_US.UTF-8",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        self.manager = PluginManager(scope_key=str(self.home))
        self.addCleanup(self.manager.unload)
        self.manifest = PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID, version="0.1.0", source="project", path=str(ROOT),
                                       provides_tools=["mail_notifier_check", "mail_notifier_status"])
        self.ctx = PluginContext(self.manifest, self.manager)
        self.host = HermesHost(self.ctx)
        self.account = Account(**account_data())

    def test_native_loader_cli_and_plugin_doctor(self):
        with patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            self.manager._load_plugin(self.manifest)
            self.assertIsNone(self.manager._plugins[PLUGIN_ID].error)
            command = self.manager._cli_commands["mail-notifier"]
            parser = argparse.ArgumentParser()
            command["setup_fn"](parser)
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(command["handler_fn"](parser.parse_args(["status", "--json"])), 0)
            self.assertEqual(json.loads(out.getvalue())["pending"], 0)
        executable = str(Path(sys.executable).with_name("hermes"))
        result = subprocess.run([executable, "plugins", "doctor", str(ROOT), "--ci"], text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = self.home / "plugins" / PLUGIN_ID
        installed.parent.mkdir()
        installed.symlink_to(ROOT, target_is_directory=True)
        (self.home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-mail-notifier\n", encoding="utf-8")
        result = subprocess.run([executable, "mail-notifier", "status", "--json"], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["accounts"], [])
        script = self.home / "scripts/hermes-mail-notifier.py"
        self.host._write_shim(script)
        result = subprocess.run([sys.executable, str(script)], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["processed"], 0)

    def test_actual_settings_storage_and_secret_helpers_are_profile_scoped(self):
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope, set_multiplex_context, reset_multiplex_context
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        self.host.check_compatibility()
        self.host.save_settings(Settings(accounts=(self.account,)))
        self.host.save_password(self.account, "test-only-profile-a")
        self.assertEqual(self.host.load_settings().accounts, (self.account,))
        store = self.host.open_store()
        store.initialize(self.account, 7, 100)
        store.close()
        self.assertTrue((self.home / "plugin-data" / PLUGIN_ID / "data.db").is_file())
        self.assertNotIn("test-only-profile-a", (self.home / "config.yaml").read_text())
        other = self.root / "profile-b"
        other.mkdir()
        home_token = set_hermes_home_override(str(other))
        scope_token = set_secret_scope(build_profile_secret_scope(other), profile_home=str(other))
        multiplex_token = set_multiplex_context(True)
        try:
            self.assertEqual(self.host.load_settings().accounts, ())
            with self.assertRaises(HostError):
                self.host.get_password(self.account)
            other_store = self.host.open_store()
            self.assertIsNone(other_store.cursor(self.account.id))
            other_store.close()
        finally:
            reset_multiplex_context(multiplex_token)
            reset_secret_scope(scope_token)
            reset_hermes_home_override(home_token)
        self.assertEqual(self.host.get_password(self.account), "test-only-profile-a")

    def test_environment_only_telegram_and_model_credentials_are_detected_before_setup(self):
        from hermes_cli.config import save_env_value, save_config
        settings = Settings(accounts=(self.account,))
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-only-environment-token"
        with self.assertRaises(HostError) as caught:
            self.host.check_environment(settings)
        self.assertEqual(caught.exception.code, "credentials_not_reloadable")
        save_env_value("TELEGRAM_BOT_TOKEN", "test-only-environment-token")
        self.host.check_environment(settings)
        save_config({"model": {"provider": "openai-api", "default": "test-only-model"}})
        os.environ["OPENAI_API_KEY"] = "test-only-environment-key"
        with self.assertRaises(HostError):
            self.host.check_environment(settings)
        save_env_value("OPENAI_API_KEY", "test-only-environment-key")
        self.host.check_environment(settings)

    def test_credential_probe_with_host_dependencies_outside_interpreter(self):
        import hermes_cli
        from hermes_cli.config import save_env_value
        runtime = self.root / "bare runtime"
        venv.EnvBuilder(with_pip=False).create(runtime)
        paths = [str(Path(hermes_cli.__file__).resolve().parents[1]), *sys.path]
        settings = Settings(accounts=(self.account,))
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-only-environment-token"
        with patch.object(sys, "executable", str(runtime / "bin" / "python")), patch.object(sys, "path", paths):
            with self.assertRaises(HostError) as caught:
                self.host.check_environment(settings)
            self.assertEqual(caught.exception.code, "credentials_not_reloadable")
            save_env_value("TELEGRAM_BOT_TOKEN", "test-only-environment-token")
            self.host.check_environment(settings)

    def test_cron_list_create_update_pause_and_recover_id(self):
        self.host.validate_schedule("every 5m")
        with self.assertRaises(HostError) as caught:
            self.host.validate_schedule("evry five minutes")
        self.assertEqual(caught.exception.code, "invalid_schedule")
        script = self.home / "scripts/hermes-mail-notifier.py"
        self.host._write_shim(script)
        created = self.host.manage_cron("create", name=PLUGIN_ID, prompt="Managed by hermes-mail-notifier.",
                                        schedule="every 5m", script=script.name, no_agent=True, deliver="local",
                                        failure_deliver="telegram:123", paused=True)
        job_id = created["job_id"]
        store = self.host.open_store()
        try:
            self.assertEqual(self.host.install_schedule(Settings(accounts=(self.account,), schedule="every 10m"), store), job_id)
            self.assertEqual(store.get_meta("cron_job_id"), job_id)
            jobs = self.host.manage_cron("list", include_disabled=True)["jobs"]
            self.assertEqual(len(jobs), 1)
            self.assertFalse(jobs[0]["enabled"])
            self.host.manage_cron("pause", job_id=job_id)
            self.host.manage_cron("remove", job_id=job_id)
        finally:
            store.close()

    def test_migrated_destination_survives_real_home_channel_change(self):
        from hermes_cli.config import save_env_value
        from tests.test_migrations import create_v1
        save_env_value("TELEGRAM_BOT_TOKEN", "test-only-contract-token")
        save_env_value("TELEGRAM_HOME_CHANNEL", "111")
        data = self.home / "plugin-data" / PLUGIN_ID
        data.mkdir(parents=True)
        db = sqlite3.connect(data / "data.db")
        create_v1(db)
        db.close()
        with self.assertLogs("hermes_state", "WARNING") as logs:
            store = self.host.open_store()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("on-disk journal_mode was delete and has been switched to WAL", logs.output[0])
        try:
            self.assertEqual(store.due("a1", 2000, 10, status="ready")[0].target, "telegram:111")
            save_env_value("TELEGRAM_HOME_CHANNEL", "222")
            self.assertEqual(self.host.resolve_target("telegram"), "telegram:222")
            self.assertEqual(store.due("a1", 2000, 10, status="ready")[0].target, "telegram:111")
        finally:
            store.close()

    def test_structured_facade_and_real_media_parser_accept_our_payload(self):
        from gateway.platforms.base import BasePlatformAdapter
        from mail_notifier.render import render_notification
        mail = MailContent("boss@example.com", "MEDIA:/private/a.pdf", "계약", "", "내일 답장", False, False)
        output = classification_data(summary="MEDIA:/tmp/a.pdf [[audio_as_voice]]")
        captured = []
        def caller(**kwargs):
            captured.append(kwargs)
            return "test", "test", types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(output)))], usage=None)
        self.ctx.llm._sync_caller = caller
        result = triage_message(mail, Settings(), self.host.complete_structured)
        self.assertTrue(result.important)
        self.assertEqual(captured[0]["timeout"], 45)
        text = render_notification(self.account, mail, result)
        self.assertEqual(BasePlatformAdapter.extract_media(text)[0], [])
        self.assertEqual(BasePlatformAdapter.extract_local_files(text)[0], [])


if __name__ == "__main__":
    unittest.main()
