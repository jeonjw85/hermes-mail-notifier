import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { webcrypto } from 'node:crypto';
import { createInterface } from 'node:readline';
import test from 'node:test';
import { encryptCredential, parseChallenge } from '../../web/credentials/v1/crypto.mjs';
import { messages } from '../../web/credentials/v1/messages.mjs';

const bytes = s => Buffer.from(s, 'base64url');
const encode = b => Buffer.from(b).toString('base64url');
const utf8 = s => new TextEncoder().encode(s);

async function withPeer(run) {
  const child = spawn(process.env.PYTHON || 'python', ['-m', 'tests.crypto_bridge'], { stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: child.stdout });
  const iterator = lines[Symbol.asyncIterator]();
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const exit = new Promise(resolve => child.on('close', resolve));
  const call = async request => {
    child.stdin.write(`${JSON.stringify(request)}\n`);
    const line = await iterator.next();
    assert.equal(line.done, false, 'Python peer exited without a response');
    return JSON.parse(line.value);
  };
  try { await run(call); }
  finally {
    child.stdin.end();
    assert.equal(await exit, 0);
    assert.equal(stderr, '');
    lines.close();
  }
}

function edit(hash, changes) {
  const outer = new URLSearchParams(hash.slice(1));
  const inner = new URLSearchParams(outer.get('mn'));
  for (const [key, value] of Object.entries(changes)) inner.set(key, value);
  outer.set('mn', inner.toString());
  return `#${outer}`;
}

test('exact Python URL → plugin mn parser → real Web Crypto → production Python accept, once', { timeout: 20000 }, async () => {
  await withPeer(async call => {
    const { url } = await call({ command: 'issue' });
    const link = new URL(url);
    assert.equal(link.search, '?theme=dark');
    assert.deepEqual([...new URLSearchParams(link.hash.slice(1)).keys()], ['mn']);
    const challenge = parseChallenge(`${link.hash}&tgWebAppVersion=9.1&tgWebAppPlatform=android&tgWebAppData=`);
    assert.equal(challenge.account_label, '개인 메일 & <work>');
    assert.equal(challenge.provider, 'gmail');
    const password = '  test-only 한글 😀  ';
    const payload = await encryptCredential(challenge, password, webcrypto);
    const wire = JSON.parse(payload);
    assert.deepEqual(Object.keys(wire).sort(), ['ciphertext', 'client_public_key', 'kind', 'nonce', 'session_id', 'v']);
    assert.equal(wire.kind, 'mail_notifier_credential');
    assert.equal(wire.v, 1);
    assert.equal(bytes(wire.client_public_key).length, 65);
    assert.equal(bytes(wire.nonce).length, 12);
    assert.ok(utf8(payload).length <= 4096);
    assert.ok(!payload.includes(password));
    assert.deepEqual(await call({ command: 'accept', payload, expected: password }), { ok: true, duplicate: false });
    assert.deepEqual(await call({ command: 'accept', payload, expected: password }), { ok: false, duplicate: true });
    const retry = await encryptCredential(challenge, password, webcrypto);
    const retryWire = JSON.parse(retry);
    for (const field of ['client_public_key', 'nonce', 'ciphertext']) assert.notEqual(retryWire[field], wire[field]);
    assert.deepEqual(await call({ command: 'accept', payload: retry, expected: password }), { ok: false, duplicate: true });
  });
});

test('Python cryptography → real Node decryption; altered GCM tag and AAD fail', { timeout: 20000 }, async () => {
  await withPeer(async call => {
    const pair = await webcrypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
    const client_public_key = encode(await webcrypto.subtle.exportKey('raw', pair.publicKey));
    const vector = await call({ command: 'vector', client_public_key, password: '  synthetic 한글 😀  ' });
    const challenge = parseChallenge(new URL(vector.url).hash);
    const peer = await webcrypto.subtle.importKey('raw', bytes(challenge.server_public_key), { name: 'ECDH', namedCurve: 'P-256' }, false, []);
    const shared = await webcrypto.subtle.deriveBits({ name: 'ECDH', public: peer }, pair.privateKey, 256);
    const material = await webcrypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
    const key = await webcrypto.subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: bytes(challenge.session_id), info: utf8(challenge.binding) }, material, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
    const algorithm = { name: 'AES-GCM', iv: bytes(vector.nonce), additionalData: utf8(challenge.binding), tagLength: 128 };
    const plaintext = await webcrypto.subtle.decrypt(algorithm, key, bytes(vector.ciphertext));
    assert.deepEqual(JSON.parse(new TextDecoder().decode(plaintext)), { password: '  synthetic 한글 😀  ', draft_id: 'draft_1', revision: 7 });
    const corrupt = bytes(vector.ciphertext);
    corrupt[corrupt.length - 1] ^= 1;
    await assert.rejects(webcrypto.subtle.decrypt(algorithm, key, corrupt));
    await assert.rejects(webcrypto.subtle.decrypt({ ...algorithm, additionalData: utf8('wrong binding') }, key, bytes(vector.ciphertext)));
  });
});

