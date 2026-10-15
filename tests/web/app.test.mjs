import assert from 'node:assert/strict';
import { webcrypto, generateKeyPairSync } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { mountCredentialForm } from '../../web/credentials/v1/app.mjs';
import { messages } from '../../web/credentials/v1/messages.mjs';

const publicKey = generateKeyPairSync('ec', { namedCurve: 'prime256v1' }).publicKey.export({ format: 'jwk' });
const raw = Buffer.concat([Buffer.from([4]), Buffer.from(publicKey.x, 'base64url'), Buffer.from(publicKey.y, 'base64url')]).toString('base64url');
function hash(language = 'en', expires = '1600') {
  const sid = Buffer.alloc(32, 12).toString('base64url');
  const fields = { v: '1', session_id: sid, expires_at: expires, provider: 'gmail', account_label: '<img src=x onerror=bad()>', ui_language: language, server_public_key: raw,
    binding: JSON.stringify(['mail-notifier-credential', '1', sid, 'draft_1', '7', '10', '20', '20', '', expires, raw]) };
  return `#${new URLSearchParams({ mn: new URLSearchParams(fields).toString() })}`;
}

class Element {
  constructor() { this.value = ''; this.disabled = false; this.textContent = ''; this.attributes = {}; this.listeners = new Map(); }
  set innerHTML(_) { assert.fail('external content must never reach innerHTML'); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  removeEventListener(type, listener) { if (this.listeners.get(type) === listener) this.listeners.delete(type); }
  async dispatch(type) { let prevented = false; await this.listeners.get(type)?.({ preventDefault() { prevented = true; } }); return prevented; }
  focus() { this.focused = true; }
}

function fixture(options = {}) {
  const ids = ['credential-form', 'password', 'submit', 'cancel', 'status', 'account', 'provider', 'expires', 'language',
    'title', 'intro', 'account-label', 'provider-label', 'expires-label', 'password-label', 'password-help', 'language-label', 'privacy'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const document = { documentElement: { lang: '' }, title: '', getElementById: id => elements[id] };
  const sent = [];
  const bridge = { platform: options.platform ?? 'android', initData: options.initData ?? '', sendData(payload) { sent.push(payload); }, close() { bridge.closed = true; } };
  Object.defineProperty(bridge, 'initDataUnsafe', { get() { assert.fail('display locale is not sender identity'); } });
  const now = { value: 1000000 };
  const mount = mountCredentialForm({ document, webApp: Object.hasOwn(options, 'webApp') ? options.webApp : bridge,
    cryptoApi: Object.hasOwn(options, 'cryptoApi') ? options.cryptoApi : webcrypto,
    location: { hash: options.hash ?? hash(options.language) }, now: () => now.value });
  return { ...elements, form: elements['credential-form'], document, bridge, sent, now, mount };
}

test('keyboard launch may have empty initData; masked accessible bilingual form uses textContent', () => {
  for (const language of ['ko', 'en']) {
    const f = fixture({ language });
    assert.equal(f.submit.disabled, false);
    assert.equal(f.document.documentElement.lang, language);
    assert.equal(f.title.textContent, messages[language]['credential.title']);
    assert.equal(f['password-label'].textContent, messages[language]['credential.password']);
    assert.equal(f.account.textContent, '<img src=x onerror=bad()>');
    assert.equal(f.provider.textContent, 'gmail');
    assert.match(f.expires.textContent, /1970/);
    f.mount.dispose();
  }
});

test('ordinary browser cannot submit through a script-only bridge; missing bridge/crypto also block', async () => {
  for (const options of [{ platform: 'unknown' }, { platform: '' }, { initData: 'query_id=inline-launch' }, { webApp: undefined }, { webApp: { platform: 'android' } }, { cryptoApi: undefined }, { cryptoApi: {} }]) {
    const f = fixture(options);
    assert.equal(f.submit.disabled, true);
    assert.equal(f.password.disabled, true);
    f.password.value = 'synthetic';
    assert.equal(await f.form.dispatch('submit'), true);
    assert.equal(f.sent.length, 0);
    assert.match(f.status.textContent, /Telegram|CLI/);
  }
});

test('identifiable non-keyboard launches block input, crypto and bridge submission', async t => {
  for (const initData of ['query_id=inline-launch', 'chat_type=private&chat_instance=direct-link',
    'chat_type=sender', 'chat_instance=direct-link', 'chat_type=', 'chat_instance=',
    'receiver=%7B%22id%22%3A20%7D', 'chat=%7B%22id%22%3A20%7D', 'start_param=attachment-link',
    'can_send_after=0', 'chat_join_request_query_id=join-request']) {
    await t.test(initData, async () => {
      const calls = [];
      const subtle = new Proxy(webcrypto.subtle, { get(target, key) {
        return (...args) => { calls.push(key); return target[key](...args); };
      } });
      const cryptoApi = { subtle, getRandomValues(...args) { calls.push('getRandomValues'); return webcrypto.getRandomValues(...args); } };
      const f = fixture({ initData, cryptoApi });
      // Dispatch even when disabled: the handler must also stay closed.
      f.password.value = 'synthetic';
      assert.equal(await f.form.dispatch('submit'), true);
      assert.equal(f.sent.length, 0);
      assert.deepEqual(calls, []);
      assert.equal(f.password.value, '');
      assert.equal(f.password.disabled, true);
      assert.equal(f.submit.disabled, true);
      assert.equal(f.status.textContent, messages.en['credential.unavailable']);
      f.mount.dispose();
    });
  }
});

test('empty keyboard initData remains usable for real encryption and one bridge submission', async () => {
  const f = fixture({ initData: '' });
  assert.equal(f.password.disabled, false);
  assert.equal(f.submit.disabled, false);
  f.password.value = 'synthetic';
  await f.form.dispatch('submit');
  assert.equal(f.password.value, '');
  assert.equal(f.sent.length, 1);
  assert.equal(JSON.parse(f.sent[0]).kind, 'mail_notifier_credential');
  assert.equal(f.status.textContent, messages.en['credential.submitted']);
  f.mount.dispose();
});

test('unsupported explicit locale falls back to English; selection updates labels without bridge identity', async () => {
  const f = fixture({ language: 'fr' });
  assert.equal(f.document.documentElement.lang, 'en');
  f.language.value = 'ko';
  await f.language.dispatch('change');
  assert.equal(f.document.documentElement.lang, 'ko');
  assert.equal(f.submit.textContent, messages.ko['credential.submit']);
});

test('malformed/version mismatch/expired challenges fail closed at mount and at submit', async () => {
  for (const fragment of ['', '#mn=bad', hash('ko', '1000'), '#mn=v%3D2']) {
    const f = fixture({ hash: fragment });
    assert.equal(f.submit.disabled, true);
    assert.equal(f.password.value, '');
  }
  const f = fixture();
  f.password.value = 'synthetic';
  f.now.value = 1600000;
  await f.form.dispatch('submit');
  assert.equal(f.sent.length, 0);
  assert.equal(f.password.value, '');
  assert.equal(f.status.textContent, messages.en['credential.expired']);
});

test('concurrent submission sends once, clears immediately and awaits bot confirmation, not success', async () => {
  const f = fixture();
  f.password.value = '  test-only 한글  ';
  const first = f.form.dispatch('submit');
  assert.equal(f.password.value, '');
  assert.equal(f.submit.disabled, true);
  await Promise.all([first, f.form.dispatch('submit'), f.form.dispatch('submit')]);
  assert.equal(f.sent.length, 1);
  assert.equal(JSON.parse(f.sent[0]).kind, 'mail_notifier_credential');
  assert.equal(f.status.textContent, messages.en['credential.submitted']);
  assert.match(f.status.textContent, /bot/);
  await f.form.dispatch('submit');
  assert.equal(f.sent.length, 1);
});

test('sendData exceptions are ambiguous: no retry or secret echo, clear input and ask bot for a new button', async () => {
  const f = fixture({ language: 'ko' });
  let attempts = 0;
  f.bridge.sendData = () => { attempts++; throw new Error('synthetic-secret-error'); };
  f.password.value = 'synthetic-secret-error';
  await f.form.dispatch('submit');
  await f.form.dispatch('submit');
  assert.equal(attempts, 1);
  assert.equal(f.password.value, '');
  assert.equal(f.submit.disabled, true);
  assert.equal(f.status.textContent, messages.ko['credential.send_failed']);
  assert.ok(!f.status.textContent.includes('synthetic-secret-error'));
});

test('cancel after a delivery attempt cannot falsely claim the submission was cancelled', async () => {
  for (const throws of [false, true]) {
    const f = fixture();
    if (throws) f.bridge.sendData = () => { throw new Error('ambiguous delivery'); };
    f.password.value = 'synthetic';
    await f.form.dispatch('submit');
    const status = f.status.textContent;
    assert.equal(f.cancel.disabled, true);
    await f.cancel.dispatch('click');
    assert.equal(f.status.textContent, status);
    assert.equal(f.bridge.closed, undefined);
  }
});

test('invalid password is cleared but can be reentered; crypto failure exposes no exception details', async () => {
  const f = fixture();
  f.password.value = 'bad\nsecret';
  await f.form.dispatch('submit');
  assert.equal(f.password.value, '');
  assert.equal(f.sent.length, 0);
  assert.equal(f.submit.disabled, false);
  assert.equal(f.status.textContent, messages.en['credential.invalid_password']);
  f.password.value = 'synthetic';
  await f.form.dispatch('submit');
  assert.equal(f.sent.length, 1);
  const broken = fixture({ cryptoApi: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto), subtle: { generateKey() { throw new Error('secret'); }, importKey() {}, deriveBits() {}, deriveKey() {}, encrypt() {}, exportKey() {} } } });
  broken.password.value = 'secret';
  await broken.form.dispatch('submit');
  assert.equal(broken.password.value, '');
  assert.equal(broken.sent.length, 0);
  assert.equal(broken.status.textContent, messages.en['credential.encryption_failed']);
});

