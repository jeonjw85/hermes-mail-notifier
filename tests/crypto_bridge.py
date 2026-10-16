"""JSON-line, test-only real crypto peer. Never print secrets or exception text."""

import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mail_notifier.credential_session import CredentialBinding, CredentialChallenge, CredentialSessions, credential_url
from mail_notifier.onboarding_auth import SetupOwner


def encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def compact(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def main():
    sessions = CredentialSessions(clock=lambda: 1000)
    owner = SetupOwner(str(Path.cwd() / ".synthetic-unused-profile"), "telegram", "10", "20", "20")
    binding = CredentialBinding(owner, "draft_1", 7)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["command"] == "issue":
                challenge = sessions.issue(binding)
                result = {"url": credential_url("https://example.com/credentials/v1/?theme=dark", challenge,
                           provider="gmail", account_label="개인 메일 & <work>", ui_language="ko")}
            elif request["command"] == "accept":
                accepted = sessions.accept(request["payload"], binding, update_key="synthetic:1")
                result = {"ok": accepted.password == request["expected"], "duplicate": accepted.duplicate}
            elif request["command"] == "vector":
                private = ec.generate_private_key(ec.SECP256R1())
                public = encode(private.public_key().public_bytes(
                    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
                session_id = encode(os.urandom(32))
                context = compact(["mail-notifier-credential", "1", session_id, "draft_1", "7", "10", "20", "20",
                                   "", "1600", public])
                challenge = CredentialChallenge(session_id, "1600", public, context)
                peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(request["client_public_key"]))
                key = HKDF(algorithm=hashes.SHA256(), length=32, salt=decode(session_id),
                           info=context.encode()).derive(private.exchange(ec.ECDH(), peer))
                nonce = os.urandom(12)
                plaintext = compact({"password": request["password"], "draft_id": "draft_1", "revision": 7}).encode()
                result = {"url": credential_url("https://example.com/credentials/v1/", challenge,
                          provider="gmail", account_label="test", ui_language="en"),
                          "nonce": encode(nonce), "ciphertext": encode(AESGCM(key).encrypt(nonce, plaintext, context.encode()))}
            else:
                raise ValueError()
        except Exception:
            result = {"ok": False}
        print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
