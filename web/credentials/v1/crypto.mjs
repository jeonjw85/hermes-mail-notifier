// Public challenge parsing and the v1 ECDH/HKDF/AES-GCM wire. No transport here.
const utf8 = new TextEncoder();
const fields = ['v', 'session_id', 'expires_at', 'provider', 'account_label', 'ui_language', 'server_public_key', 'binding'];

function encode(value) {
  return btoa(String.fromCharCode(...new Uint8Array(value))).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

function decode(value, size) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error();
  const bytes = Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - value.length % 4) % 4)), c => c.charCodeAt(0));
  if (bytes.length !== size || encode(bytes) !== value) throw new Error();
  return bytes;
}

function decimal(value) {
  return typeof value === 'string' && /^(0|[1-9][0-9]*)$/.test(value) && Number.isSafeInteger(Number(value));
}

function validate(challenge) {
  try {
    if (!challenge || Object.keys(challenge).length !== fields.length || fields.some(key => typeof challenge[key] !== 'string') || challenge.v !== '1') throw new Error();
    decode(challenge.session_id, 32);
    if (decode(challenge.server_public_key, 65)[0] !== 4 || !decimal(challenge.expires_at)) throw new Error();
    const binding = JSON.parse(challenge.binding);
    if (!Array.isArray(binding) || binding.length !== 11 || binding.some(item => typeof item !== 'string') ||
        JSON.stringify(binding) !== challenge.binding || binding[0] !== 'mail-notifier-credential' || binding[1] !== '1' ||
        binding[2] !== challenge.session_id || !/^[A-Za-z0-9_-]{1,128}$/.test(binding[3]) || !decimal(binding[4]) ||
        binding.slice(5, 9).some(item => !/^[\x21-\x7e]*$/.test(item)) ||
        binding[9] !== challenge.expires_at || binding[10] !== challenge.server_public_key) throw new Error();
    return binding;
  } catch {
    throw new Error('invalid_challenge');
  }
}

export function parseChallenge(hash) {
  try {
    if (typeof hash !== 'string' || !hash.startsWith('#') || hash.length > 16384) throw new Error();
    const outer = new URLSearchParams(hash.slice(1));
    // Telegram adds its own launch fields alongside, never inside, mn.
    if (outer.getAll('mn').length !== 1) throw new Error();
    const inner = new URLSearchParams(outer.get('mn'));
    const entries = [...inner];
    if (entries.length !== fields.length || fields.some(key => inner.getAll(key).length !== 1)) throw new Error();
    const challenge = Object.fromEntries(entries);
    validate(challenge);
    return Object.freeze(challenge);
  } catch {
    throw new Error('invalid_challenge');
  }
}

export async function encryptCredential(challenge, password, cryptoApi) {
  const binding = validate(challenge);
  // Reject unpaired UTF-16 surrogates instead of silently encoding U+FFFD.
  if (typeof password !== 'string' || /[\p{Cc}\p{Cs}]/u.test(password) || utf8.encode(password).length < 1 || utf8.encode(password).length > 1024) {
    throw new Error('invalid_password');
  }
  let plaintext;
  let shared;
  try {
    const subtle = cryptoApi.subtle;
    const pair = await subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
    const peer = await subtle.importKey('raw', decode(challenge.server_public_key, 65), { name: 'ECDH', namedCurve: 'P-256' }, false, []);
    shared = new Uint8Array(await subtle.deriveBits({ name: 'ECDH', public: peer }, pair.privateKey, 256));
    const material = await subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
    const context = utf8.encode(challenge.binding);
    const key = await subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: decode(challenge.session_id, 32), info: context }, material, { name: 'AES-GCM', length: 256 }, false, ['encrypt']);
    const nonce = cryptoApi.getRandomValues(new Uint8Array(12));
    plaintext = utf8.encode(JSON.stringify({ password, draft_id: binding[3], revision: Number(binding[4]) }));
    password = '';
    const ciphertext = await subtle.encrypt({ name: 'AES-GCM', iv: nonce, additionalData: context, tagLength: 128 }, key, plaintext);
    const wire = JSON.stringify({ kind: 'mail_notifier_credential', v: 1, session_id: challenge.session_id,
      client_public_key: encode(await subtle.exportKey('raw', pair.publicKey)), nonce: encode(nonce), ciphertext: encode(ciphertext) });
    if (utf8.encode(wire).length > 4096) throw new Error();
    return wire;
  } catch {
    throw new Error('encryption_failed');
  } finally {
    password = '';
    plaintext?.fill(0);
    shared?.fill(0);
    // Best effort only: JS strings and crypto implementation memory cannot be zeroized here.
  }
}