function pausedCrypto() {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const subtle = new Proxy(webcrypto.subtle, { get(target, key) {
    if (key === 'generateKey') return async (...args) => { await gate; return target.generateKey(...args); };
    return typeof target[key] === 'function' ? target[key].bind(target) : target[key];
  } });
  return { cryptoApi: { subtle, getRandomValues: webcrypto.getRandomValues.bind(webcrypto) }, release: () => release() };
}

test('cancel/dispose during encryption prevents late send; expiry is rechecked after encryption', async () => {
  for (const action of ['cancel', 'dispose', 'expire']) {
    const paused = pausedCrypto();
    const f = fixture({ cryptoApi: paused.cryptoApi });
    f.password.value = 'synthetic';
    const pending = f.form.dispatch('submit');
    if (action === 'cancel') await f.cancel.dispatch('click');
    if (action === 'dispose') f.mount.dispose();
    if (action === 'expire') f.now.value = 1600000;
    paused.release();
    await pending;
    assert.equal(f.sent.length, 0);
    assert.equal(f.password.value, '');
    if (action === 'cancel') assert.equal(f.bridge.closed, true);
    if (action === 'dispose') assert.equal(f.form.listeners.size, 0);
  }
});

test('HTML security/accessibility contract: closed CSP, no HTTP form or inline scripts/events, masked input', async () => {
  const html = await readFile(new URL('../../web/credentials/v1/index.html', import.meta.url), 'utf8');
  const policy = html.match(/http-equiv="Content-Security-Policy" content="([^"]+)"/)[1];
  const directives = Object.fromEntries(policy.split(';').filter(s => s.trim()).map(s => { const [key, ...values] = s.trim().split(/\s+/); return [key, values]; }));
  for (const name of ['default-src', 'connect-src', 'form-action', 'base-uri']) assert.deepEqual(directives[name], ["'none'"]);
  assert.deepEqual(directives['script-src'], ["'self'", 'https://telegram.org']);
  assert.deepEqual(directives['style-src'], ["'self'"]);
  assert.match(html, /name="referrer" content="no-referrer"/);
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  assert.deepEqual(scripts.map(s => s[1].match(/src="([^"]+)"/)[1]), ['https://telegram.org/js/telegram-web-app.js', './app.mjs']);
  assert.ok(scripts.every(s => s[2].trim() === ''));
  assert.doesNotMatch(html, /\bon\w+\s*=|<form[^>]+action=|<iframe|<img|<style\b|style=/i);
  assert.match(html, /<input[^>]+id="password"[^>]+type="password"/);
  assert.match(html, /<label[^>]+for="password"/);
  assert.match(html, /aria-describedby="password-help"/);
  assert.match(html, /id="status"[^>]+role="status"[^>]+aria-live="polite"/);
  assert.match(html, /id="submit"[^>]+disabled/);
  assert.match(html, /id="cancel"[^>]+type="button"/);
});

