"""Real cryptography, synthetic credentials, no host profiles or live services."""

import base64
import concurrent.futures
import json
import os
import shutil
import subprocess
import tempfile
import threading
import traceback
import unittest
import venv
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mail_notifier.hermes_host import HostError
from mail_notifier.onboarding_auth import SetupOwner
from tests.support import feature


def encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def compact(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def encrypt(challenge, plaintext, *, info=None, aad=None, private_key=None):
    key = private_key or ec.generate_private_key(ec.SECP256R1())
    server = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(challenge.server_public_key))
    binding = challenge.binding.encode("utf-8")
    aes_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=decode(challenge.session_id),
                   info=binding if info is None else info).derive(key.exchange(ec.ECDH(), server))
    nonce = os.urandom(12)
    return compact({"kind": "mail_notifier_credential", "v": 1, "session_id": challenge.session_id,
                    "client_public_key": encode(key.public_key().public_bytes(
                        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
                    "nonce": encode(nonce), "ciphertext": encode(AESGCM(aes_key).encrypt(
                        nonce, plaintext, binding if aad is None else aad))})


class CredentialSessionTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.credential_session")
        self.now = 1000.25
        self.sessions = self.module.CredentialSessions(clock=lambda: self.now)
        self.owner = SetupOwner(str(Path(tempfile.gettempdir()) / "synthetic-mail-profile"),
                                "telegram", "10", "20", "20")
        self.binding = self.module.CredentialBinding(self.owner, "draft_1", 7)

    def payload(self, challenge, password="  test-only password  ", **kwargs):
        return encrypt(challenge, compact({"password": password, "draft_id": "draft_1", "revision": 7}).encode(), **kwargs)

    def reject(self, payload, binding=None, *, update_key="700:1"):
        with self.assertRaises(HostError) as caught:
            self.sessions.accept(payload, binding or self.binding, update_key=update_key)
        self.assertEqual(caught.exception.code, "invalid_credential_submission")
        self.assertIsNone(caught.exception.__context__)

    def test_valid_submission_is_consumed_once(self):
        challenge = self.sessions.issue(self.binding)
        payload = self.payload(challenge)
        first = self.sessions.accept(payload, self.binding, update_key="700:1")
        again = self.sessions.accept(payload, self.binding, update_key="700:1")
        self.assertEqual(first.password, "  test-only password  ")
        self.assertFalse(first.duplicate)
        self.assertEqual((first.draft_id, first.revision), ("draft_1", 7))
        self.assertTrue(again.duplicate)
        self.assertIsNone(again.password)
        self.assertEqual(first.attempt_id, again.attempt_id)
        retry = self.sessions.accept(self.payload(challenge, "different test password"),
                                     self.binding, update_key="700:2")
        self.assertEqual(retry.attempt_id, first.attempt_id)
        self.assertTrue(retry.duplicate)
        self.assertIsNone(retry.password)

    def test_challenge_is_public_frozen_and_uses_exact_binding(self):
        challenge = self.sessions.issue(self.binding)
        self.assertEqual(len(decode(challenge.session_id)), 32)
        self.assertEqual(len(decode(challenge.server_public_key)), 65)
        self.assertEqual(decode(challenge.server_public_key)[0], 4)
        self.assertEqual(challenge.expires_at, "1600")
        self.assertEqual(challenge.binding, compact([
            "mail-notifier-credential", "1", challenge.session_id, "draft_1", "7", "10", "20", "20", "",
            "1600", challenge.server_public_key]))
        self.assertNotIn(self.owner.profile_home, repr(challenge))
        self.assertNotIn("private", repr(challenge))
        with self.assertRaises(FrozenInstanceError):
            self.binding.revision = 8
        with self.assertRaises(FrozenInstanceError):
            challenge.expires_at = "2000"
        other = self.module.CredentialSessions(clock=lambda: self.now).issue(self.binding)
        self.assertNotEqual(challenge.session_id, other.session_id)
        self.assertNotEqual(challenge.server_public_key, other.server_public_key)
        threaded = self.sessions.issue(replace(self.binding, owner=replace(self.owner, thread_id="42")))
        self.assertEqual(json.loads(threaded.binding)[8], "42")
        for changes in ({"draft_id": "한글"}, {"revision": True}, {"revision": "7"}, {"revision": -1},
                        {"owner": replace(self.owner, bot_id="봇")}):
            with self.subTest(changes=changes):
                with self.assertRaises(HostError) as caught:
                    replace(self.binding, **changes)
                self.assertEqual(caught.exception.code, "invalid_credential_binding")

    def test_expiry_boundary_prune_and_restart_require_new_challenge(self):
        challenge = self.sessions.issue(self.binding)
        payload = self.payload(challenge)
        self.now = 1599.999
        self.assertIsNotNone(self.sessions.accept(payload, self.binding, update_key="700:1").password)
        self.now = 1600
        self.reject(payload)
        self.sessions.prune()
        self.assertFalse(self.sessions._sessions)
        self.reject(payload)
        self.sessions = self.module.CredentialSessions(clock=lambda: self.now)
        self.reject(payload)

    def test_reissue_invalidate_and_other_owner_isolation(self):
        old = self.sessions.issue(self.binding)
        other_owner = replace(self.owner, user_id="30", chat_id="30")
        other_binding = replace(self.binding, owner=other_owner)
        other = self.sessions.issue(other_binding)
        fresh = self.sessions.issue(replace(self.binding, revision=8))
        self.reject(self.payload(old))
        self.sessions.invalidate("draft_1", self.owner)
        fresh_payload = encrypt(fresh, compact({"password": "test-only", "draft_id": "draft_1", "revision": 8}).encode())
        self.reject(fresh_payload, replace(self.binding, revision=8))
        self.assertIsNotNone(self.sessions.accept(self.payload(other), other_binding, update_key="700:1").password)

    def test_every_owner_component_and_revision_are_authoritative(self):
        for changes in ({"profile_home": self.owner.profile_home + "-other"}, {"surface": "cli"},
                        {"bot_id": "11"}, {"user_id": "21"}, {"chat_id": "21"}, {"thread_id": "1"}):
            with self.subTest(changes=changes):
                challenge = self.sessions.issue(self.binding)
                payload = self.payload(challenge)
                self.reject(payload, replace(self.binding, owner=replace(self.owner, **changes)))
                self.assertIsNotNone(self.sessions.accept(payload, self.binding, update_key="700:1").password)
        for changes in ({"revision": 8}, {"draft_id": "other"}):
            self.now += 60
            challenge = self.sessions.issue(self.binding)
            payload = self.payload(challenge)
            self.reject(payload, replace(self.binding, **changes))
            self.assertIsNotNone(self.sessions.accept(payload, self.binding, update_key="700:2").password)

    def test_concurrent_updates_and_distinct_ciphertexts_share_one_attempt(self):
        challenge = self.sessions.issue(self.binding)
        payloads = [self.payload(challenge) for _ in range(12)]
        gate = threading.Barrier(len(payloads))
        def accept(item):
            index, payload = item
            gate.wait(timeout=10)
            return self.sessions.accept(payload, self.binding, update_key=f"700:{index // 2}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            results = list(pool.map(accept, enumerate(payloads)))
        self.assertEqual(sum(not item.duplicate for item in results), 1)
        self.assertEqual(sum(item.password is not None for item in results), 1)
        self.assertEqual(len({item.attempt_id for item in results}), 1)

    def test_invalid_payload_does_not_consume_session_and_errors_never_echo_secrets(self):
        challenge = self.sessions.issue(self.binding)
        password = "test-only-error-secret"
        record = next(iter(self.sessions._sessions.values()))
        scalar = record.private_key.private_numbers().private_value
        private_markers = (str(scalar), hex(scalar)[2:], encode(scalar.to_bytes(32)))
        payload = encrypt(challenge, (compact({"password": password}) + " invalid").encode())
        try:
            self.sessions.accept(payload, self.binding, update_key="700:1")
        except HostError as exc:
            error = repr(exc) + str(exc) + traceback.format_exc()
            self.assertNotIn(password, error)
            for marker in private_markers:
                self.assertNotIn(marker, error)
            self.assertIsNone(exc.__context__)
        else:
            self.fail("invalid plaintext accepted")
        first = self.sessions.accept(self.payload(challenge, password), self.binding, update_key="700:2")
        self.assertNotIn(password, repr(first) + repr(self.sessions))
        self.assertNotIn(password, repr(vars(self.sessions)))
        self.assertNotIn(payload, repr(vars(self.sessions)))
        url = self.module.credential_url("https://example.com/input", challenge,
                                         provider="naver", account_label="test", ui_language="en")
        public = repr(challenge) + repr(first) + repr(record) + repr(vars(self.sessions)) + url
        self.assertNotIn(password, public)
        for marker in private_markers:
            self.assertNotIn(marker, public)
        self.assertEqual(first.password, password)
        self.reject(payload)

    def test_password_limits_are_utf8_bytes_and_never_trimmed(self):
        for password in (" ", "a" * 1024, "한" * 341 + "x", "e\u0301", "  a b  "):
            with self.subTest(length=len(password.encode())):
                challenge = self.sessions.issue(self.binding)
                self.assertEqual(self.sessions.accept(self.payload(challenge, password), self.binding,
                                                     update_key="700:1").password, password)
        challenge = self.sessions.issue(self.binding)
        unicode_password = "😀" * 256
        raw = json.dumps({"password": unicode_password, "draft_id": "draft_1", "revision": 7}, ensure_ascii=False).encode()
        self.assertEqual(self.sessions.accept(encrypt(challenge, raw), self.binding, update_key="700:1").password,
                         unicode_password)
        for password in ("", "a" * 1025, "한" * 342, None, 1, True, [], {}):
            with self.subTest(password=password):
                self.now += 60
                challenge = self.sessions.issue(self.binding)
                self.reject(self.payload(challenge, password))
                self.assertIsNotNone(self.sessions.accept(self.payload(challenge), self.binding, update_key="700:2").password)

    def test_all_unicode_cc_controls_are_rejected(self):
        for codepoint in (*range(32), *range(127, 160)):
            with self.subTest(codepoint=codepoint):
                self.now += 60
                challenge = self.sessions.issue(self.binding)
                self.reject(self.payload(challenge, "test" + chr(codepoint)))

    def test_plaintext_json_is_strict_including_duplicate_keys_and_revision_types(self):
        valid = {"password": "test-only", "draft_id": "draft_1", "revision": 7}
        bad = [b"\xff", b"[]", b"null", b"{}", compact({**valid, "extra": 1}).encode(),
               b'{"password":"a","password":"b","draft_id":"draft_1","revision":7}',
               b'{"password":"a","draft_id":"draft_1","revision":7,"revision":7}',
               b'{"password":"\\ud800","draft_id":"draft_1","revision":7}',
               compact({**valid, "draft_id": "other"}).encode()]
        bad.extend(compact({**valid, "revision": value}).encode() for value in (True, "7", 7.0, None, -1, 8, float("nan")))
        bad.extend(compact({k: v for k, v in valid.items() if k != field}).encode() for field in valid)
        for plaintext in bad:
            with self.subTest(plaintext=plaintext):
                self.now += 60
                challenge = self.sessions.issue(self.binding)
                self.reject(encrypt(challenge, plaintext))
                self.assertIsNotNone(self.sessions.accept(self.payload(challenge), self.binding, update_key="700:2").password)

    def test_wire_json_is_strict_and_limited_to_4096_utf8_bytes(self):
        bad = [lambda valid, value=value: value for value in ("", "[]", "null", "{}", "{" * 1100, "\ud800",
                                                              "[" * 1100 + "0" + "]" * 1100)]
        bad.extend((lambda valid: compact({**valid, "extra": 1}),
                    lambda valid: compact(valid)[:-1] + ',"v":1}',
                    lambda valid: compact(valid)[:-1] + ',"nonce":"x"}',
                    lambda valid: compact({**valid, "kind": "other"})))
        bad.extend(lambda valid, value=value: compact({**valid, "v": value})
                   for value in (0, 2, True, "1", 1.0, None, float("nan")))
        fields = ("kind", "v", "session_id", "client_public_key", "nonce", "ciphertext")
        bad.extend(lambda valid, field=field: compact({k: v for k, v in valid.items() if k != field}) for field in fields)
        for index, make_bad in enumerate(bad):
            with self.subTest(case=index):
                self.now += 60
                challenge = self.sessions.issue(self.binding)
                valid = json.loads(self.payload(challenge))
                self.reject(make_bad(valid))
                self.assertIsNotNone(self.sessions.accept(compact(valid), self.binding, update_key="700:2").password)
        self.now += 60
        challenge = self.sessions.issue(self.binding)
        valid = json.loads(self.payload(challenge))
        payload = compact(valid)
        # Leading JSON whitespace counts toward the UTF-8 wire limit, not characters.
        padded = " " * (4096 - len(payload.encode())) + payload
        self.reject(" " + padded)
        self.assertIsNotNone(self.sessions.accept(padded, self.binding, update_key="700:2").password)

    def test_binary_fields_require_canonical_unpadded_base64url_and_valid_p256(self):
        variants = []
        for field in ("session_id", "client_public_key", "nonce", "ciphertext"):
            for value in (None, 1, "", "!", "abc=", "a+b/", "a\nb", "한"):
                variants.append(lambda valid, field=field, value=value: {**valid, field: value})
            variants.append(lambda valid, field=field: {**valid, field: valid[field] + "="})
        variants.extend(lambda valid, value=value: {**valid, "client_public_key": encode(value)} for value in (
            b"\x04" + b"\x00" * 64, b"\x05" + b"\x00" * 64, b"\x02" + b"\x00" * 32,
            ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)))
        variants.extend(lambda valid, size=size: {**valid, "nonce": encode(b"x" * size)} for size in (11, 13))
        variants.extend(lambda valid, size=size: {**valid, "ciphertext": encode(b"x" * size)} for size in (0, 15, 16))
        # 32/65-byte values have non-zero unused pad bits in these spellings.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for field in ("session_id", "client_public_key"):
            variants.append(lambda valid, field=field: {**valid, field: valid[field][:-1]
                                                        + alphabet[alphabet.index(valid[field][-1]) + 1]})
        for index, make_bad in enumerate(variants):
            with self.subTest(case=index):
                self.now += 60
                challenge = self.sessions.issue(self.binding)
                valid = json.loads(self.payload(challenge))
                self.reject(compact(make_bad(valid)))
                self.assertIsNotNone(self.sessions.accept(compact(valid), self.binding, update_key="700:2").password)

    def test_tampering_wrong_hkdf_info_and_aad_are_rejected_without_consumption(self):
        challenge = self.sessions.issue(self.binding)
        valid = json.loads(self.payload(challenge))
        ciphertext = bytearray(decode(valid["ciphertext"]))
        ciphertext[-1] ^= 1
        for payload in (compact({**valid, "ciphertext": encode(ciphertext)}),
                        self.payload(challenge, info=b"other"), self.payload(challenge, aad=b"other")):
            self.reject(payload)
        self.assertIsNotNone(self.sessions.accept(compact(valid), self.binding, update_key="700:2").password)

    def test_invalid_attempts_are_rate_limited_per_user_for_60_seconds(self):
        challenge = self.sessions.issue(self.binding)
        for _ in range(5):
            self.reject("not json")
        # Reissue, another draft and another thread do not reset a user's budget.
        challenge = self.sessions.issue(self.binding)
        self.reject(self.payload(challenge))
        other_binding = replace(self.binding, owner=replace(self.owner, thread_id="1"))
        threaded = self.sessions.issue(other_binding)
        self.reject(self.payload(threaded), other_binding)
        other_draft = replace(self.binding, draft_id="draft_2")
        other_challenge = self.sessions.issue(other_draft)
        self.reject(encrypt(other_challenge, compact({"password": "test", "draft_id": "draft_2", "revision": 7}).encode()),
                    other_draft)
        other = replace(self.binding, owner=replace(self.owner, user_id="30", chat_id="30"))
        self.assertIsNotNone(self.sessions.accept(self.payload(self.sessions.issue(other)), other, update_key="700:1").password)
        self.now += 59.999
        self.sessions.prune()
        self.reject(self.payload(challenge))
        self.now += .001
        self.sessions.prune()
        self.assertIsNotNone(self.sessions.accept(self.payload(challenge), self.binding, update_key="700:1").password)
        # Invalid submissions using a victim's session ID charge the admitted
        # attacker, not the owner named by the payload.
        victim = replace(self.binding, owner=replace(self.owner, user_id="40", chat_id="40"))
        victim_challenge = self.sessions.issue(victim)
        for _ in range(5):
            self.reject(self.payload(victim_challenge))
        self.assertIsNotNone(self.sessions.accept(self.payload(victim_challenge), victim, update_key="700:1").password)

    def test_url_contains_public_context_only_and_rejects_unsafe_bases(self):
        challenge = self.sessions.issue(self.binding)
        url = self.module.credential_url("https://example.com/input?theme=dark", challenge,
                                         provider="naver", account_label="개인 메일 & work", ui_language="ko")
        parsed = urlsplit(url)
        self.assertEqual(parsed.query, "theme=dark")
        fragment = parse_qs(parsed.fragment)
        self.assertEqual(set(fragment), {"mn"})
        self.assertEqual(parse_qs(fragment["mn"][0]), {
            "v": ["1"], "session_id": [challenge.session_id], "expires_at": ["1600"],
            "provider": ["naver"], "account_label": ["개인 메일 & work"], "ui_language": ["ko"],
            "server_public_key": [challenge.server_public_key], "binding": [challenge.binding]})
        self.assertNotIn(self.owner.profile_home, url)
        self.assertNotIn("password", url)
        self.assertNotIn("private", url)
        for base in ("http://example.com", "//example.com", "javascript:alert(1)", "https://",
                     "https://user:secret@example.com", "https://example.com/#password=secret",
                     "https://example.com/#session_id=old", "https://example.com/#anything",
                     "https://example.com/\nsecret", " https://example.com", "https://example.com:bad"):
            with self.subTest(base=base):
                with self.assertRaises(HostError) as caught:
                    self.module.credential_url(base, challenge, provider="naver", account_label="test", ui_language="en")
                self.assertNotIn("secret", repr(caught.exception))

    @unittest.skipUnless(shutil.which("node"), "Node is a development/CI dependency for Web Crypto vectors")
    def test_node_webcrypto_encrypts_for_python_and_decrypts_python_vector(self):
        challenge = self.sessions.issue(self.binding)
        # Independent Web Crypto implementation of the approved wire operations.
        script = r"""
const {webcrypto: {subtle}} = require('node:crypto');
const b = s => Buffer.from(s, 'base64url');
const e = s => Buffer.from(s).toString('base64url');
const c = JSON.parse(process.argv[1]);
(async () => {
 const pair = await subtle.generateKey({name:'ECDH',namedCurve:'P-256'},true,['deriveBits']);
 const pub = await subtle.importKey('raw', b(c.server_public_key), {name:'ECDH',namedCurve:'P-256'},false,[]);
 const shared = await subtle.deriveBits({name:'ECDH',public:pub},pair.privateKey,256);
 const material = await subtle.importKey('raw',shared,'HKDF',false,['deriveKey']);
 const key = await subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:b(c.session_id),info:Buffer.from(c.binding)},
   material,{name:'AES-GCM',length:256},false,['encrypt','decrypt']);
 const nonce = Buffer.alloc(12, 17);
 const ciphertext = await subtle.encrypt({name:'AES-GCM',iv:nonce,additionalData:Buffer.from(c.binding),tagLength:128},
   key,Buffer.from(c.plaintext));
 console.log(JSON.stringify({kind:'mail_notifier_credential',v:1,session_id:c.session_id,
   client_public_key:e(await subtle.exportKey('raw',pair.publicKey)),nonce:e(nonce),ciphertext:e(ciphertext)}));
 if(c.vector) {
   const v=c.vector;
   const p=await subtle.importKey('jwk',v.private,{name:'ECDH',namedCurve:'P-256'},false,['deriveBits']);
   const remote=await subtle.importKey('raw',b(v.public),{name:'ECDH',namedCurve:'P-256'},false,[]);
   const bits=await subtle.deriveBits({name:'ECDH',public:remote},p,256);
   const m=await subtle.importKey('raw',bits,'HKDF',false,['deriveKey']);
   const k=await subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:b(c.session_id),info:Buffer.from(c.binding)},
     m,{name:'AES-GCM',length:256},false,['decrypt']);
   console.log(Buffer.from(await subtle.decrypt({name:'AES-GCM',iv:b(v.nonce),additionalData:Buffer.from(c.binding),
     tagLength:128},k,b(v.ciphertext))).toString('utf8'));
 }
})().catch(e=>{console.error(e.name);process.exitCode=1});
"""
        remote = ec.generate_private_key(ec.SECP256R1())
        public = encode(remote.public_key().public_bytes(serialization.Encoding.X962,
                                                        serialization.PublicFormat.UncompressedPoint))
        peer = ec.generate_private_key(ec.SECP256R1())
        plaintext = compact({"password": "  test-only 한글  ", "draft_id": "draft_1", "revision": 7})
        vector_challenge = replace(challenge, server_public_key=public)
        vector = json.loads(encrypt(vector_challenge, plaintext.encode(), private_key=peer))
        numbers = remote.private_numbers()
        vector["private"] = {"kty": "EC", "crv": "P-256", "x": encode(numbers.public_numbers.x.to_bytes(32)),
                             "y": encode(numbers.public_numbers.y.to_bytes(32)), "d": encode(numbers.private_value.to_bytes(32)),
                             "ext": True}
        vector["public"] = encode(peer.public_key().public_bytes(serialization.Encoding.X962,
                                                                serialization.PublicFormat.UncompressedPoint))
        context = {"server_public_key": challenge.server_public_key, "session_id": challenge.session_id,
                   "binding": challenge.binding, "plaintext": plaintext, "vector": vector}
        result = subprocess.run([shutil.which("node"), "-e", script, compact(context)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload, decrypted = result.stdout.splitlines()
        self.assertEqual(decrypted, plaintext)
        accepted = self.sessions.accept(payload, self.binding, update_key="700:1")
        self.assertEqual(accepted.password, "  test-only 한글  ")

    def test_without_crypto_separate_interpreter_fails_before_input_and_keeps_registration_status(self):
        # A bare interpreter, not a patched successful decryptor/import.
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            venv.EnvBuilder(with_pip=False).create(runtime)
            script = r'''
import argparse, importlib.util, json, sys
from pathlib import Path
def offline(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
        raise AssertionError('external effects forbidden')
sys.addaudithook(offline)
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from mail_notifier.credential_session import CredentialSessions, CredentialBinding
from mail_notifier.onboarding_auth import SetupOwner
from mail_notifier.hermes_host import HostError
assert not any(n.startswith(("cryptography", "telegram", "hermes_cli")) for n in sys.modules)
binding = CredentialBinding(SetupOwner(str(Path(sys.argv[2]) / "profile"), "telegram", "10", "20", "20"), "draft_1", 7)
sessions = CredentialSessions()
try:
    sessions.issue(binding)
except HostError as exc:
    assert exc.code == "credential_crypto_unavailable"
    assert exc.__context__ is None
else:
    raise AssertionError("missing crypto accepted")
try:
    sessions.accept('must not parse input without crypto', binding, update_key='700:1')
except HostError as exc:
    assert exc.code == "credential_crypto_unavailable"
    assert exc.__context__ is None
else:
    raise AssertionError("missing crypto received input")
spec = importlib.util.spec_from_file_location("synthetic_plugin", root / "__init__.py", submodule_search_locations=[str(root)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
tools, commands = {}, {}
class Context:
    def register_tool(self, **kw): tools[kw['name']] = kw['handler']
    def register_cli_command(self, **kw): commands[kw['name']] = kw
plugin.register(Context())
from tests.test_cli import SetupHost
plugin.HermesHost = lambda ctx: SetupHost(Path(sys.argv[2]) / "temporary-profile")
# SetupHost needs its temporary root, never an actual Hermes profile.
(Path(sys.argv[2]) / "temporary-profile").mkdir()
assert json.loads(tools['mail_notifier_status']({}))['success']
assert json.loads(tools['mail_notifier_check']({}))['success']
parser = argparse.ArgumentParser()
commands['mail-notifier']['setup_fn'](parser)
assert commands['mail-notifier']['handler_fn'](parser.parse_args(['status','--json'])) == 0
assert len(tools) == 7
print('lazy crypto: registration, check, status and CLI OK')
'''
            result = subprocess.run([str(runtime / "bin" / "python"), "-I", "-c", script,
                                     str(Path(__file__).resolve().parents[1]), directory],
                                    text=True, capture_output=True, timeout=30,
                                    env={"PATH": os.defpath, "HOME": directory,
                                         "HERMES_HOME": str(Path(directory) / "unused-profile")})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("lazy crypto: registration, check, status and CLI OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
