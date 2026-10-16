"""Process-local native input escrow. Only the admitted gateway hook grants access.

The private module slot is plugin-owned, not a Hermes singleton: profile loaders
give this file different module names. Its records deliberately use structural
fields rather than profile-local class identity.
"""

# Hermes modules are supplied by the host, not plugin runtime dependencies.
# pyright: reportMissingImports=false
import asyncio
import concurrent.futures
import contextvars
import json
import secrets
import sys
import threading
import time
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "mail-notifier-input:"
_SLOT = "_hermes_mail_notifier_onboarding_v1"
_OWNER_FIELDS = ("profile_home", "surface", "bot_id", "user_id", "chat_id", "thread_id")
_INPUT_FIELDS = ("kind", "bot_id", "user_id", "chat_id", "thread_id", "message_id", "update_key", "language_code", "value")


def _home(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("invalid_profile_home")
    return str(Path(value).resolve())


def owner_key(owner) -> tuple[str, str, str, str, str, str]:
    values = tuple(getattr(owner, name) for name in _OWNER_FIELDS)
    if any(not isinstance(value, str) for value in values) or values[1] not in ("telegram", "cli"):
        raise ValueError("invalid_setup_owner")
    return (_home(values[0]), *values[1:])


@dataclass(frozen=True)
class SetupOwner:
    profile_home: str
    surface: str
    bot_id: str = ""
    user_id: str = ""
    chat_id: str = ""
    thread_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "profile_home", owner_key(self)[0])


@dataclass(frozen=True)
class NativeInput:
    kind: str
    bot_id: str
    user_id: str
    chat_id: str
    thread_id: str
    message_id: str
    update_key: str
    language_code: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class AdmissionReceipt:
    owner: SetupOwner
    session_key: str
    message_id: str
    context: contextvars.Context = field(repr=False, compare=False, metadata={"serialize": False})

    def to_dict(self):
        return {"owner": dict(zip(_OWNER_FIELDS, owner_key(self.owner))),
                "session_key": self.session_key, "message_id": self.message_id}

    def __reduce_ex__(self, protocol):
        raise TypeError("admission_receipts_are_process_local")


def _shared():
    existing = sys.modules.get(_SLOT)
    if existing is not None:
        if getattr(existing, "ABI", None) != 1:
            raise RuntimeError("incompatible_onboarding_abi")
        return getattr(existing, "state")
    candidate = types.ModuleType(_SLOT)
    setattr(candidate, "ABI", 1)
    setattr(candidate, "state", types.SimpleNamespace(lock=threading.RLock(), records={}, leases=set(), origins={},
                                                     handoff=contextvars.ContextVar("mail_native_handoff", default=None),
                                                     observation=contextvars.ContextVar("mail_native_origin", default=None)))
    module = sys.modules.setdefault(_SLOT, candidate)
    if getattr(module, "ABI", None) != 1:
        raise RuntimeError("incompatible_onboarding_abi")
    return getattr(module, "state")


def _native(item):
    values = {name: getattr(item, name) for name in _INPUT_FIELDS}
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("invalid_native_input")
    for name in ("bot_id", "user_id", "chat_id", "message_id"):
        if not values[name].isascii() or not values[name].isdigit() or int(values[name]) <= 0:
            raise ValueError("invalid_native_identity")
    if values["user_id"] != values["chat_id"]:
        raise ValueError("private_chat_required")
    thread = values["thread_id"]
    if thread and (not thread.isascii() or not thread.isdigit() or int(thread) <= 0):
        raise ValueError("invalid_native_identity")
    if not values["kind"] or not values["update_key"] or len(values["update_key"]) > 200:
        raise ValueError("invalid_native_input")
    size = len(values["value"].encode("utf-8"))
    if values["kind"] == "reply":
        # Text edits have a separate bound. Account for JSON escaping, then
        # enforce the decoded UTF-8 limit; encrypted Web App wires stay 4096.
        if size > 6 * 16 * 1024 + 256:
            raise ValueError("native_input_too_large")
        try:
            reply = json.loads(values["value"])
        except (ValueError, RecursionError):
            raise ValueError("invalid_native_input") from None
        if (type(reply) is not dict or set(reply) != {"prompt_message_id", "text"}
                or not isinstance(reply["prompt_message_id"], str) or len(reply["prompt_message_id"]) > 200
                or not isinstance(reply["text"], str)):
            raise ValueError("invalid_native_input")
        if len(reply["text"].encode("utf-8")) > 16 * 1024:
            raise ValueError("native_input_too_large")
    elif size > 4096:
        raise ValueError("native_input_too_large")
    return NativeInput(**values)