test('production module execution never touches HTTP/storage/cookie/service worker/console sinks', async () => {
  // Trap real runtime sinks, including attempts swallowed by an error handler.
  const attempts = [];
  const forbidden = () => { attempts.push('forbidden sink'); throw new Error('forbidden sink'); };
  const names = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'localStorage', 'sessionStorage', 'indexedDB', 'caches', 'navigator', 'Worker', 'SharedWorker', 'BroadcastChannel'];
  const previous = names.map(name => Object.getOwnPropertyDescriptor(globalThis, name));
  const consoleDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'console');
  try {
    for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, get: forbidden });
    Object.defineProperty(globalThis, 'console', { configurable: true, value: new Proxy({}, { get: forbidden }) });
    const f = fixture();
    Object.defineProperty(f.document, 'cookie', { get: forbidden, set: forbidden });
    f.password.value = 'synthetic-sink-secret';
    await f.form.dispatch('submit');
    assert.equal(f.sent.length, 1);
    assert.ok(!f.sent[0].includes('synthetic-sink-secret'));
    f.mount.dispose();
    const bad = fixture();
    Object.defineProperty(bad.document, 'cookie', { get: forbidden, set: forbidden });
    bad.bridge.sendData = () => { throw new Error('synthetic-sink-secret'); };
    bad.password.value = 'synthetic-sink-secret';
    await bad.form.dispatch('submit');
    assert.equal(bad.status.textContent, messages.en['credential.send_failed']);
    const invalid = fixture();
    Object.defineProperty(invalid.document, 'cookie', { get: forbidden, set: forbidden });
    invalid.password.value = 'synthetic-sink-secret\n';
    await invalid.form.dispatch('submit');
    assert.equal(invalid.status.textContent, messages.en['credential.invalid_password']);
  } finally {
    names.forEach((name, index) => { if (previous[index]) Object.defineProperty(globalThis, name, previous[index]); else delete globalThis[name]; });
    Object.defineProperty(globalThis, 'console', consoleDescriptor);
  }
  assert.deepEqual(attempts, []);
});
