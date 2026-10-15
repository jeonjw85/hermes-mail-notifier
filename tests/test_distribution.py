"""Offline distribution/configuration guards, not public-host or device proof."""

import contextvars
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from mail_notifier.config import Account
from mail_notifier.hermes_host import HermesHost
from mail_notifier.onboarding_auth import AdmissionReceipt, NativeInput, SetupOwner
from mail_notifier.service import check_mail
from mail_notifier.telegram_onboarding import TelegramOnboarding
from tests.support import account_data
from tests.test_cli import SetupHost
from tests.test_hermes_host import Context
from tests.test_service import FakeMailbox

ROOT = Path(__file__).resolve().parents[1]


def block(source, key, indent=0):
    """Read a block in our indentation-only YAML configs, without a YAML dependency.

    These are deliberately configuration checks; they do not execute Actions.
    """
    match = re.search(rf"(?m)^{' ' * indent}{re.escape(key)}:\s*\n", source)
    if match is None:
        raise AssertionError(f"Missing configuration block: {key}")
    lines = []
    for line in source[match.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


class DistributionConfigTests(unittest.TestCase):
    def test_pages_deployment_is_manual_and_only_publishes_static_frontend(self):
        path = ROOT / ".github/workflows/pages.yml"
        self.assertTrue(path.is_file(), "Manual static Pages workflow is missing")
        workflow = path.read_text()
        self.assertEqual(block(workflow, "on").strip(), "workflow_dispatch:")
        jobs = block(workflow, "jobs")
        build = block(jobs, "build")
        deploy = block(jobs, "deploy")
        self.assertIn("uses: actions/upload-pages-artifact@v3", build)
        self.assertRegex(build, r"(?m)^\s+path: web$")
        self.assertNotIn("upload-artifact@", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertEqual(block(build, "permissions").strip(), "contents: read")
        self.assertEqual(set(block(deploy, "permissions").splitlines()), {"pages: write", "id-token: write"})
        self.assertIn("needs: build", deploy)
        self.assertIn("name: github-pages", block(deploy, "environment"))
        self.assertIn("uses: actions/deploy-pages@v4", deploy)

    def test_static_artifact_contains_only_versioned_frontend_files(self):
        files = {p.relative_to(ROOT / "web").as_posix() for p in (ROOT / "web").rglob("*") if p.is_file()}
        self.assertEqual(files, {"credentials/v1/" + name for name in
                                ("index.html", "app.mjs", "crypto.mjs", "messages.mjs", "styles.css")})
        self.assertFalse(any(p.is_symlink() for p in (ROOT / "web").rglob("*")))
        for relative in files:
            data = (ROOT / "web" / relative).read_bytes()
            for secret in (b"-----BEGIN PRIVATE KEY", b"TELEGRAM_BOT_TOKEN=", b"APP_PASSWORD=", b"SQLite format 3"):
                self.assertNotIn(secret, data, relative)

    def test_ci_installs_dependencies_before_python_node_and_no_skip_host_contract(self):
        jobs = block((ROOT / ".github/workflows/ci.yml").read_text(), "jobs")
        unit = block(jobs, "unittest")
        self.assertIn('python: ["3.11", "3.14"]', unit)
        ordered = ["pip install -e '.[dev]'", "unittest discover", "compileall", "ruff check", "pyright", "node --test"]
        positions = [unit.index(command) for command in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("node-version: '24'", unit)
        self.assertIn("ref: d5aa9d7adf308ca466a118206c201254cf3ca977", unit)
        self.assertIn("-e '../hermes[telegram]'", unit)
        self.assertIn("ruamel.yaml==0.18.16", unit)
        self.assertIn("python-dotenv==1.2.2", unit)
        self.assertLess(unit.index("-e '../hermes[telegram]'"), unit.index("unittest discover"))
        host = block(jobs, "hermes-contract")
        self.assertIn("ref: d5aa9d7adf308ca466a118206c201254cf3ca977", host)
        self.assertIn("pip install -e './hermes[telegram]' -e './plugin[dev]'", host)
        self.assertLess(host.index("pip install"), host.index("import cryptography, telegram"))
        self.assertIn("from gateway.run import GatewayRunner", host)
        self.assertIn("HERMES_CONTRACT: '1'", host)
        self.assertIn("result.skipped", host)
        self.assertIn('discover("tests")', host)
        self.assertIn("tests.test_recovery", block(jobs, "load-recovery"))
        self.assertIn("tests.load_scenario", block(jobs, "load-recovery"))
        container = block(jobs, "container")
        self.assertIn("docker build -f tests/container/Dockerfile -t mail-onboarding-smoke .", container)
        self.assertIn("python tests/container/run.py", container)

    def test_host_gate_exits_nonzero_on_any_skip_or_failure(self):
        jobs = block((ROOT / ".github/workflows/ci.yml").read_text(), "jobs")
        host = block(jobs, "hermes-contract")
        match = re.search(r"python - <<'PY'\n(.*?)\n\s*PY", host, re.DOTALL)
        self.assertIsNotNone(match, "Missing executable no-skips host gate")
        gate = textwrap.dedent(match.group(1))
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory) / "tests"
            tests.mkdir()
            for body, expected in (("pass", 0), ("self.skipTest('missing host')", 1), ("self.fail('broken contract')", 1)):
                with self.subTest(body=body):
                    (tests / "test_gate.py").write_text("import unittest\nclass Gate(unittest.TestCase):\n    def test_contract(self):\n        " + body + "\n")
                    result = subprocess.run([sys.executable, "-B", "-c", gate], cwd=directory,
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_docker_context_is_allowlisted_and_image_has_no_broad_copy_or_runtime_install(self):
        path = ROOT / ".dockerignore"
        self.assertTrue(path.is_file(), "Sensitive local files must not enter the build context")
        rules = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(rules[0], "**")
        allowed = {line[1:] for line in rules[1:] if line.startswith("!")}
        self.assertEqual(allowed, {"pyproject.toml", "README.md", "LICENSE", "__init__.py", "plugin.yaml",
                                   "mail_notifier/", "mail_notifier/*.py", "tests/", "tests/*.py",
                                   "tests/container/", "tests/container/*.py", "tests/container/Dockerfile",
                                   "web/", "web/credentials/", "web/credentials/v1/",
                                   "web/credentials/v1/*.html", "web/credentials/v1/*.mjs", "web/credentials/v1/*.css"})
        dockerfile = (ROOT / "tests/container/Dockerfile").read_text()
        self.assertNotRegex(dockerfile, r"(?m)^COPY\s+(?:\.\s|\[\s*\"\.\")")
        self.assertNotRegex(dockerfile, r"(?mi)^EXPOSE\s")
        self.assertIn("[telegram]", dockerfile)
        self.assertIn("d5aa9d7adf308ca466a118206c201254cf3ca977", dockerfile)
        self.assertIn("pip install", dockerfile)


class MissingPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_public_page_does_not_break_existing_accounts_or_nonsecret_management(self):
        from mail_notifier.telegram_views import _ui
        with tempfile.TemporaryDirectory() as directory:
            host = SetupHost(directory)
            account = Account(**account_data())
            host.settings = replace(host.settings, accounts=(account,))
            host.passwords[account.id] = "test-only-existing-password"
            host.check_onboarding_compatibility = lambda: None
            host.credential_page_url = lambda: HermesHost(Context()).credential_page_url()
            owner = SetupOwner(str(Path(directory).resolve()), "telegram", "700", "111", "111")
            controller = TelegramOnboarding(SimpleNamespace())
            self.addAsyncCleanup(controller.runtime.close_profile, owner.profile_home)
            _ui()["profiles"][owner.profile_home] = controller
            self.addCleanup(_ui()["profiles"].pop, owner.profile_home, None)
            receipt = AdmissionReceipt(owner, "session", "42", contextvars.copy_context())
            number = 0
            async def send(kind, value=""):
                nonlocal number
                number += 1
                item = NativeInput(kind, "700", "111", "111", "", str(number), str(number), "en", value)
                return await controller.handle(replace(receipt, message_id=item.message_id), item)
            async def press(view, label):
                token = next(b["callback_data"] for b in view["buttons"] if b["text"] == label)
                return await send("callback", token)
            with patch("mail_notifier.telegram_onboarding.HermesHost", return_value=host):
                view = await send("start")
                self.assertTrue(any("a1@example.com" in b["text"] for b in view["buttons"]))
                view = await press(view, "Connect another account")
                view = await press(view, "Gmail")
                await controller.bind_prompt(receipt, view, "500")
                view = await send("reply", json.dumps({"prompt_message_id": "500", "text": "new@example.com"}))
                result = await press(view, "Secure app password")
                self.assertEqual(result.get("error"), "credential_page_unavailable")
                self.assertNotIn("web_app_url", result)
                self.assertIn("Do not send passwords", result["text"])
                self.assertIn("비밀번호", result["text"])
            box = FakeMailbox(account)
            with closing(host.open_store()) as store:
                store.initialize(account, 7, 100)
            box.add()
            self.assertEqual(check_mail(host, mailbox_factory=lambda *a, **kw: box).sent, 1)
            self.assertEqual(len(host.settings.accounts), 1)

    def test_url_validation_and_protocol_context_match_the_distributed_v1_screen(self):
        from mail_notifier.credential_session import CredentialBinding, CredentialSessions, credential_url
        from mail_notifier.hermes_host import HostError
        from tests.test_credential_session import compact, encrypt
        owner = SetupOwner(str(ROOT / "synthetic-profile"), "telegram", "700", "111", "111")
        binding = CredentialBinding(owner, "draft_1", 0)
        sessions = CredentialSessions()
        self.addCleanup(sessions.close)
        challenge = sessions.issue(binding)
        for url in ("http://example.invalid/", "file:///web/credentials/v1/", "https://user:pass@example.invalid/",
                    "https://example.invalid/#v=2", "https://example.invalid:99999/", "https://example.invalid/\n"):
            with self.subTest(url=url), self.assertRaises(HostError):
                credential_url(url, challenge, provider="gmail", account_label="test", ui_language="en")
        url = credential_url("https://operator.example/credentials/v1/", challenge,
                             provider="gmail", account_label="test", ui_language="en")
        public = parse_qs(parse_qs(urlsplit(url).fragment)["mn"][0])
        self.assertEqual(public["v"], ["1"])
        payload = json.loads(encrypt(challenge, compact({"password": "test-only-password", "draft_id": "draft_1", "revision": 0}).encode()))
        payload["v"] = 2
        with self.assertRaises(HostError) as caught:
            sessions.accept(json.dumps(payload), binding, update_key="700:1")
        self.assertEqual(caught.exception.code, "invalid_credential_submission")


class ContainerRunnerTests(unittest.TestCase):
    def run_runner(self, failure=""):
        path = ROOT / "tests/container/run.py"
        self.assertTrue(path.is_file(), "Two-container offline smoke runner is missing")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        docker = directory / "docker"
        docker.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import json, os, sys
            args = sys.argv[1:]
            with open(os.environ['DOCKER_LEDGER'], 'a') as log:
                log.write(json.dumps(args) + '\\n')
            if args[:2] == ['volume', 'create']:
                print(args[-1])
            if os.environ.get('DOCKER_FAILURE') in ('create', 'write', 'resume', 'cleanup'):
                mode = os.environ['DOCKER_FAILURE']
                if ((mode == 'create' and args[:2] == ['volume', 'create']) or
                    (mode == 'cleanup' and args[:2] == ['volume', 'rm']) or
                    (args[:1] == ['run'] and args[-1] == mode)):
                    sys.exit(9)
            '''))
        docker.chmod(0o700)
        ledger = directory / "commands.jsonl"
        result = subprocess.run([sys.executable, str(path)], env={**os.environ, "PATH": str(directory),
                                "DOCKER_LEDGER": str(ledger), "DOCKER_FAILURE": failure},
                                text=True, capture_output=True, timeout=15)
        return result, [json.loads(line) for line in ledger.read_text().splitlines()]

    def test_two_distinct_containers_share_only_owned_uuid_volume_without_network_or_ports(self):
        result, commands = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(commands[0][:2], ["volume", "create"])
        volume = commands[0][-1]
        UUID(volume.removeprefix("mail-onboarding-smoke-"))
        self.assertEqual(commands[-1], ["volume", "rm", volume])
        runs = commands[1:-1]
        self.assertEqual(len(runs), 2)
        self.assertEqual([args[-1] for args in runs], ["write", "resume"])
        for args in runs:
            self.assertEqual(args[0], "run")
            self.assertIn("--rm", args)
            self.assertEqual(args[args.index("--network") + 1], "none")
            self.assertEqual(args.count("--mount"), 1)
            self.assertEqual(args[args.index("--mount") + 1], f"type=volume,src={volume},dst=/profile")
            self.assertFalse(any(arg in {"-p", "-P", "--publish", "--publish-all", "--privileged"} or arg.startswith("--publish=") for arg in args))
        self.assertIn("fake", result.stdout.lower())

    def test_failures_are_nonzero_and_cleanup_never_targets_unowned_volumes(self):
        for failure in ("create", "write", "resume", "cleanup"):
            with self.subTest(failure=failure):
                result, commands = self.run_runner(failure)
                self.assertNotEqual(result.returncode, 0)
                if failure == "create":
                    self.assertEqual(len(commands), 1)
                else:
                    self.assertEqual(commands[-1], ["volume", "rm", commands[0][-1]])
                self.assertFalse(any("prune" in command or "-f" in command for command in commands))
