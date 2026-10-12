"""Lazy adapters for the host's profile, secrets, model, cron, and messaging."""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import PLUGIN_ID, Account, Settings, dump_settings, load_settings
from .store import Store
from .triage import TriageError

_MARKER = "Managed by hermes-mail-notifier."
_CHILD_ENV = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
})


class HostError(RuntimeError):
    def __init__(self, code="host_unavailable"):
        self.code = code
        super().__init__(code)


class DeliveryError(HostError):
    def __init__(self, code="delivery_failed"):
        super().__init__(code)


class ModelUnavailable(HostError):
    def __init__(self, code="model_unavailable"):
        super().__init__(code)


class HermesHost:
    def __init__(self, ctx):
        self.ctx = ctx

    def home(self) -> Path:
        try:
            from hermes_constants import get_hermes_home
            return get_hermes_home().expanduser().resolve()
        except Exception:
            raise HostError("profile_unavailable") from None

    def load_settings(self) -> Settings:
        snapshot = self.ctx.get_config("configuration", None)
        if snapshot is not None:
            return load_settings(snapshot)
        # Read the pre-snapshot layout until its next successful setup.
        defaults = dump_settings(Settings())
        return load_settings({key: self.ctx.get_config(key, value) for key, value in defaults.items()})

    def save_settings(self, settings: Settings) -> None:
        try:
            # The host atomically publishes one value. Accounts and destination
            # must change together, including when the process dies mid-setup.
            self.ctx.set_config("configuration", dump_settings(settings))
            if self.load_settings() != settings:
                raise HostError()
        except Exception:
            raise HostError("settings_save_failed") from None

    def get_password(self, account: Account) -> str:
        try:
            from agent.secret_scope import get_secret
            value = get_secret(account.password_secret)
            if not isinstance(value, str) or not value:
                raise HostError()
            return value
        except Exception:
            raise HostError("credential_unavailable") from None

    def save_password(self, account: Account, password: str) -> None:
        if not password or any(char in password for char in "\r\n\x00"):
            raise HostError("invalid_app_password")
        try:
            from hermes_cli.config import save_env_value
            save_env_value(account.password_secret, password)
            if self.get_password(account) != password:
                raise HostError()
        except Exception:
            raise HostError("credential_save_failed") from None

    def open_store(self) -> Store:
        try:
            from plugins.plugin_storage import plugin_data_dir, plugin_db
            data = plugin_data_dir(PLUGIN_ID)
            data.chmod(0o700)
            connection = plugin_db(PLUGIN_ID)
            (data / "data.db").chmod(0o600)
            try:
                return Store(connection, data / "run.lock")
            except Exception:
                connection.close()
                raise
        except Exception:
            raise HostError("storage_unavailable") from None

    def complete_structured(self, payload: dict):
        try:
            return self.ctx.llm.complete_structured(
                **payload, timeout=45, max_tokens=1800, purpose="mail-notifier-triage",
            ).parsed
        except ValueError:
            raise TriageError("invalid_model_output") from None
        except Exception:
            raise ModelUnavailable() from None

    def _executable(self) -> str:
        executable = shutil.which("hermes")
        if not executable:
            raise HostError("hermes_executable_missing")
        return str(Path(executable).absolute())

    def _child_env(self) -> dict:
        env = {key: value for key, value in os.environ.items() if key in _CHILD_ENV or key.startswith("LC_")}
        env["HERMES_HOME"] = str(self.home())
        return env

    def check_environment(self, settings: Settings) -> None:
        """Verify cron can reload the relevant credentials before activating it.

        Cron strips ambient secrets before our script runs. A current-profile
        export may work interactively yet be absent in that fresh process.
        Compare in a clean child, passing values only over stdin and emitting
        only a boolean. Never persist ambient credentials on the user's behalf.
        """
        if not any(account.enabled for account in settings.accounts):
            return
        try:
            from agent.secret_scope import get_secret
            from hermes_cli.config import load_config_readonly
            auth = importlib.import_module("hermes_cli.auth")
            registry = auth.PROVIDER_REGISTRY
            config = load_config_readonly() or {}
            model = config.get("model") or {}
            provider = model.get("provider", "auto") if isinstance(model, dict) else "auto"
            provider = str(provider).lower()
            provider = getattr(auth, "_PROVIDER_ALIASES", {}).get(provider, provider)
            names = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL"}
            candidates = list(registry.values()) if provider in ("", "auto") else [registry.get(provider)]
            for candidate in candidates:
                names.update(getattr(candidate, "api_key_env_vars", ()) or ())
                if getattr(candidate, "base_url_env_var", None):
                    names.add(candidate.base_url_env_var)
            if provider == "custom":
                names.update({"OPENAI_API_KEY", "OPENAI_BASE_URL"})

            def references(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {"key_env", "api_key_env", "api_key_env_var"} and isinstance(child, str):
                            names.add(child)
                        references(child)
                elif isinstance(value, list):
                    for child in value:
                        references(child)
                elif isinstance(value, str):
                    names.update(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", value))
            references(model)
            references((config.get("providers") or {}).get(provider, {}))
            expected = {name: value for name in names if (value := get_secret(name))}
            if not expected:
                return
            probe = (
                "import json,sys\n"
                "from hermes_cli.env_loader import load_hermes_dotenv\n"
                "from hermes_cli.send_cmd import _load_hermes_env\n"
                "from agent.secret_scope import get_secret\n"
                "expected=json.load(sys.stdin)\n"
                "load_hermes_dotenv()\n"
                "_load_hermes_env()\n"
                "print(json.dumps({'reloadable':all(get_secret(k)==v for k,v in expected.items())}))\n"
            )
            result = subprocess.run([sys.executable, "-c", probe], input=json.dumps(expected),
                                    env=self._child_env(), text=True, encoding="utf-8", capture_output=True,
                                    timeout=30, check=False)
            if result.returncode or json.loads(result.stdout).get("reloadable") is not True:
                raise HostError("credentials_not_reloadable")
        except HostError:
            raise
        except Exception:
            raise HostError("credential_reload_check_failed") from None

    def send(self, target: str, text: str) -> dict:
        # Revalidate even when called outside the normal configuration path.
        Settings(target=target)
        try:
            result = subprocess.run(
                [self._executable(), "send", "--to", target, "--file", "-", "--json"],
                input=text, text=True, encoding="utf-8", capture_output=True,
                env=self._child_env(), timeout=30, check=False,
            )
            payload = json.loads(result.stdout)
            if result.returncode or not isinstance(payload, dict) or payload.get("success") is not True:
                raise DeliveryError()
            if payload.get("error") or payload.get("partial_success") or payload.get("skipped") or payload.get("delivered") is False:
                raise DeliveryError()
            message_id = payload.get("message_id")
            if not message_id and isinstance(payload.get("response"), dict):
                response = payload["response"]
                if response.get("ok") is True and isinstance(response.get("result"), dict):
                    message_id = response["result"].get("message_id")
            if isinstance(message_id, bool) or not isinstance(message_id, (int, str)) or not str(message_id).strip():
                raise DeliveryError("delivery_unverified")
            return {"success": True, "message_id": str(message_id)}
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError() from None

    def manage_cron(self, action: str, **fields) -> dict:
        try:
            # This module registers the built-in tool when the CLI has not loaded it yet.
            importlib.import_module("tools.cronjob_tools")
            result = json.loads(self.ctx.dispatch_tool("cronjob_manage", {"action": action, **fields}))
            if not isinstance(result, dict) or result.get("success") is not True:
                raise HostError()
            return result
        except Exception:
            raise HostError("cron_operation_failed") from None

    def _write_shim(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        executable = self._executable()
        script = (
            "# Generated by hermes-mail-notifier; account data stays in the profile.\n"
            "import os\n"
            f"allowed = {sorted(_CHILD_ENV)!r}\n"
            "env = {k: v for k, v in os.environ.items() if k in allowed or k.startswith('LC_')}\n"
            f"env['HERMES_HOME'] = {str(self.home())!r}\n"
            f"os.execve({executable!r}, [{executable!r}, 'mail-notifier', 'check'], env)\n"
        )
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
                name = file.name
                file.write(script)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def install_schedule(self, settings: Settings, store: Store) -> str:
        script = self.home() / "scripts" / "hermes-mail-notifier.py"
        jobs = self.manage_cron("list", include_disabled=True).get("jobs")
        if not isinstance(jobs, list):
            raise HostError("invalid_cron_response")
        owned = [job for job in jobs if job.get("name") == PLUGIN_ID
                 and job.get("prompt_preview") == _MARKER
                 and job.get("script") and (script.parent / job["script"]).resolve() == script.resolve()]
        if len(owned) > 1:
            raise HostError("duplicate_owned_cron_jobs")
        existing = owned[0] if owned else None
        if not any(account.enabled for account in settings.accounts):
            if existing:
                self.manage_cron("pause", job_id=existing["job_id"])
                store.set_meta("cron_job_id", existing["job_id"])
                return existing["job_id"]
            store.set_meta("cron_job_id", None)
            return ""
        self._write_shim(script)
        fields = dict(name=PLUGIN_ID, prompt=_MARKER, schedule=settings.schedule,
                      script=script.name, no_agent=True, deliver="local", failure_deliver=settings.target)
        if existing:
            result = self.manage_cron("update", job_id=existing["job_id"], **fields)
        else:
            result = self.manage_cron("create", **fields)
        job_id = result.get("job_id") or result.get("job", {}).get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise HostError("invalid_cron_response")
        store.set_meta("cron_job_id", job_id)
        return job_id

    def check_compatibility(self) -> None:
        try:
            for method in ("get_config", "set_config", "dispatch_tool"):
                if not callable(getattr(self.ctx, method, None)):
                    raise HostError()
            if not callable(getattr(self.ctx.llm, "complete_structured", None)):
                raise HostError()
            for module, names in (
                ("plugins.plugin_storage", ("plugin_data_dir", "plugin_db")),
                ("agent.secret_scope", ("get_secret",)),
                ("hermes_cli.config", ("save_env_value",)),
            ):
                imported = importlib.import_module(module)
                if not all(callable(getattr(imported, name, None)) for name in names):
                    raise HostError()
            self._executable()
        except Exception:
            raise HostError("incompatible_hermes") from None
