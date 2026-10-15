"""Short-lived, process-local credential transport, not sender authorization.

Callers supply the admitted owner and the current draft revision. No drafts,
passwords, ciphertexts or keys are persisted here; duplicate results carry no
password. Python/crypto memory is not guaranteed to be immediately zeroized.
"""

import base64
import json
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from .hermes_host import HostError
from .onboarding_auth import SetupOwner, owner_key

_WIRE_FIELDS = {"kind", "v", "session_id", "client_public_key", "nonce", "ciphertext"}


def _crypto():
    # Neither registration nor existing check/status paths require crypto/SDKs.
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        return ec, hashes, serialization, HKDF, AESGCM, InvalidTag
    except (ImportError, OSError):
        pass
    # Outside the handler: do not attach dependency exceptions as context.
    raise HostError("credential_crypto_unavailable")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value, size=None):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError()
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _encode(decoded) != value or (size is not None and len(decoded) != size):
        raise ValueError()
    return decoded


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(value):
    raise ValueError()


def _object(value, fields):
    result = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
    if type(result) is not dict or set(result) != fields:
        raise ValueError()
    return result


@dataclass(frozen=True)
class CredentialBinding:
    owner: SetupOwner
    draft_id: str
    revision: int

    def __post_init__(self):
        try:
            owner = SetupOwner(*owner_key(self.owner))
            if (not isinstance(self.draft_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.draft_id)
                    or type(self.revision) is not int or self.revision < 0
                    or any(not re.fullmatch(r"[\x21-\x7e]*", value)
                           for value in (owner.bot_id, owner.user_id, owner.chat_id, owner.thread_id))):
                raise ValueError()
            object.__setattr__(self, "owner", owner)
            return
        except (AttributeError, TypeError, ValueError):
            pass
        raise HostError("invalid_credential_binding")


@dataclass(frozen=True)
class CredentialChallenge:
    session_id: str
    expires_at: str
    server_public_key: str
    binding: str


@dataclass(frozen=True)
class CredentialInput:
    attempt_id: str
    draft_id: str
    revision: int
    duplicate: bool
    password: str | None = field(repr=False)


@dataclass
class _Session:
    binding: CredentialBinding
    challenge: CredentialChallenge
    expires: int
    private_key: Any = field(repr=False)
    attempt_id: str = ""


class CredentialSessions:
    def __init__(self, *, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._failures: dict[tuple, list[float]] = {}

    def _prune(self, now):
        for session_id, record in list(self._sessions.items()):
            if now >= record.expires:
                del self._sessions[session_id]
        for user, times in list(self._failures.items()):
            recent = [stamp for stamp in times if now < stamp + 60]
            if recent:
                self._failures[user] = recent
            else:
                del self._failures[user]

    def prune(self) -> None:
        with self._lock:
            self._prune(self._clock())

    def invalidate(self, draft_id: str, owner: SetupOwner) -> None:
        identity = owner_key(owner)
        with self._lock:
            for session_id, record in list(self._sessions.items()):
                if record.binding.draft_id == draft_id and owner_key(record.binding.owner) == identity:
                    del self._sessions[session_id]

    def issue(self, binding: CredentialBinding) -> CredentialChallenge:
        ec, _, serialization, _, _, _ = _crypto()
        binding = CredentialBinding(binding.owner, binding.draft_id, binding.revision)
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = _encode(private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        with self._lock:
            now = self._clock()
            self._prune(now)
            self.invalidate(binding.draft_id, binding.owner)
            session_id = _encode(secrets.token_bytes(32))
            expires = int(now) + 600
            owner = binding.owner
            context = json.dumps(["mail-notifier-credential", "1", session_id, binding.draft_id,
                                  str(binding.revision), owner.bot_id, owner.user_id, owner.chat_id,
                                  owner.thread_id, str(expires), public_key], ensure_ascii=True, separators=(",", ":"))
            challenge = CredentialChallenge(session_id, str(expires), public_key, context)
            self._sessions[session_id] = _Session(binding, challenge, expires, private_key)
            return challenge

    def _open(self, payload, binding, update_key, crypto):
        ec, hashes, _, HKDF, AESGCM, _ = crypto
        if (not isinstance(update_key, str) or not update_key or len(update_key) > 200
                or not isinstance(payload, str) or len(payload) > 4096 or len(payload.encode("utf-8")) > 4096):
            raise ValueError()
        wire = _object(payload, _WIRE_FIELDS)
        if wire["kind"] != "mail_notifier_credential" or type(wire["v"]) is not int or wire["v"] != 1:
            raise ValueError()
        salt = _decode(wire["session_id"], 32)
        record = self._sessions.get(wire["session_id"])
        if record is None or record.binding != binding:
            raise ValueError()
        public_key = _decode(wire["client_public_key"], 65)
        if public_key[0] != 4:
            raise ValueError()
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key)
        nonce = _decode(wire["nonce"], 12)
        ciphertext = _decode(wire["ciphertext"])
        if len(ciphertext) < 16:
            raise ValueError()
        context = record.challenge.binding.encode("utf-8")
        shared = record.private_key.exchange(ec.ECDH(), peer)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=context).derive(shared)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, context).decode("utf-8")
        data = _object(plaintext, {"password", "draft_id", "revision"})
        password = data["password"]
        if (data["draft_id"] != binding.draft_id or type(data["revision"]) is not int
                or data["revision"] != binding.revision or not isinstance(password, str)
                or not 1 <= len(password.encode("utf-8")) <= 1024
                or any(unicodedata.category(char) == "Cc" for char in password)
                or self._clock() >= record.expires):
            raise ValueError()
        return record, password

    def accept(self, payload: str, binding: CredentialBinding, *, update_key: str) -> CredentialInput:
        crypto = _crypto()
        # update_key is bounded transport metadata, never proof of identity.
        # The session, not a Telegram delivery ID, is the idempotency unit.
        user = owner_key(binding.owner)[:4]
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._failures.get(user, [])) >= 5:
                raise HostError("invalid_credential_submission")
            try:
                record, password = self._open(payload, binding, update_key, crypto)
            except (ValueError, TypeError, RecursionError, crypto[-1]):
                pass
            else:
                duplicate = bool(record.attempt_id)
                if not duplicate:
                    record.attempt_id = secrets.token_urlsafe(32)
                return CredentialInput(record.attempt_id, binding.draft_id, binding.revision,
                                       duplicate, None if duplicate else password)
            self._failures.setdefault(user, []).append(now)
            # No parser/Unicode/crypto exception context containing plaintext.
            raise HostError("invalid_credential_submission")


def credential_url(base_url: str, challenge: CredentialChallenge, *, provider: str,
                   account_label: str, ui_language: str) -> str:
    """Build public fragment context from a configured (not chat/model) base."""
    try:
        if not isinstance(base_url, str) or any(char.isspace() or unicodedata.category(char) == "Cc" for char in base_url):
            raise ValueError()
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "#" in base_url):
            raise ValueError()
        parsed.port  # Reject malformed/out-of-range configured ports.
        fragment = urlencode({"v": "1", "session_id": challenge.session_id, "expires_at": challenge.expires_at,
                              "provider": provider, "account_label": account_label, "ui_language": ui_language,
                              "server_public_key": challenge.server_public_key, "binding": challenge.binding})
        return urlunsplit(parsed._replace(fragment=fragment))
    except (ValueError, TypeError):
        pass
    raise HostError("invalid_credential_url")
