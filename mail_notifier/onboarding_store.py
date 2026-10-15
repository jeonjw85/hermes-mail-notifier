"""Profile-local, non-secret setup drafts; each worker owns its connection."""

import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import NoReturn

from .config import Settings, dump_settings, load_settings, settings_fingerprint
from .hermes_host import HostError
from .onboarding_auth import SetupOwner, owner_key

_TTL = 24 * 60 * 60
_TERMINAL = ("completed", "cancelled", "expired", "acknowledged")
_STATES = {"draft", "awaiting_secret", "validating", "awaiting_confirmation", "committing", "scheduled",
           "completed", "cancelled", "expired", "acknowledged", "needs_secret", "needs_review", "repair_required"}
_ACTIVE = "state NOT IN ('completed','cancelled','expired','acknowledged')"
_OWNER_COLUMNS = "profile_home,surface,bot_id,user_id,chat_id,thread_id"
_OWNER_WHERE = "profile_home=? AND surface=? AND bot_id=? AND user_id=? AND chat_id=? AND thread_id=?"
_FORM_FIELDS = {"action", "account_id", "provider", "username", "label", "mailbox", "input_field", "prompt_message_id"}


def _invalid() -> NoReturn:
    raise HostError("invalid_setup_draft")


def _object(value, allowed):
    if type(value) is not dict or set(value) - allowed:
        _invalid()
    return value


def _matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        _invalid()


def _form(value):
    result = dict(_object(value, _FORM_FIELDS))
    if any(not isinstance(text, str) or "\x00" in text for text in result.values()):
        _invalid()
    if result.get("input_field", "") not in (_FORM_FIELDS - {"input_field", "prompt_message_id"}) | {
            "", "important_senders", "ignored_senders", "always_notify_senders", "important_keywords", "importance_instructions"}:
        _invalid()
    return result


def _steps(value):
    """Allow only recovery markers, never arbitrary external responses or errors."""
    _object(value, {"credentials", "baselines", "settings_fingerprint", "cron", "test_delivery", "error_code", "acknowledgement"})
    result = {}
    if "acknowledgement" in value:
        entry = _object(value["acknowledgement"], {"reason", "settings_fingerprint"})
        if (entry.get("reason") not in ("paused", "conflict")
                or not _matches(entry.get("settings_fingerprint"), r"[a-f0-9]{64}")):
            _invalid()
        result["acknowledgement"] = dict(entry)
    for name in ("credentials", "baselines"):
        if name not in value:
            continue
        accounts = value[name]
        if type(accounts) is not dict:
            _invalid()
        result[name] = {}
        for account_id, entry in accounts.items():
            if not _matches(account_id, r"[a-z0-9]{1,64}"):
                _invalid()
            if name == "credentials":
                _object(entry, {"write_started", "verified"})
                if any(type(flag) is not bool for flag in entry.values()):
                    _invalid()
            else:
                _object(entry, {"uidvalidity", "last_uid"})
                if any(type(number) is not int or number < (1 if key == "uidvalidity" else 0)
                       for key, number in entry.items()):
                    _invalid()
            result[name][account_id] = dict(entry)
    for name, allowed in (("cron", {"job_id", "paused"}), ("test_delivery", {"started", "message_id"})):
        if name not in value:
            continue
        entry = _object(value[name], allowed)
        for key, item in entry.items():
            if key in ("paused", "started"):
                if type(item) is not bool:
                    _invalid()
            elif not _matches(item, r"[A-Za-z0-9_.:-]{1,200}"):
                _invalid()
        result[name] = dict(entry)
    for name, pattern in (("settings_fingerprint", r"[a-f0-9]{64}"), ("error_code", r"[a-z][a-z0-9_]{0,79}")):
        if name in value:
            if not _matches(value[name], pattern):
                _invalid()
            result[name] = value[name]
    return result


@dataclass(frozen=True)
class SetupDraft:
    id: str
    owner: SetupOwner
    revision: int
    state: str
    original: Settings
    candidate: Settings
    original_saved: bool
    updated_at: float
    form: dict = field(default_factory=dict, repr=False)
    steps: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if (not _matches(self.id, r"[A-Za-z0-9_-]{1,128}") or type(self.revision) is not int or self.revision < 0
                or not isinstance(self.state, str) or self.state not in _STATES or type(self.original_saved) is not bool):
            _invalid()
        _timestamp(self.updated_at)
        try:
            object.__setattr__(self, "owner", SetupOwner(*owner_key(self.owner)))
            for name in ("original", "candidate"):
                object.__setattr__(self, name, load_settings(dump_settings(getattr(self, name))))
        except (AttributeError, TypeError, ValueError):
            _invalid()
        object.__setattr__(self, "form", _form(self.form))
        object.__setattr__(self, "steps", _steps(self.steps))

    def to_dict(self) -> dict:
        # Revalidate mutable nested dictionaries before any serialization.
        return asdict(replace(self))


def _draft(row) -> SetupDraft:
    try:
        return SetupDraft(row["id"], SetupOwner(*(row[key] for key in _OWNER_COLUMNS.split(","))),
                          row["revision"], row["state"], load_settings(json.loads(row["original_json"])),
                          load_settings(json.loads(row["candidate_json"])), bool(row["original_saved"]),
                          row["updated_at"], json.loads(row["form_json"]), json.loads(row["steps_json"]))
    except (TypeError, ValueError, KeyError):
        _invalid()


