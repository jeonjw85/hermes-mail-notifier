import { encryptCredential, parseChallenge } from './crypto.mjs';
import { messages } from './messages.mjs';

export function mountCredentialForm({ document, webApp, cryptoApi, location, now = Date.now }) {
  const element = id => document.getElementById(id);
  const form = element('credential-form');
  const input = element('password');
  const submit = element('submit');
  const cancel = element('cancel');
  const language = element('language');
  let challenge;
  let locale = 'en';
  let state = 'ready';
  let locked = false;
  let disposed = false;
  try {
    challenge = parseChallenge(location.hash);
    locale = challenge.ui_language.toLowerCase().split(/[-_.@]/)[0] === 'ko' ? 'ko' : 'en';
    element('account').textContent = challenge.account_label;
    element('provider').textContent = challenge.provider;
    element('expires').textContent = new Date(Number(challenge.expires_at) * 1000).toISOString();
    if (now() >= Number(challenge.expires_at) * 1000) state = 'expired';
  } catch {
    state = 'invalid_challenge';
  }

  // This is a usability/context gate, NOT sender authentication. The bot must
  // authenticate the received event. KeyboardButton initData may be empty.
  const supportedPlatforms = ['android', 'android_x', 'ios', 'macos', 'tdesktop', 'web', 'weba', 'webk', 'unigram'];
  const bridgeAvailable = webApp && supportedPlatforms.includes(webApp.platform) && typeof webApp.sendData === 'function' &&
    !new URLSearchParams(webApp.initData || '').has('query_id');
  const cryptoAvailable = cryptoApi && typeof cryptoApi.getRandomValues === 'function' &&
    ['generateKey', 'importKey', 'deriveBits', 'deriveKey', 'encrypt', 'exportKey'].every(method => typeof cryptoApi.subtle?.[method] === 'function');
  if (state === 'ready' && (!bridgeAvailable || !cryptoAvailable)) state = 'unavailable';
  locked = state !== 'ready';

  function render() {
    const catalog = messages[locale];
    const labels = { title: 'title', intro: 'intro', 'account-label': 'account', 'provider-label': 'provider',
      'expires-label': 'expires', 'password-label': 'password', 'password-help': 'password_help',
      'language-label': 'language', submit: 'submit', cancel: 'cancel', privacy: 'privacy' };
    for (const [id, key] of Object.entries(labels)) element(id).textContent = catalog[`credential.${key}`];
    document.documentElement.lang = locale;
    document.title = catalog['credential.title'];
    language.value = locale;
    element('status').textContent = catalog[`credential.${state}`];
    input.disabled = locked;
    submit.disabled = locked;
    cancel.disabled = state === 'submitted' || state === 'send_failed' || state === 'cancelled';
    form.setAttribute('aria-busy', state === 'encrypting' ? 'true' : 'false');
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (locked || disposed) { input.value = ''; return; }
    locked = true; // Before the first await: concurrent clicks cannot encrypt/send twice.
    let password = input.value;
    input.value = '';
    try {
      if (now() >= Number(challenge.expires_at) * 1000) { state = 'expired'; return; }
      state = 'encrypting';
      render();
      const pending = encryptCredential(challenge, password, cryptoApi);
      password = '';
      const payload = await pending;
      if (disposed || state === 'cancelled') return;
      if (now() >= Number(challenge.expires_at) * 1000) { state = 'expired'; return; }
      try {
        webApp.sendData(payload);
        state = 'submitted'; // sendData has no delivery/connection acknowledgement.
      } catch {
        state = 'send_failed'; // Ambiguous delivery: never auto-retry or unlock.
      }
    } catch (error) {
      if (disposed || state === 'cancelled') return;
      state = error?.message === 'invalid_password' ? 'invalid_password' : 'encryption_failed';
      locked = state !== 'invalid_password';
    } finally {
      password = '';
      input.value = '';
      if (!disposed) render();
    }
  }

  function onCancel(event) {
    event.preventDefault();
    if (disposed || state === 'submitted' || state === 'send_failed') return;
    locked = true;
    state = 'cancelled';
    input.value = '';
    render();
    try { webApp?.close?.(); } catch { /* Keep the cleared, cancelled screen. */ }
  }

  function onLanguage() {
    locale = language.value === 'ko' ? 'ko' : 'en';
    render();
  }

  input.value = '';
  render();
  form.addEventListener('submit', onSubmit);
  cancel.addEventListener('click', onCancel);
  language.addEventListener('change', onLanguage);
  return { dispose() {
    disposed = true;
    locked = true;
    input.value = '';
    input.disabled = true;
    submit.disabled = true;
    form.removeEventListener('submit', onSubmit);
    cancel.removeEventListener('click', onCancel);
    language.removeEventListener('change', onLanguage);
  } };
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const mounted = mountCredentialForm({ document, webApp: window.Telegram?.WebApp,
    cryptoApi: window.crypto, location: window.location, now: Date.now });
  window.addEventListener('pagehide', () => mounted.dispose(), { once: true });
}
