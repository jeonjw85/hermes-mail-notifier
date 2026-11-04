import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mail_notifier.config import Account, Settings, dump_settings
from mail_notifier.store import Store
from tests.support import account_data, feature


class Context:
    def __init__(self):
        self.settings = {}
        self.jobs = []
        self.calls = []
        self.llm = types.SimpleNamespace(complete_structured=Mock(return_value=types.SimpleNamespace(parsed={"answer": 1})))

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def dispatch_tool(self, name, args):
        assert name == "cronjob_manage"
        self.calls.append(args)
        action = args["action"]
        if action == "list":
            return json.dumps({"success": True, "jobs": self.jobs})
        if action == "create":
            job = {**args, "job_id": "job" + str(len(self.jobs)), "prompt_preview": args["prompt"], "enabled": not args.get("paused", False)}
            self.jobs.append(job)
        else:
            job = next(j for j in self.jobs if j["job_id"] == args["job_id"])
            if action == "update":
                job.update(args)
            elif action == "pause":
                job["enabled"] = False
            elif action == "resume":
                job["enabled"] = True
        return json.dumps({"success": True, "job": job, "job_id": job["job_id"]})


class HermesHostTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.hermes_host")
        self.temp = tempfile.TemporaryDirectory(prefix="mail 호스트 ")
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve()
        self.secrets = {}
        self.ctx = Context()
        self.host = self.module.HermesHost(self.ctx)
        self.account = Account(**account_data())
        self.settings = Settings(accounts=(self.account,))
        self.store = Store(sqlite3.connect(self.profile / "test.db"), self.profile / "run.lock")
        self.addCleanup(self.store.close)

        def save_secret(key, value):
            self.secrets[(str(self.profile), key)] = value

        modules = {
            "hermes_constants": types.SimpleNamespace(get_hermes_home=lambda: self.profile),
            "hermes_cli.config": types.SimpleNamespace(save_env_value=save_secret),
            "agent.secret_scope": types.SimpleNamespace(get_secret=lambda key: self.secrets.get((str(self.profile), key))),
            "tools.cronjob_tools": types.SimpleNamespace(),
        }
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.executable = patch.object(self.module.shutil, "which", return_value="/usr/local/bin/hermes")
        self.executable.start()
        self.addCleanup(self.executable.stop)

    def test_constructor_is_lazy_and_settings_roundtrip(self):
        untouched = Mock()
        self.module.HermesHost(untouched)
        self.assertEqual(untouched.mock_calls, [])
        self.host.save_settings(self.settings)
        self.assertEqual(self.host.load_settings(), self.settings)
        self.assertEqual(self.ctx.settings, {"configuration": dump_settings(self.settings)})

    def test_disable_only_schedule_reconciliation_never_creates_or_resumes_a_job(self):
        self.assertEqual(self.host.install_schedule(self.settings, self.store, disable_only=True), "")
        self.assertEqual(self.ctx.jobs, [])
        job = self.host.install_schedule(self.settings, self.store)
        self.ctx.jobs[0]["enabled"] = False
        self.ctx.calls.clear()
        self.assertEqual(self.host.install_schedule(self.settings, self.store, disable_only=True), job)
        self.assertEqual([call["action"] for call in self.ctx.calls], ["list"])
        self.assertTrue(self.store.get_meta("cron_paused"))
        self.assertFalse(self.ctx.jobs[0]["enabled"])

    def test_saved_settings_distinguish_defaults_empty_snapshots_and_legacy_values(self):
        self.assertFalse(self.host.has_saved_settings())
        for saved in ({"configuration": {}}, {"configuration": dump_settings(Settings())},
                      {"accounts": []}, {"language": "fr"}, {"ui_language": ""}, {"important_senders": []}):
            with self.subTest(saved=saved):
                self.ctx.settings = saved
                self.assertTrue(self.host.has_saved_settings())
        self.ctx.settings = {"unrelated": True}
        self.assertFalse(self.host.has_saved_settings())

    def test_setup_store_uses_plugin_data_dir_without_opening_mail_database(self):
        from mail_notifier.onboarding_auth import SetupOwner
        data = self.profile / "plugin data"
        storage = types.SimpleNamespace(plugin_data_dir=lambda name: data if name == "hermes-mail-notifier" else None)
        with patch.dict(sys.modules, {"plugins.plugin_storage": storage}):
            store = self.host.open_setup_store()
        self.addCleanup(store.close)
        owner = SetupOwner(str(self.profile), "cli")
        draft = store.create(owner, Settings(), original_saved=False, now=1000)
        self.assertEqual(store.get(draft.id, owner), draft)
        self.assertTrue((data / "setup.db").is_file())
        self.assertFalse((data / "data.db").exists())

    def test_interrupted_save_never_combines_new_accounts_with_old_target(self):
        original = Settings(target="telegram:111")
        self.ctx.settings = dump_settings(original)  # Existing flat settings remain readable.
        changed = Settings(accounts=(self.account,), target="telegram:222")
        writer = self.ctx.set_config
        def interrupted(key, value):
            if key in {"target", "configuration"}:
                raise OSError("simulated interrupted publish")
            writer(key, value)
        with patch.object(self.ctx, "set_config", side_effect=interrupted), self.assertRaises(self.module.HostError):
            self.host.save_settings(changed)
        self.assertEqual(self.host.load_settings(), original)
        self.host.save_settings(changed)
        self.assertEqual(self.host.load_settings(), changed)

    def test_scoped_credentials_and_blocked_save(self):
        self.host.save_password(self.account, "test-only-password")
        self.assertEqual(self.host.get_password(self.account), "test-only-password")
        self.profile = self.profile / "another profile"
        with self.assertRaises(self.module.HostError):
            self.host.get_password(self.account)
        with patch.object(sys.modules["hermes_cli.config"], "save_env_value", lambda *args: None):
            with self.assertRaises(self.module.HostError):
                self.host.save_password(self.account, "test-only-password")

    def test_structured_call_uses_host_model_with_timeout(self):
        self.assertEqual(self.host.complete_structured({"instructions": "test", "input": []}).parsed, {"answer": 1})
        args = self.ctx.llm.complete_structured.call_args.kwargs
        self.assertEqual(args["timeout"], 45)
        self.assertNotIn("provider", args)
        self.assertNotIn("model", args)
        self.ctx.llm.complete_structured.side_effect = TimeoutError("private provider details")
        with self.assertRaises(self.module.ModelUnavailable) as caught:
            self.host.complete_structured({})
        self.assertNotIn("private", str(caught.exception))

    def test_host_api_mismatch_is_not_reported_as_a_model_failure(self):
        # A host that lacks both model methods is genuinely incompatible.
        self.ctx.llm = types.SimpleNamespace()
        with self.assertRaises(self.module.HostError) as caught:
            self.host.complete_structured({"instructions": "x", "input": []})
        self.assertEqual(caught.exception.code, "incompatible_hermes")

    def test_older_structured_signature_is_adapted_not_rejected(self):
        # Optional keywords come and go between host revisions. Only ask for
        # the ones the installed host actually accepts.
        calls = {}

        def older(instructions, input, json_schema=None, schema_name=None):
            calls["kwargs"] = {"instructions": instructions, "input": input}
            return types.SimpleNamespace(parsed={"answer": 1}, usage=None, provider="p", model="m")

        self.ctx.llm = types.SimpleNamespace(complete_structured=older)
        self.assertEqual(self.host.complete_structured(
            {"instructions": "x", "input": [], "json_schema": {}, "schema_name": "t"}).parsed, {"answer": 1})
        self.assertEqual(calls["kwargs"]["instructions"], "x")

    def test_host_without_structured_completion_uses_text_completion(self):
        # A host exposing only ctx.llm.complete must still work: the plugin
        # validates the schema itself, so it only needs the JSON text back.
        sent = {}

        def complete(messages, **kwargs):
            sent["messages"] = messages
            return types.SimpleNamespace(text='```json\n{"answer": 1}\n```', usage=None, provider="p", model="m")

        self.ctx.llm = types.SimpleNamespace(complete=complete)
        reply = self.host.complete_structured(
            {"instructions": "classify", "input": [{"type": "text", "text": "body"}],
             "json_schema": {"type": "object"}, "schema_name": "mail_triage"})
        self.assertEqual(reply.parsed, {"answer": 1})
        # The schema and instructions must reach the model in the fallback too.
        rendered = str(sent["messages"])
        self.assertIn("classify", rendered)
        self.assertIn("body", rendered)

    def test_text_fallback_reports_unparseable_output_as_invalid_model_output(self):
        self.ctx.llm = types.SimpleNamespace(
            complete=lambda messages, **kw: types.SimpleNamespace(text="not json at all"))
        with self.assertRaises(self.module.TriageError) as caught:
            self.host.complete_structured({"instructions": "x", "input": []})
        self.assertEqual(caught.exception.code, "invalid_model_output")

    def test_model_failure_names_the_part_that_needs_fixing(self):
        # "model_unavailable" cannot tell "not logged in" from "out of credits"
        # from "model retired"; those need different fixes.
        cases = {
            "No LLM provider configured for task=None provider=auto. Run: hermes setup": "model_not_configured",
            "Provider 'anthropic' is set in config.yaml but no credentials were found. Run `hermes auth add anthropic`": "model_auth_failed",
            "Provider 'openrouter' is out of usable credentials: its only credential is exhausted (last error 402: Insufficient credits)": "model_out_of_credits",
            "Provider 'nous' is set in config.yaml but its only credential is cooling down after a rate limit / quota error (429)": "model_rate_limited",
            "Error code: 404 - model `claude-9` not found, use one of: claude-sonnet-4-5": "model_not_found",
            "something private happened": "model_unavailable",
        }
        for message, code in cases.items():
            with self.subTest(code=code):
                self.ctx.llm.complete_structured.side_effect = RuntimeError(message)
                with self.assertRaises(self.module.ModelUnavailable) as caught:
                    self.host.complete_structured({})
                self.assertEqual(caught.exception.code, code)
                # Provider text, URLs and keys must never ride along.
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("sk-", str(caught.exception))

    def test_usage_is_preserved_without_raw_model_text(self):
        self.ctx.llm.complete_structured.return_value = types.SimpleNamespace(
            parsed={"answer": 1}, text="private raw model output", provider="test-provider", model="test-model",
            usage=types.SimpleNamespace(input_tokens=12, output_tokens=3, total_tokens=15, cost_usd=None))
        reply = self.host.complete_structured({}, timeout=1.5)
        self.assertEqual(reply.usage["total_tokens"], 15)
        self.assertIsNone(reply.usage["cost_usd"])
        self.assertEqual(reply.model, "test-model")
        self.assertNotIn("private raw", repr(reply))
        self.assertEqual(self.ctx.llm.complete_structured.call_args.kwargs["timeout"], 1.5)

    def test_send_uses_stdin_and_clean_profile_environment(self):
        proc = subprocess.CompletedProcess([], 0, json.dumps({"success": True, "message_id": "99"}), "")
        with patch.dict(os.environ, {"HERMES_PROFILE": "other", "TELEGRAM_BOT_TOKEN": "test-only-secret", "HERMES_MAIL_A1_APP_PASSWORD": "test-only-secret"}), patch.object(self.module.subprocess, "run", return_value=proc) as run:
            self.assertEqual(self.host.send("telegram:123:456", "summary")["message_id"], "99")
        argv = run.call_args.args[0]
        kw = run.call_args.kwargs
        self.assertEqual(argv, ["/usr/local/bin/hermes", "send", "--to", "telegram:123:456", "--file", "-", "--json"])
        self.assertEqual(kw["input"], "summary")
        self.assertEqual(kw["env"]["HERMES_HOME"], str(self.profile))
        self.assertNotIn("HERMES_PROFILE", kw["env"])
        # The gateway may hold the bot token only in this process; a one-shot
        # send must forward it or it cannot deliver at all.
        self.assertEqual(kw["env"]["TELEGRAM_BOT_TOKEN"], "test-only-secret")
        # Other profile secrets and the mail password never travel.
        self.assertNotIn("HERMES_MAIL_A1_APP_PASSWORD", kw["env"])
        self.assertNotIn("test-only-secret", repr(argv))
        self.assertEqual(kw["timeout"], 30)

    def test_environment_preflight_rejects_unreloadable_scoped_credentials(self):
        self.secrets[(str(self.profile), "TELEGRAM_BOT_TOKEN")] = "test-only-token"
        config_module = sys.modules["hermes_cli.config"]
        provider = types.SimpleNamespace(api_key_env_vars=("MODEL_TEST_KEY",), base_url_env_var="MODEL_TEST_URL")
        self.secrets[(str(self.profile), "MODEL_TEST_KEY")] = "test-only-model-key"
        with patch.object(config_module, "load_config_readonly", return_value={"model": {"provider": "alias"}}, create=True), patch.dict(sys.modules, {"hermes_cli.auth": types.SimpleNamespace(PROVIDER_REGISTRY={"configured": provider}, _PROVIDER_ALIASES={"alias": "configured"})}), patch.object(self.module.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"reloadable": false}', "")
            with self.assertRaises(self.module.HostError) as caught:
                self.host.check_environment(self.settings)
            self.assertEqual(caught.exception.code, "credentials_not_reloadable")
            self.assertNotIn("test-only-token", str(caught.exception))
            self.assertNotIn("test-only-token", repr(run.call_args.args[0]))
            self.assertIn("MODEL_TEST_KEY", json.loads(run.call_args.kwargs["input"])["expected"])
            run.return_value = subprocess.CompletedProcess([], 0, '{"reloadable": true}', "")
            self.host.check_environment(self.settings)

    def test_environment_preflight_keeps_runtime_imports_without_inheriting_secrets(self):
        # PM installs expose host code/dependencies through sys.path, not the
        # interpreter's own site-packages. Exercise a real clean child process.
        runtime = self.profile / "runtime packages"
        sources = {
            "hermes_cli/__init__.py": "",
            "hermes_cli/env_loader.py": (
                "import os\nfrom pathlib import Path\n"
                "def load_hermes_dotenv():\n"
                "    path = Path(os.environ['HERMES_HOME']) / '.env'\n"
                "    if path.exists():\n"
                "        for line in path.read_text().splitlines():\n"
                "            key, value = line.split('=', 1)\n"
                "            os.environ[key] = value\n"
            ),
            "hermes_cli/send_cmd.py": "def _load_hermes_env(): pass\n",
            "agent/__init__.py": "",
            "agent/secret_scope.py": "from os import environ\nget_secret = environ.get\n",
        }
        for name, source in sources.items():
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        self.secrets[(str(self.profile), "TELEGRAM_BOT_TOKEN")] = "test-only-token"
        config_module = sys.modules["hermes_cli.config"]
        with patch.object(sys, "path", [str(runtime), *sys.path]), patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-only-token"}), patch.object(config_module, "load_config_readonly", return_value={}, create=True), patch.dict(sys.modules, {"hermes_cli.auth": types.SimpleNamespace(PROVIDER_REGISTRY={})}):
            with self.assertRaises(self.module.HostError) as caught:
                self.host.check_environment(self.settings)
            self.assertEqual(caught.exception.code, "credentials_not_reloadable")
            (self.profile / ".env").write_text("TELEGRAM_BOT_TOKEN=test-only-token\n", encoding="utf-8")
            self.host.check_environment(self.settings)

    def test_environment_preflight_distinguishes_probe_errors_from_missing_credentials(self):
        self.secrets[(str(self.profile), "TELEGRAM_BOT_TOKEN")] = "test-only-token"
        config_module = sys.modules["hermes_cli.config"]
        results = [
            subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: hermes_cli; test-only-token"),
            subprocess.CompletedProcess([], 0, "not json", ""),
            subprocess.CompletedProcess([], 0, '{}', ""),
            subprocess.CompletedProcess([], 0, '{"reloadable": "false"}', ""),
        ]
        with patch.object(config_module, "load_config_readonly", return_value={}, create=True), patch.dict(sys.modules, {"hermes_cli.auth": types.SimpleNamespace(PROVIDER_REGISTRY={})}):
            for result in results:
                with self.subTest(result=result), patch.object(self.module.subprocess, "run", return_value=result):
                    with self.assertRaises(self.module.HostError) as caught:
                        self.host.check_environment(self.settings)
                    self.assertEqual(caught.exception.code, "credential_reload_check_failed")
                    self.assertNotIn("test-only-token", str(caught.exception))

    def test_send_forwards_an_ambient_bot_token_but_cron_reload_does_not(self):
        # The gateway may hold the token only in its process environment.
        # A one-shot send must still work; the cron reload gate must still
        # refuse to count that ambient value as "reloadable".
        seen = {}

        def capture(cmd, **kwargs):
            seen["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"success": True, "message_id": "9"}), "")

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-only-token",
                                     "TELEGRAM_HOME_CHANNEL": "-100123"}), \
                patch.object(self.module.subprocess, "run", side_effect=capture):
            self.host.send("telegram", "summary")
        self.assertEqual(seen["env"].get("TELEGRAM_BOT_TOKEN"), "test-only-token")
        self.assertEqual(seen["env"].get("TELEGRAM_HOME_CHANNEL"), "-100123")

        # The cron child keeps the strict list: no ambient credentials, so the
        # reload gate (which reads the same env) still proves reloadability.
        self.assertEqual(self.host._child_env().get("TELEGRAM_BOT_TOKEN"), None)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", self.module._CHILD_ENV)
        self.assertIn("TELEGRAM_BOT_TOKEN", self.module._SEND_ENV)

    def test_delivery_failure_names_the_cause(self):
        # The host already explains itself ("No live adapter...", "Plugin
        # standalone send failed: ...", a bot API error). Collapsing all of it
        # into delivery_failed leaves the user with "check Telegram", which is
        # the one thing they already did.
        cases = {
            "No live adapter for platform 'telegram'. Is the gateway running with this platform connected?":
                "delivery_no_adapter",
            "Plugin standalone send failed: chat not found": "delivery_destination_not_found",
            "Adapter send failed: Forbidden: bot was blocked by the user": "delivery_forbidden",
            "Send failed: Error code: 401 - Unauthorized": "delivery_auth_failed",
            "Send failed: Error code: 429 - Too Many Requests: retry after 30": "delivery_rate_limited",
            "Send failed: Request timed out": "delivery_timeout",
            "Platform 'telegram' is not configured. Looked in: /home/x/.hermes/.env (missing), "
            "/home/x/.hermes/config.yaml (no platforms.telegram block), environment (TELEGRAM_BOT_TOKEN unset)":
                "delivery_not_configured",
            "Something nobody classified": "delivery_failed",
        }
        for message, code in cases.items():
            with self.subTest(code=code):
                output = json.dumps({"error": message})
                with patch.object(self.module.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 1, output, "")):
                    with self.assertRaises(self.module.DeliveryError) as caught:
                        self.host.send("telegram", "summary")
                self.assertEqual(caught.exception.code, code)

    def test_delivery_guidance_explains_the_stripped_child_environment(self):
        # send() runs `hermes send` with a minimal env, so a token that exists
        # only in the gateway process cannot reach it. The code must say so,
        # rather than sending the user back to "check Telegram".
        from mail_notifier.diagnostics import next_action
        guidance = next_action("delivery_not_configured", language="en")
        self.assertIn(".env", guidance)
        self.assertIn("TELEGRAM_BOT_TOKEN", guidance)

    def test_send_requires_actual_delivery_evidence(self):
        invalid = [{"success": True}, {"success": True, "message_id": "9", "delivered": False},
                   {"success": True, "message_id": "9", "partial_success": True},
                   {"success": True, "message_id": "9", "skipped": True}, {"success": False}, []]
        for payload in invalid:
            with self.subTest(payload=payload), patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
                with self.assertRaises(self.module.DeliveryError):
                    self.host.send("telegram", "summary")
        for code, text in [(0, 'noise\n{"success":true,"message_id":"9"}'), (1, '{"success":true,"message_id":"9"}')]:
            with patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess([], code, text, "")), self.assertRaises(self.module.DeliveryError):
                self.host.send("telegram", "summary")

        with patch.object(self.module.subprocess, "run", side_effect=subprocess.TimeoutExpired("hermes", 30)), self.assertRaises(self.module.DeliveryError):
            self.host.send("telegram", "summary")

    def test_resolve_target_freezes_home_channel_and_topic(self):
        self.assertTrue(hasattr(self.host, "resolve_target"), "destination resolution is not implemented")
        home = types.SimpleNamespace(chat_id="-100123", thread_id="456")
        gateway = types.SimpleNamespace(Platform=types.SimpleNamespace(TELEGRAM="telegram"),
                                        load_gateway_config=lambda: types.SimpleNamespace(get_home_channel=lambda _: home))
        with patch.dict(sys.modules, {"gateway.config": gateway,
                                      "hermes_cli.send_cmd": types.SimpleNamespace(_load_hermes_env=lambda: None)}):
            saved = self.host.resolve_target("telegram")
            home.chat_id = "-100789"
            self.assertEqual(saved, "telegram:-100123:456")
            self.assertEqual(self.host.resolve_target(saved), saved)
    def test_schedule_recovers_after_crash_before_saving_id(self):
        job_id = self.host.install_schedule(self.settings, self.store)
        self.store.set_meta("cron_job_id", None)
        self.assertEqual(self.host.install_schedule(self.settings, self.store), job_id)
        self.assertEqual(len(self.ctx.jobs), 1)
        job = self.ctx.jobs[0]
        self.assertEqual(job["deliver"], "local")
        self.assertEqual(job["failure_deliver"], "telegram")
        self.assertTrue(job["no_agent"])
        self.assertEqual(job["script"], "hermes-mail-notifier.py")
        shim = (self.profile / "scripts/hermes-mail-notifier.py").read_text()
        self.assertIn(str(self.profile), shim)
        self.assertNotIn("test-only-password", shim)

    def test_paused_schedule_stays_paused(self):
        self.host.install_schedule(self.settings, self.store)
        self.ctx.jobs[0]["enabled"] = False
        self.host.install_schedule(self.settings, self.store)
        self.assertFalse(self.ctx.jobs[0]["enabled"])
        self.assertTrue(self.store.get_meta("cron_paused"))

    def test_explicit_resume_verifies_owned_job_not_just_stored_id(self):
        job_id = self.host.install_schedule(self.settings, self.store)
        self.ctx.jobs[0]["enabled"] = False
        self.host.resume_schedule(job_id, self.store)
        self.assertTrue(self.ctx.jobs[0]["enabled"])
        self.assertFalse(self.store.get_meta("cron_paused"))
        self.ctx.jobs[0]["prompt_preview"] = "user-owned"
        self.ctx.jobs[0]["enabled"] = False
        with self.assertRaises(self.module.HostError):
            self.host.resume_schedule(job_id, self.store)
        self.assertFalse(self.ctx.jobs[0]["enabled"])

    def test_foreign_job_is_not_modified_and_duplicate_owned_jobs_fail(self):
        self.ctx.jobs.append({"job_id": "foreign", "name": "hermes-mail-notifier", "prompt_preview": "user job", "script": "/other.py"})
        self.store.set_meta("cron_job_id", "foreign")
        self.host.install_schedule(self.settings, self.store)
        self.assertEqual(self.ctx.jobs[0]["prompt_preview"], "user job")
        self.assertFalse(any(c.get("job_id") == "foreign" for c in self.ctx.calls))
        self.ctx.jobs.append({**self.ctx.jobs[1], "job_id": "duplicate"})
        with self.assertRaises(self.module.HostError):
            self.host.install_schedule(self.settings, self.store)

    def test_no_enabled_accounts_pause_owned_job_without_creating_one(self):
        self.host.install_schedule(Settings(), self.store)
        self.assertEqual(self.ctx.jobs, [])
        self.host.install_schedule(self.settings, self.store)
        self.host.install_schedule(Settings(), self.store)
        self.assertFalse(self.ctx.jobs[0]["enabled"])


if __name__ == "__main__":
    unittest.main()
