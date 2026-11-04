"""Lazy adapters for the host's profile, secrets, model, cron, and messaging."""

# Hermes modules are supplied by the host, not plugin runtime dependencies.
# pyright: reportMissingImports=false
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .config import PLUGIN_ID, Account, Settings, dump_settings, load_settings
from .store import StorageError, Store
from .triage import ModelReply, TriageError

if TYPE_CHECKING:
    from .onboarding_store import SetupStore

_MARKER = "Managed by hermes-mail-notifier."
# Deliberately excludes credentials. check_environment uses this list to prove a
# fresh child can RELOAD them from the profile; forwarding an ambient token
# would make that check pass while cron still fails.
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
    """A model call failed; the code names which part needs fixing.

    The host raises the same ``AuxiliaryClientUnavailable`` for "no provider
    configured", "this provider has no credentials", "the pool is out of
    credits" and "the credential is cooling down", and providers surface
    authentication and unknown-model errors through the same call. All of them
    used to collapse into ``model_unavailable``, whose advice was to re-check
    the provider — useless when the provider is fine and the login is not.

    Distinct codes keep the existing machinery: the durable allowlist, the
    grouped ``next_action`` guidance and the i18n table all key off codes.
    """


# Ordered: the first matching phrase wins. Keep the phrases terse and free of
# provider response text; they only select a code and are never shown.
_MODEL_FAILURES = (
    ("model_not_configured", ("no llm provider configured", "no provider available", "run: hermes setup")),
    ("model_auth_failed", ("no credentials were found", "not logged into", "hermes auth", "re-authenticate",
                           "invalid api key", "authentication failed", "401")),
    ("model_out_of_credits", ("out of usable credentials", "insufficient credits", "exhausted", "402")),
    ("model_rate_limited", ("cooling down", "rate limit", "429")),
    ("model_not_found", ("not found, use one of", "does not exist", "404")),
)


def model_failure(exc) -> str:
    """Pick the code for a failed model call without copying provider text."""
    body = str(exc).lower()
    for code, phrases in _MODEL_FAILURES:
        if any(phrase in body for phrase in phrases):
            return code
    return "model_unavailable"


# Delivery failures need the same treatment: "check the destination" is useless
# when the real cause is a blocked bot, a wrong token or no gateway adapter.
# Ordered; the first matching phrase wins.
_DELIVERY_FAILURES = (
    ("delivery_not_configured", ("is not configured", "looked in:", "bot_token unset", "telegram_bot_token unset",
                                 "no platforms.", "has no token", "no platforms.telegram block")),
    ("delivery_no_adapter", ("no live adapter", "is the gateway running", "standalone_sender_fn")),
    ("delivery_auth_failed", ("401", "unauthorized", "invalidtoken", "invalid token", "bot token", "not authorized",
                              "python-telegram-bot not installed")),
    ("delivery_forbidden", ("403", "forbidden", "blocked by the user", "bot was blocked", "not enough rights",
                            "chat_write_forbidden", "user is deactivated", "bot is not a member")),
    ("delivery_destination_not_found", ("chat not found", "peer_id_invalid", "user not found", "chat_id is empty",
                                        "unknown chat", "thread not found", "no deliverable text")),
    ("delivery_rate_limited", ("429", "too many requests", "retry after", "flood")),
    ("delivery_timeout", ("timed out", "timeout", "readtimeout", "connecttimeout")),
)


def delivery_failure(message) -> str:
    """Pick the code for a failed send from the host's own explanation."""
    body = str(message).lower()
    for code, phrases in _DELIVERY_FAILURES:
        if any(phrase in body for phrase in phrases):
            return code
    return "delivery_failed"


def _call_boundary_error(exc) -> bool:
    """True when a TypeError came from the call itself, not from inside the host.

    A wrong keyword or arity fails before the callee frame exists, so its
    traceback holds only the calling frame. A TypeError raised inside the host
    or provider carries that deeper frame and is a real runtime fault.
    """
    return isinstance(exc, TypeError) and exc.__traceback__ is not None and exc.__traceback__.tb_next is None


