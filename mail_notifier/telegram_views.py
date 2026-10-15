"""Non-secret, deterministic Korean/English panels with opaque server actions."""

import secrets
import time

from .config import settings_fingerprint
from .hermes_host import HostError
from .i18n import resolve_ui_language, text
from .onboarding_auth import _shared, owner_key


def _ui():
    shared = _shared()
    with shared.lock:
        if not hasattr(shared, "telegram_ui"):
            shared.telegram_ui = {"actions": {}, "waiting": {}, "languages": {}, "transports": {}, "profiles": {}}
        return shared.telegram_ui


def prune():
    now = time.time()
    with _shared().lock:
        for name in ("actions", "waiting", "languages"):
            for key, record in list(_ui()[name].items()):
                if now >= record["expires"]:
                    del _ui()[name][key]


def clear_profile(home):
    with _shared().lock:
        for name in ("actions", "waiting", "languages"):
            for key, record in list(_ui()[name].items()):
                if record["owner"][0] == home:
                    del _ui()[name][key]


def button(draft, label, action, **values):
    prune()
    token = "mn:" + secrets.token_urlsafe(24)
    with _shared().lock:
        actions = _ui()["actions"]
        if len(actions) >= 8192:
            del actions[next(iter(actions))]
        actions[token] = {"owner": owner_key(draft.owner), "draft_id": draft.id, "revision": draft.revision,
                          "action": action, "values": values, "expires": time.time() + 600}
    return {"text": label, "callback_data": token}


def peek_action(token, owner):
    prune()
    with _shared().lock:
        record = _ui()["actions"].get(token)
        if record is None or record["owner"] != owner_key(owner):
            raise HostError("setup_changed_retry")
        return dict(record)


def take_action(token, owner):
    with _shared().lock:
        record = peek_action(token, owner)
        del _ui()["actions"][token]
        return record


def panel(draft, settings, *, language: str, page: int = 0) -> dict:
    language = resolve_ui_language(explicit=language)
    view = {"text": "Mail Notifier\n", "buttons": [], "input_field": None, "language": language}
    if draft is None:
        view["text"] += text("telegram.entry", language)
        return view
    candidate = draft.candidate
    view.update(draft_id=draft.id, revision=draft.revision, state=draft.state)
    def add(key, action, **values):
        view["buttons"].append(button(draft, text("telegram." + key, language), action, **values))
    state = draft.state
    if state in {"completed", "cancelled", "expired", "acknowledged"}:
        view["text"] += text("telegram." + state, language)
        return view
    if state == "needs_review":
        view["text"] += text("setup.review_external", language) + "\n"
        view["text"] += text("telegram.current", language) + "\n" + _settings(settings, language)
        view["text"] += "\n" + text("setup.preview", language) + "\n" + _settings(candidate, language)
        view["text"] += "\n" + text("telegram.steps", language, steps=", ".join(sorted(draft.steps)))
        add("acknowledge", "acknowledge", current_fingerprint=settings_fingerprint(settings))
        return view
    if state in {"scheduled", "repair_required", "committing"}:
        paused = state == "scheduled" and draft.steps.get("cron", {}).get("paused")
        view["text"] += text("telegram.paused" if paused else "telegram.partial", language)
        view["text"] += "\n" + _settings(candidate, language)
        view["text"] += "\n" + text("telegram.steps", language, steps=", ".join(sorted(draft.steps)))
        if draft.steps.get("error_code"):
            view["text"] += "\n" + text("telegram.error", language, code=draft.steps["error_code"])
        if paused:
            add("resume", "resume")
            if draft.steps.get("test_delivery", {}).get("message_id"):
                add("keep_paused", "acknowledge", current_fingerprint=settings_fingerprint(settings))
            else:
                add("repair", "repair")
        else:
            add("repair", "repair")
        return view
    if state == "awaiting_confirmation":
        view["text"] += text("setup.preview", language) + "\n" + _settings(candidate, language)
        if draft.steps.get("error_code"):
            view["text"] += "\n" + text("telegram.error", language, code=draft.steps["error_code"])
        add("confirm", "confirm")
        add("back", "back")
        add("cancel", "cancel")
        return view
    if state in {"awaiting_secret", "needs_secret", "validating"}:
        account = next((a for a in candidate.accounts if a.id == draft.form.get("account_id")), None)
        view["text"] += text("telegram.secret", language)
        if account:
            view["text"] += "\n" + account.username + "\n" + text("setup.provider." + account.provider, language)
        if draft.steps.get("error_code"):
            view["text"] += "\n" + text("telegram.error", language, code=draft.steps["error_code"])
        add("secure", "secure")
        add("guide", "guide")
        add("back", "back")
        add("cancel", "cancel")
        return view
    if draft.form.get("input_field") == "username":
        view["text"] += text("telegram.address", language)
        view["input_field"] = "username"
        add("back", "back")
        add("cancel", "cancel")
        return view
    if candidate.accounts and draft.form.get("action") != "connect":
        view["text"] += _settings(candidate, language)
        add("add", "add")
    else:
        view["text"] += text("telegram.provider", language)
        for provider in ("naver", "daum", "gmail"):
            view["buttons"].append(button(draft, text("telegram." + provider, language), "provider", provider=provider))
    for locale, label in (("ko", "한국어"), ("en", "English")):
        view["buttons"].append(button(draft, label, "language", language=locale))
    add("cancel", "cancel")
    return view


def _settings(settings, language):
    accounts = "\n".join(f"{a.label} · {a.username} · {a.provider} · {a.mailbox}" for a in settings.accounts)
    return accounts + "\n" + text("telegram.settings", language, target=settings.target,
                                  schedule=settings.schedule, summary_language=settings.language)