def _matches(item, platform, source, message_id):
    return (platform == "telegram" and source.get("chat_type") == "dm" and not source.get("is_bot")
            and str(source.get("user_id") or "") == item.user_id
            and str(source.get("chat_id") or "") == item.chat_id
            and str(source.get("thread_id") or "") == item.thread_id
            and str(message_id or "") == item.message_id)


class AdmissionBroker:
    def __init__(self, profile_home: str):
        self.profile_home = _home(profile_home)
        self._state = _shared()
        self._lease = secrets.token_urlsafe(24)
        with self._state.lock:
            self._state.leases.add(self._lease)

    def _expire(self, now):
        for reference, record in list(self._state.records.items()):
            if now >= record["expires"]:
                self._discard(reference)
        for key, record in list(self._state.origins.items()):
            if now >= record["expires"]:
                del self._state.origins[key]

    def _discard(self, reference):
        record = self._state.records.pop(reference, None)
        if record is not None and not record["future"].done():
            record["future"].set_result(None)

    def prune(self):
        """Periodic gateway cleanup uses the same expiry policy as admission."""
        with self._state.lock:
            self._expire(time.monotonic())

    def close(self):
        """Release this profile registration's lease, never another profile's state."""
        with self._state.lock:
            self._state.leases.discard(self._lease)
            for reference, record in list(self._state.records.items()):
                if self._lease in (record["offered_by"], record.get("admitted_by")):
                    self._discard(reference)
            for key, record in list(self._state.origins.items()):
                if record["lease"] == self._lease:
                    del self._state.origins[key]

    def offer(self, item: NativeInput, *, now: float) -> str:
        item = _native(item)
        with self._state.lock:
            self._expire(now)
            if self._lease not in self._state.leases or len(self._state.records) >= 128:
                raise ValueError("native_mailbox_unavailable")
            if any((r["item"].bot_id, r["item"].update_key) == (item.bot_id, item.update_key)
                   for r in self._state.records.values()):
                raise ValueError("duplicate_native_input")
            reference = secrets.token_urlsafe(32)
            self._state.records[reference] = dict(item=item, expires=now + 30, offered_by=self._lease,
                                                  future=concurrent.futures.Future(), receipt=None)
            return reference

    @contextmanager
    def native_scope(self, reference):
        """Transport-only provenance inherited by handle_message's background task."""
        token = self._state.handoff.set(reference)
        try:
            yield
        finally:
            self._state.handoff.reset(token)

    async def wait(self, reference: str, *, timeout: float = 30) -> AdmissionReceipt | None:
        with self._state.lock:
            self._expire(time.monotonic())
            record = self._state.records.get(reference)
            if record is None or record["offered_by"] != self._lease:
                return None
            future = record["future"]
            timeout = min(timeout, max(0, record["expires"] - time.monotonic()))
        try:
            receipt = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout)
        except TimeoutError:
            with self._state.lock:
                self._discard(reference)
            return None
        except BaseException:
            with self._state.lock:
                self._discard(reference)
            raise
        with self._state.lock:
            self._expire(time.monotonic())
            if (receipt is not None and self._state.records.get(reference) is record
                    and self._lease in self._state.leases):
                return receipt
            self._discard(reference)
            return None

    def admit(self, *, platform: str, source: dict, message_id: str, session_key: str,
              text: str, now: float) -> AdmissionReceipt | None:
        if not text.startswith(PREFIX) or not session_key:
            return None
        reference = text[len(PREFIX):]
        with self._state.lock:
            self._expire(now)
            record = self._state.records.get(reference)
            if (self._lease not in self._state.leases or record is None or record["receipt"] is not None
                    or self._state.handoff.get() != reference):
                return None
            item = record["item"]
            if (not _matches(item, platform, source, message_id)
                    or str(source.get("scope_id") or "") != item.bot_id):
                return None
            receipt = AdmissionReceipt(SetupOwner(self.profile_home, "telegram", item.bot_id, item.user_id,
                                                  item.chat_id, item.thread_id), session_key, message_id,
                                       contextvars.copy_context())
            record.update(receipt=receipt, admitted_by=self._lease)
            record["future"].set_result(receipt)
            return receipt

    def take(self, reference: str, receipt: AdmissionReceipt) -> NativeInput:
        with self._state.lock:
            self._expire(time.monotonic())
            record = self._state.records.get(reference)
            if (record is None or record["offered_by"] != self._lease or record["receipt"] is not receipt
                    or owner_key(receipt.owner)[1:] != ("telegram", record["item"].bot_id,
                                                       record["item"].user_id, record["item"].chat_id,
                                                       record["item"].thread_id)):
                raise ValueError("invalid_admission_receipt")
            self._discard(reference)
            return _native(record["item"])

    def observe(self, item: NativeInput | None, *, now: float):
        """Bind SDK identities only, before core dispatch creates its background task.

        Never retain text, callback data, or an SDK object in an origin proof.
        An invalid observation clears any inherited identity in this native task.
        """
        observation = None
        if item is not None:
            item = _native(item)
            observation = (self._lease, now + 30, tuple(getattr(item, name) for name in _INPUT_FIELDS[:-1]))
        self._state.observation.set(observation)

    def record_origin(self, *, platform, source, message_id, session_key, now):
        with self._state.lock:
            self._expire(now)
            observation = self._state.observation.get()
            if (self._lease not in self._state.leases or not observation or not session_key
                    or observation[0] not in self._state.leases or now >= observation[1]):
                return None
            item = NativeInput(*observation[2], value="")
            if not _matches(item, platform, source, message_id):
                return None
            owner = SetupOwner(self.profile_home, "telegram", item.bot_id, item.user_id, item.chat_id, item.thread_id)
            receipt = AdmissionReceipt(owner, session_key, message_id, contextvars.copy_context())
            key = (owner_key(owner), session_key, message_id)
            if len(self._state.origins) >= 128 and key not in self._state.origins:
                del self._state.origins[next(iter(self._state.origins))]
            self._state.origins[key] = dict(receipt=receipt, expires=now + 300, lease=self._lease,
                                            observed_by=observation[0])
            return receipt