test('production Python rejects altered JS tag and binding without consuming the good session', { timeout: 20000 }, async () => {
  await withPeer(async call => {
    const { url } = await call({ command: 'issue' });
    const challenge = parseChallenge(new URL(url).hash);
    const payload = await encryptCredential(challenge, 'synthetic', webcrypto);
    const wire = JSON.parse(payload);
    const corrupt = bytes(wire.ciphertext);
    corrupt[corrupt.length - 1] ^= 1;
    assert.deepEqual(await call({ command: 'accept', payload: JSON.stringify({ ...wire, ciphertext: encode(corrupt) }), expected: 'synthetic' }), { ok: false });
    const binding = JSON.parse(challenge.binding);
    binding[5] = 'other-bot';
    const changed = parseChallenge(edit(new URL(url).hash, { binding: JSON.stringify(binding) }));
    const bad = await encryptCredential(changed, 'synthetic', webcrypto);
    assert.deepEqual(await call({ command: 'accept', payload: bad, expected: 'synthetic' }), { ok: false });
    assert.deepEqual(await call({ command: 'accept', payload, expected: 'synthetic' }), { ok: true, duplicate: false });
  });
});

test('challenge rejects ambiguous, noncanonical, malformed and mismatched protocol context', { timeout: 20000 }, async () => {
  await withPeer(async call => {
    const { url } = await call({ command: 'issue' });
    const hash = new URL(url).hash;
    const c = parseChallenge(hash);
    const outer = new URLSearchParams(hash.slice(1));
    const nested = outer.get('mn');
    const badBinding = JSON.parse(c.binding);
    badBinding[4] = '07';
    for (const bad of ['', '#v=1', '#mn=%', `${hash}&mn=${outer.get('mn')}`, `#mn=${encodeURIComponent(`${nested}&v=1`)}`,
      edit(hash, { extra: 'x' }), edit(hash, { v: '2' }), edit(hash, { session_id: `${c.session_id}=` }),
      edit(hash, { session_id: 'A'.repeat(43) }), edit(hash, { expires_at: '01600' }),
      edit(hash, { expires_at: '1601' }), edit(hash, { binding: JSON.stringify(badBinding) }),
      edit(hash, { binding: c.binding.replace(',', ', ') }), edit(hash, { binding: '{}' }),
      edit(hash, { server_public_key: encode(Buffer.alloc(65)) })]) {
      assert.throws(() => parseChallenge(bad), { message: 'invalid_challenge' });
    }
    const noVersion = new URLSearchParams(nested);
    noVersion.delete('v');
    assert.throws(() => parseChallenge(`#mn=${encodeURIComponent(noVersion)}`), { message: 'invalid_challenge' });
  });
});

test('UTF-8 limits, all Cc controls, lone surrogates, invalid curve and safe crypto failures', { timeout: 20000 }, async () => {
  await withPeer(async call => {
    for (const password of [' ', 'a'.repeat(1024), '"'.repeat(1024), '한'.repeat(341) + 'x', '😀'.repeat(256), 'e\u0301', '  a b  ']) {
      const { url } = await call({ command: 'issue' });
      const payload = await encryptCredential(parseChallenge(new URL(url).hash), password, webcrypto);
      assert.deepEqual(await call({ command: 'accept', payload, expected: password }), { ok: true, duplicate: false });
    }
    const { url } = await call({ command: 'issue' });
    const challenge = parseChallenge(new URL(url).hash);
    const controls = [...Array(32).keys(), ...Array.from({ length: 33 }, (_, i) => i + 127)];
    for (const password of ['', 'a'.repeat(1025), '한'.repeat(342), '\ud800', '\udfff', null, ...controls.map(c => `test${String.fromCharCode(c)}`)]) {
      await assert.rejects(encryptCredential(challenge, password, webcrypto), { message: 'invalid_password' });
    }
    await assert.rejects(encryptCredential(challenge, 'synthetic', undefined), { message: 'encryption_failed' });
    const invalid = encode(Buffer.concat([Buffer.from([4]), Buffer.alloc(64)]));
    const context = JSON.parse(challenge.binding);
    context[10] = invalid;
    const bad = parseChallenge(edit(new URL(url).hash, { server_public_key: invalid, binding: JSON.stringify(context) }));
    await assert.rejects(encryptCredential(bad, 'synthetic', webcrypto), { message: 'encryption_failed' });
  });
});

test('browser catalogs are exactly the paired Python credential subset', () => {
  const result = spawnSync(process.env.PYTHON || 'python', ['-c',
    'import json; from mail_notifier.i18n import MESSAGES; print(json.dumps({lang: {k: v for k, v in catalog.items() if k.startswith("credential.")} for lang, catalog in MESSAGES.items()}))'],
    { encoding: 'utf8', timeout: 20000 });
  assert.equal(result.status, 0, result.stderr);
  const catalogs = JSON.parse(result.stdout);
  assert.ok(Object.keys(catalogs.en).length > 10);
  assert.deepEqual(messages, catalogs);
  assert.deepEqual(Object.keys(messages.ko).sort(), Object.keys(messages.en).sort());
});
