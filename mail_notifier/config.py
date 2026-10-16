"""Validated, non-secret account settings."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields

PLUGIN_ID = "hermes-mail-notifier"
PROVIDERS = {
    "naver": "imap.naver.com",
    "daum": "imap.daum.net",
    "gmail": "imap.gmail.com",
}
MAX_RAW_BYTES = 256 * 1024
MAX_BODY_CHARS = 12_000
MAX_MESSAGES = 20
MAX_RUN_SECONDS = 240
MAX_NOTIFICATION_UNITS = 3500


class ConfigError(ValueError):
    """An invalid configuration; messages never include supplied values."""


def _text(value, name, *, empty=False):
    if not isinstance(value, str) or "\x00" in value:
        raise ConfigError(f"{name} must be text")
    if not empty and not value.strip():
        raise ConfigError(f"{name} must not be empty")
    return value


def secret_name(account_id: str) -> str:
    if not isinstance(account_id, str) or not re.fullmatch(r"[a-z0-9]{1,64}", account_id):
        raise ConfigError("account id must contain 1–64 lowercase letters or digits")
    return f"HERMES_MAIL_{account_id.upper()}_APP_PASSWORD"


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    provider: str
    username: str
    password_secret: str
    mailbox: str = "INBOX"
    enabled: bool = True

    def __post_init__(self):
        if self.password_secret != secret_name(self.id):
            raise ConfigError("password_secret must match the account's own secret name")
        if not isinstance(self.provider, str) or self.provider not in PROVIDERS:
            raise ConfigError("provider must be naver, daum, or gmail")
        for name in ("label", "username", "mailbox"):
            value = _text(getattr(self, name), name)
            if any(ord(char) < 32 for char in value):
                raise ConfigError(f"{name} must not contain control characters")
        if type(self.enabled) is not bool:
            raise ConfigError("enabled must be a boolean")


@dataclass(frozen=True)
class Settings:
    accounts: tuple[Account, ...] = ()
    schedule: str = "every 5m"
    target: str = "telegram"
    language: str = "ko"
    important_senders: tuple[str, ...] = ()
    ignored_senders: tuple[str, ...] = ()
    important_keywords: tuple[str, ...] = ()
    importance_instructions: str = ""
    schema_version: int = 2
    always_notify_senders: tuple[str, ...] = ()
    max_run_seconds: int = 240
    max_fetches: int = 20
    max_model_calls: int = 20
    max_deliveries: int = 20
    max_pending: int = 2000
    max_cached_bodies: int = 200
    ui_language: str = ""

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ConfigError("unsupported settings version")
        limits = {"max_run_seconds": (30, 240), "max_fetches": (1, 100),
                  "max_model_calls": (1, 100), "max_deliveries": (1, 100),
                  "max_pending": (20, 10000), "max_cached_bodies": (1, 1000)}
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"{name} must be an integer between {low} and {high}")
        if self.max_cached_bodies > self.max_pending:
            raise ConfigError("body cache limit must not exceed queue limit")
        if not isinstance(self.accounts, tuple) or not all(isinstance(a, Account) for a in self.accounts):
            raise ConfigError("accounts must be a sequence of accounts")
        if len({a.id for a in self.accounts}) != len(self.accounts):
            raise ConfigError("account ids must be unique")
        for name in ("schedule", "target", "language"):
            value = _text(getattr(self, name), name)
            if any(ord(char) < 32 for char in value):
                raise ConfigError(f"{name} must not contain control characters")
        if not isinstance(self.ui_language, str) or self.ui_language not in ("", "ko", "en"):
            raise ConfigError("ui_language must be empty, ko, or en")
        if not re.fullmatch(r"telegram(?::-?[1-9]\d*(?::[1-9]\d*)?)?", self.target):
            raise ConfigError("target must be telegram or telegram:chat_id[:thread_id]")
        for name in ("important_senders", "ignored_senders", "important_keywords", "always_notify_senders"):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ConfigError(f"{name} must be a list of text values")
            for value in values:
                _text(value, name)
                if name.endswith("senders") and not re.fullmatch(r"(?:[^@\s<>]+)?@[^@\s<>]+", value):
                    raise ConfigError(f"{name} requires email addresses or @domains")
        _text(self.importance_instructions, "importance_instructions", empty=True)


def load_settings(raw: dict) -> Settings:
    if not isinstance(raw, dict):
        raise ConfigError("settings must be an object")
    allowed = {field.name for field in fields(Settings)}
    if set(raw) - allowed:
        raise ConfigError("unrecognized settings field")
    data = dict(raw)
    accounts = data.get("accounts", [])
    if not isinstance(accounts, (list, tuple)):
        raise ConfigError("accounts must be a list")
    parsed = []
    for account in accounts:
        if not isinstance(account, dict):
            raise ConfigError("each account must be an object")
        try:
            parsed.append(Account(**account))
        except TypeError:
            raise ConfigError("account fields are missing or unrecognized") from None
    data["accounts"] = tuple(parsed)
    for name in ("important_senders", "ignored_senders", "important_keywords", "always_notify_senders"):
        values = data.get(name, [])
        if not isinstance(values, (list, tuple)):
            raise ConfigError(f"{name} must be a list")
        data[name] = tuple(values)
    return Settings(**data)


def dump_settings(settings: Settings) -> dict:
    data = asdict(settings)
    for name in ("accounts", "important_senders", "ignored_senders", "important_keywords", "always_notify_senders"):
        data[name] = list(data[name])
    return data


def settings_fingerprint(settings: Settings) -> str:
    encoded = json.dumps(dump_settings(settings), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