class OriginProofs:
    @staticmethod
    def language_code(receipt: AdmissionReceipt) -> str:
        """Display-only SDK locale from the same currently verified observation."""
        if OriginProofs.current() is not receipt:
            return ""
        observation = _shared().observation.get()
        return NativeInput(*observation[2], value="").language_code if observation else ""

    @staticmethod
    def current() -> AdmissionReceipt | None:
        """Require the host's *bound* task locals, never its legacy env fallback.

        These ContextVars and the delegation predicate are pinned host contracts.
        An unsupported host must fall back to the explicit /mail entry point.
        """
        try:
            from agent.delegation_context import is_delegated_child_process_context
            from gateway.session_context import (_SESSION_PLATFORM, _SESSION_KEY, _SESSION_MESSAGE_ID,
                                                 _SESSION_CHAT_ID, _SESSION_USER_ID, _SESSION_THREAD_ID,
                                                 _SESSION_CHAT_TYPE, _CRON_SESSION)
            from hermes_constants import get_hermes_home
        except ImportError:
            return None
        if (is_delegated_child_process_context() or _SESSION_PLATFORM.get() != "telegram"
                or _SESSION_CHAT_TYPE.get() != "dm" or _CRON_SESSION.get() != ""):
            return None
        home = str(get_hermes_home().resolve())
        session_key, message_id = _SESSION_KEY.get(), _SESSION_MESSAGE_ID.get()
        if not session_key or not message_id:
            return None
        state = _shared()
        with state.lock:
            observation = state.observation.get()
            if observation is None:
                return None
            item = NativeInput(*observation[2], value="")
            owner = owner_key(SetupOwner(home, "telegram", item.bot_id, item.user_id, item.chat_id, item.thread_id))
            key = (owner, session_key, message_id)
            record = state.origins.get(key)
            if record is not None and time.monotonic() >= record["expires"]:
                del state.origins[key]
                return None
            if (record is not None and item.message_id == message_id
                    and owner[3:] == (_SESSION_USER_ID.get(), _SESSION_CHAT_ID.get(), _SESSION_THREAD_ID.get())
                    and record["lease"] in state.leases and record["observed_by"] == observation[0]
                    and observation[0] in state.leases):
                return record["receipt"]
        return None


def acquire_broker() -> AdmissionBroker:
    from hermes_constants import get_hermes_home
    return AdmissionBroker(str(get_hermes_home()))