def _accepts(call, *names) -> bool:
    """True when *call*'s signature accepts all *names* (keyword-capable).

    Host revisions add and drop optional keywords; asking for one the installed
    host does not know would fail the whole call. A signature we cannot inspect
    is treated as accepting nothing, so the caller drops to the next option.
    """
    import inspect
    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):
        return False
    return all(name in parameters or any(p.kind is p.VAR_KEYWORD for p in parameters.values())
               for name in names)


def _completion_text(result) -> str:
    """Plain assistant text from either result shape, tolerating a bare string."""
    if isinstance(result, str):
        return result
    text = getattr(result, "text", "")
    return text if isinstance(text, str) else ""


def parse_json_text(text):
    """Best-effort JSON from a model reply, including a fenced code block.

    The text path mirrors the host's own structured parser: models often wrap
    JSON in ```json fences even when told not to.
    """
    import json
    body = (text or "").strip()
    if not body:
        return None
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    try:
        return json.loads(body.strip())
    except (ValueError, TypeError):
        return None


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

    def has_saved_settings(self) -> bool:
        missing = object()
        return any(self.ctx.get_config(key, missing) is not missing
                   for key in ("configuration", *dump_settings(Settings())))

    def credential_page_url(self) -> str:
        # Operator configuration, deliberately outside user-editable Settings.
        value = self.ctx.get_config("credential_page_url", "")
        if not isinstance(value, str):
            raise HostError("invalid_credential_url")
        return value

    def check_onboarding_compatibility(self) -> None:
        """Independent native-admission capability gate; no subprocess/network I/O."""
        try:
            from hermes_cli.plugins import PluginContext, VALID_HOOKS
            from gateway.platforms.event import MessageEvent
            from gateway import session_context
            from hermes_constants import get_hermes_home
            from agent.delegation_context import is_delegated_child_process_context
            if "post_gateway_admission" not in VALID_HOOKS:
                raise HostError()
            if not all(callable(getattr(PluginContext, name, None)) for name in
                       ("register_hook", "register_telegram_handler", "on_unload")):
                raise HostError()
            if not {"internal", "raw_message", "allow_gateway_control"} <= set(MessageEvent.__dataclass_fields__):
                raise HostError()
            if not all(hasattr(session_context, name) for name in
                       ("_SESSION_KEY", "_SESSION_MESSAGE_ID", "_SESSION_PLATFORM", "_SESSION_CHAT_TYPE", "_CRON_SESSION")):
                raise HostError()
            if not callable(get_hermes_home) or not callable(is_delegated_child_process_context):
                raise HostError()
        except Exception:
            raise HostError("incompatible_hermes_onboarding") from None

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
                return Store(connection, data / "run.lock",
                             migration_target=lambda: self.resolve_target(self.load_settings().target))
            except Exception:
                connection.close()
                raise
        except (StorageError, HostError):
            raise
        except Exception:
            raise HostError("storage_unavailable") from None

    def open_setup_store(self) -> "SetupStore":
        try:
            from plugins.plugin_storage import plugin_data_dir
            from .onboarding_store import SetupStore
            return SetupStore(plugin_data_dir(PLUGIN_ID) / "setup.db")
        except HostError:
            raise
        except Exception:
            raise HostError("setup_storage_unavailable") from None

    def complete_structured(self, payload: dict, *, timeout=45) -> ModelReply:
        """Ask the host for schema-shaped JSON, adapting to the installed API.

        Hermes is an open-source host; plugin revisions and host revisions
        diverge. Rather than demand one exact signature, use what this host
        offers, best first:

        1. ``complete_structured`` with only the keywords it accepts.
        2. ``complete`` (plain text) plus local JSON parsing — the plugin
           validates the schema itself, so structured support is a convenience,
           not a requirement.
        """
        llm = getattr(self.ctx, "llm", None)
        structured = getattr(llm, "complete_structured", None)
        complete = getattr(llm, "complete", None)
        try:
            if callable(structured):
                try:
                    return self._structured_reply(structured, payload, timeout)
                except Exception as exc:
                    if not _call_boundary_error(exc) or not callable(complete):
                        raise
                    # This host's structured signature differs; the text path
                    # can still answer, so fall through instead of failing.
            if callable(complete):
                return self._text_reply(complete, payload, timeout)
            raise HostError("incompatible_hermes")
        except (HostError, TriageError):
            raise
        except ValueError:
            raise TriageError("invalid_model_output") from None
        except Exception as exc:
            if _call_boundary_error(exc):
                # Neither method accepts our keywords: the installed host is
                # older or newer than this plugin. Not a model fault.
                raise HostError("incompatible_hermes") from None
            raise ModelUnavailable(model_failure(exc)) from None

    def _structured_reply(self, call, payload, timeout) -> ModelReply:
        kwargs = dict(payload)
        if _accepts(call, "timeout"):
            kwargs["timeout"] = timeout
        if _accepts(call, "max_tokens"):
            kwargs["max_tokens"] = 1800
        if _accepts(call, "purpose"):
            kwargs["purpose"] = "mail-notifier-triage"
        result: object = call(**kwargs)
        usage = getattr(result, "usage", None)
        values = {name: getattr(usage, name, None) for name in
                  ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")}
        return ModelReply(getattr(result, "parsed", None), values,
                          getattr(result, "provider", ""), getattr(result, "model", ""))

    def _text_reply(self, call, payload, timeout) -> ModelReply:
        instructions = str(payload.get("instructions", ""))
        schema = payload.get("json_schema")
        if schema is not None:
            import json
            instructions = f"{instructions}\n\nReturn one JSON object matching:\n{json.dumps(schema, ensure_ascii=False)}"
        parts = [{"type": "text", "text": instructions}]
        for block in payload.get("input") or ():
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append({"type": "text", "text": block["text"]})
        messages = [{"role": "user", "content": parts}]
        try:
            result = call(messages, timeout=timeout, max_tokens=1800, purpose="mail-notifier-triage")
        except TypeError as exc:
            if not _call_boundary_error(exc):
                raise
            result = call(messages)
        usage = getattr(result, "usage", None)
        values = {name: getattr(usage, name, None) for name in
                  ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")}
        parsed = parse_json_text(_completion_text(result))
        if parsed is None:
            raise TriageError("invalid_model_output")
        return ModelReply(parsed, values, getattr(result, "provider", ""), getattr(result, "model", ""))

    def resolve_target(self, target: str) -> str:
        Settings(target=target)
        if target != "telegram":
            return target
        try:
            from hermes_cli.send_cmd import _load_hermes_env
            from gateway.config import Platform, load_gateway_config
            _load_hermes_env()
            home = load_gateway_config().get_home_channel(Platform.TELEGRAM)
            if home is None:
                raise HostError("destination_unavailable")
            resolved = f"telegram:{home.chat_id}"
            if home.thread_id:
                resolved += f":{home.thread_id}"
            Settings(target=resolved)
            return resolved
        except Exception:
            raise HostError("destination_unavailable") from None

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
            # PM can attach Hermes and its dependencies outside sys.executable's
            # site-packages. Carry those import paths, not the ambient secrets.
            probe = (
                "import json,sys\n"
                "request=json.load(sys.stdin)\n"
                "sys.path[:]=request['sys_path']\n"
                "from hermes_cli.env_loader import load_hermes_dotenv\n"
                "from hermes_cli.send_cmd import _load_hermes_env\n"
                "from agent.secret_scope import get_secret\n"
                "expected=request['expected']\n"
                "load_hermes_dotenv()\n"
                "_load_hermes_env()\n"
                "print(json.dumps({'reloadable':all(get_secret(k)==v for k,v in expected.items())}))\n"
            )
            request = {"expected": expected, "sys_path": sys.path}
            result = subprocess.run([sys.executable, "-c", probe], input=json.dumps(request),
                                    env=self._child_env(), text=True, encoding="utf-8", capture_output=True,
                                    timeout=30, check=False)
            if result.returncode:
                raise HostError("credential_reload_check_failed")
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or not isinstance(response.get("reloadable"), bool):
                raise HostError("credential_reload_check_failed")
            if not response["reloadable"]:
                raise HostError("credentials_not_reloadable")
        except HostError:
            raise
        except Exception:
            raise HostError("credential_reload_check_failed") from None

    def send(self, target: str, text: str, *, timeout=30) -> dict:
        # Revalidate even when called outside the normal configuration path.
        Settings(target=target)
        try:
            result = subprocess.run(
                [self._executable(), "send", "--to", target, "--file", "-", "--json"],
                input=text, text=True, encoding="utf-8", capture_output=True,
                env=self._child_env(), timeout=timeout, check=False,
            )
            payload = json.loads(result.stdout)
            if result.returncode or not isinstance(payload, dict) or payload.get("success") is not True:
                raise DeliveryError(delivery_failure(payload.get("error") if isinstance(payload, dict) else ""))
            if payload.get("error") or payload.get("partial_success") or payload.get("skipped") or payload.get("delivered") is False:
                raise DeliveryError(delivery_failure(payload.get("error")))
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
        except subprocess.TimeoutExpired:
            raise DeliveryError("delivery_timeout") from None
        except Exception:
            raise DeliveryError() from None

    def validate_schedule(self, schedule: str) -> None:
        try:
            from cron.jobs import parse_schedule
            parse_schedule(schedule)
        except Exception:
            raise HostError("invalid_schedule") from None

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

    def _owned_schedule(self):
        script = self.home() / "scripts" / "hermes-mail-notifier.py"
        jobs = self.manage_cron("list", include_disabled=True).get("jobs")
        if not isinstance(jobs, list):
            raise HostError("invalid_cron_response")
        owned = [job for job in jobs if job.get("name") == PLUGIN_ID
                 and job.get("prompt_preview") == _MARKER
                 and job.get("script") and (script.parent / job["script"]).resolve() == script.resolve()]
        if len(owned) > 1:
            raise HostError("duplicate_owned_cron_jobs")
        return owned[0] if owned else None

    def resume_schedule(self, job_id: str, store: Store) -> None:
        existing = self._owned_schedule()
        if existing is None or existing.get("job_id") != job_id:
            raise HostError("cron_ownership_failed")
        self.manage_cron("resume", job_id=job_id)
        current = self._owned_schedule()
        if current is None or current.get("job_id") != job_id or current.get("enabled") is False or current.get("state") == "paused":
            raise HostError("cron_operation_failed")
        store.set_meta("cron_job_id", job_id)
        store.set_meta("cron_paused", False)

    def install_schedule(self, settings: Settings, store: Store, *, disable_only: bool = False) -> str:
        script = self.home() / "scripts" / "hermes-mail-notifier.py"
        existing = self._owned_schedule()
        if disable_only and any(account.enabled for account in settings.accounts):
            # Reducing enabled sources must not activate an absent job. No
            # schedule/destination change belongs to a disable-only snapshot.
            job_id = existing["job_id"] if existing else ""
            store.set_meta("cron_job_id", job_id or None)
            store.set_meta("cron_paused", bool(existing and (existing.get("enabled") is False or existing.get("state") == "paused")))
            return job_id
        if not any(account.enabled for account in settings.accounts):
            if existing:
                self.manage_cron("pause", job_id=existing["job_id"])
                store.set_meta("cron_job_id", existing["job_id"])
                store.set_meta("cron_paused", True)
                return existing["job_id"]
            store.set_meta("cron_job_id", None)
            store.set_meta("cron_paused", False)
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
        store.set_meta("cron_paused", bool(existing and (existing.get("enabled") is False or existing.get("state") == "paused")))
        return job_id

    def check_compatibility(self) -> None:
        try:
            for method in ("get_config", "set_config", "dispatch_tool"):
                if not callable(getattr(self.ctx, method, None)):
                    raise HostError()
            # Either completion method is enough; complete_structured is the
            # better one, but the text path parses and validates just as well.
            llm = getattr(self.ctx, "llm", None)
            if not any(callable(getattr(llm, name, None)) for name in ("complete_structured", "complete")):
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