class SetupStore:
    def __init__(self, path: Path):
        path = Path(path)
        connection = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            # Keep SQLite's thread check: runtime workers must open their own store.
            connection = sqlite3.connect(path, isolation_level=None)
            self.db = connection
            self.db.row_factory = sqlite3.Row
            with self._transaction():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise HostError("unsupported_setup_schema")
                if version == 0:
                    if self.db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                        raise HostError("unsupported_setup_schema")
                    self.db.execute("""CREATE TABLE drafts(
                        id TEXT PRIMARY KEY, profile_home TEXT NOT NULL, surface TEXT NOT NULL,
                        bot_id TEXT NOT NULL, user_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                        revision INTEGER NOT NULL, state TEXT NOT NULL, original_json TEXT NOT NULL,
                        candidate_json TEXT NOT NULL, original_saved INTEGER NOT NULL, updated_at REAL NOT NULL,
                        form_json TEXT NOT NULL, steps_json TEXT NOT NULL, original_fingerprint TEXT NOT NULL)""")
                if version < 2:
                    # v2 only changes the terminal-state index; snapshots and
                    # recovery evidence in existing v1 records are untouched.
                    self.db.execute("DROP INDEX IF EXISTS one_active_owner")
                    self.db.execute(f"""CREATE UNIQUE INDEX one_active_owner
                        ON drafts(profile_home,surface,bot_id,user_id,chat_id) WHERE {_ACTIVE}""")
                    self.db.execute("PRAGMA user_version=2")
        except Exception as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, HostError):
                raise
            raise HostError("setup_storage_unavailable") from None

    @contextmanager
    def _transaction(self):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            if isinstance(exc, sqlite3.Error):
                raise HostError("setup_storage_unavailable") from None
            raise

    def create(self, owner: SetupOwner, settings: Settings, *, original_saved: bool, now: float) -> SetupDraft:
        draft = SetupDraft(secrets.token_urlsafe(24), owner, 0, "draft", settings, settings, original_saved, now)
        self.prune(now=now)
        with self._transaction():
            key = owner_key(draft.owner)
            row = self.db.execute(f"""SELECT * FROM drafts WHERE profile_home=? AND surface=? AND bot_id=?
                AND user_id=? AND chat_id=? AND {_ACTIVE}""", key[:5]).fetchone()
            if row is not None:
                existing = _draft(row)
                if owner_key(existing.owner) != key:
                    raise HostError("setup_changed_retry")
                return existing
            count = self.db.execute(f"SELECT count(*) FROM drafts WHERE profile_home=? AND {_ACTIVE}", key[:1]).fetchone()[0]
            if count >= 50:
                raise HostError("setup_limit_reached")
            self.db.execute(f"""INSERT INTO drafts(id,{_OWNER_COLUMNS},revision,state,original_json,candidate_json,
                original_saved,updated_at,form_json,steps_json,original_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (draft.id, *key, 0, "draft", json.dumps(dump_settings(draft.original)), json.dumps(dump_settings(draft.candidate)),
                 int(original_saved), now, "{}", "{}", settings_fingerprint(draft.original)))
        return draft

    def get(self, draft_id: str, owner: SetupOwner) -> SetupDraft:
        row = self.db.execute(f"SELECT * FROM drafts WHERE id=? AND {_OWNER_WHERE}", (draft_id, *owner_key(owner))).fetchone()
        if row is None:
            raise HostError("setup_not_found")
        return _draft(row)

    def cas(self, draft: SetupDraft, *, expected_revision: int, now: float) -> SetupDraft:
        draft = replace(draft)  # Validate/copy even if a caller mutated nested data.
        _timestamp(now)
        self.prune(now=now)
        with self._transaction():
            current = self.get(draft.id, draft.owner)
            if (type(expected_revision) is not int or current.revision != expected_revision
                    or draft.revision != expected_revision or current.state in _TERMINAL
                    or current.original != draft.original or current.original_saved != draft.original_saved
                    or now < current.updated_at):
                raise HostError("setup_changed_retry")
            changed = replace(draft, revision=expected_revision + 1, updated_at=now,
                              form={} if draft.state in _TERMINAL else draft.form)
            result = self.db.execute(f"""UPDATE drafts SET revision=?,state=?,candidate_json=?,updated_at=?,form_json=?,steps_json=?
                WHERE id=? AND {_OWNER_WHERE} AND revision=? AND {_ACTIVE}""",
                (changed.revision, changed.state, json.dumps(dump_settings(changed.candidate)), now,
                 json.dumps(changed.form), json.dumps(changed.steps), changed.id, *owner_key(changed.owner), expected_revision))
            if result.rowcount != 1:
                raise HostError("setup_changed_retry")
        return changed

    def prune(self, *, now: float) -> None:
        _timestamp(now)
        with self._transaction():
            # Date expiry from the deadline, not the cleanup run, so a late run
            # cannot extend the retention period of an abandoned attempt.
            self.db.execute(f"""UPDATE drafts SET state='expired',revision=revision+1,updated_at=updated_at+?,form_json='{{}}'
                WHERE {_ACTIVE} AND updated_at<=?""", (_TTL, now - _TTL))
            self.db.execute(f"DELETE FROM drafts WHERE NOT ({_ACTIVE}) AND updated_at<=?", (now - _TTL,))

    def close(self) -> None:
        self.db.close()
