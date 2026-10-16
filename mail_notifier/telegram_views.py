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


EDIT_SETTINGS = ("important_senders", "ignored_senders", "always_notify_senders", "important_keywords", "importance_instructions")


def bounded(value, units=160):
    """Summarize output only. Never feed this back into a candidate setting."""
    value = str(value)
    if len(value.encode("utf-16-le")) // 2 <= units:
        return value
    result, used = [], 0
    for char in value:
        size = 2 if ord(char) > 0xffff else 1
        if used + size > units - 1:
            break
        result.append(char)
        used += size
    return "".join(result) + "…"


def _value(value):
    if isinstance(value, tuple):
        return bounded(", ".join(value), 140) + f" ({len(value)})"
    return bounded(value, 180)


def _account(account, language):
    state = text("status.enabled" if account.enabled else "status.disabled", language)
    return " · ".join((bounded(account.label, 55), bounded(account.username, 75), state))


def panel(draft, settings, *, language: str, page: int = 0, change_page: int = 0, resume_confirmation=False,
          status=None, current_fingerprint=None, can_handoff_delivery=False, disable_only=False, footer="") -> dict:
    language = resolve_ui_language(explicit=language)
    view = {"text": "Mail Notifier\n", "buttons": [], "input_field": None, "language": language}
    if draft is None:
        view["text"] += text("telegram.entry", language)
        return view
    candidate = draft.candidate
    page_accounts = settings.accounts if draft.state == "needs_review" else candidate.accounts
    page = max(0, min(page, max(0, (len(page_accounts) - 1) // 5)))
    view.update(draft_id=draft.id, revision=draft.revision, state=draft.state)
    def add(key, action, **values):
        view["buttons"].append(button(draft, text("telegram." + key, language), action, **values))
    def menu(key):
        add("settings_menu" if key == "settings" else key, "menu", menu=key)
    def pages():
        if page:
            add("previous_page", "page", page=page - 1, change_page=change_page)
        if (page + 1) * 5 < len(page_accounts):
            add("next_page", "page", page=page + 1, change_page=change_page)
    def changes(*, original=None, tail=""):
        rows = _changes(draft, language, original=original)
        heading = "\n" + text("telegram.changes", language) + "\n"
        # Reserve the exact surrounding text and evidence, plus an upper bound
        # on page-counter digits. Only individual values are abbreviated; never
        # clip whole field comparisons to fit a panel.
        counter = text("telegram.change_page", language, page=len(rows), pages=len(rows)) + "\n"
        budget = min(1400, 3500 - len((view["text"] + heading + counter + tail + footer).encode("utf-16-le")) // 2)
        groups, used = [[]], 0
        for row in rows:
            size = len(row.encode("utf-16-le")) // 2
            if groups[-1] and used + 1 + size > budget:
                groups.append([])
                used = 0
            used += size + bool(groups[-1])
            groups[-1].append(row)
        selected = max(0, min(change_page, len(groups) - 1))
        view["text"] += (heading + text("telegram.change_page", language, page=selected + 1, pages=len(groups))
                         + "\n" + "\n".join(groups[selected]) + tail)
        if selected:
            add("previous_changes", "changes_page", page=selected - 1, account_page=page)
        if selected + 1 < len(groups):
            add("next_changes", "changes_page", page=selected + 1, account_page=page)
    def finish():
        view["text"] = bounded(view["text"], 3500 - len(footer.encode("utf-16-le")) // 2) + footer
        return view
    state = draft.state
    if state in {"completed", "cancelled", "expired", "acknowledged"}:
        key = "all_disabled" if state == "completed" and not any(a.enabled for a in candidate.accounts) else state
        if state == "completed" and disable_only and key != "all_disabled":
            key = "disabled_saved"
        view["text"] += text("telegram." + key, language)
        return finish()
    if resume_confirmation:
        view["text"] += text("setup.resume", language) + "\n" + _settings(candidate, language, page)
        add("confirm_resume", "confirm_resume")
        add("back", "recovery_back")
        return finish()
    if state == "needs_review":
        view["text"] += text("setup.review_external", language) + "\n"
        view["text"] += text("telegram.current", language) + "\n" + _settings(settings, language, page)
        view["text"] += "\n" + text("setup.preview", language)
        changes(original=settings, tail="\n" + text("telegram.steps", language, steps=", ".join(sorted(draft.steps))))
        add("acknowledge", "acknowledge", current_fingerprint=current_fingerprint or settings_fingerprint(settings))
        pages()
        return finish()
    if state in {"scheduled", "repair_required", "committing"}:
        paused = state == "scheduled" and draft.steps.get("cron", {}).get("paused")
        view["text"] += text("telegram.paused" if paused else "telegram.partial", language)
        if not paused and draft.steps.get("cron", {}).get("paused"):
            view["text"] += "\n" + text("telegram.paused", language)
        view["text"] += "\n" + _settings(candidate, language, page)
        if disable_only:
            view["text"] += "\n" + text("setup.disable_only", language)
        view["text"] += "\n" + text("telegram.steps", language, steps=", ".join(sorted(draft.steps)))
        if draft.steps.get("error_code"):
            view["text"] += "\n" + text("telegram.error", language, code=draft.steps["error_code"])
        if paused:
            if not disable_only:
                add("resume", "resume")
            if draft.steps.get("test_delivery", {}).get("message_id") or disable_only:
                add("keep_paused", "acknowledge", current_fingerprint=current_fingerprint or settings_fingerprint(settings))
            else:
                add("repair", "repair", step="test_delivery")
        else:
            step = "test_delivery" if "cron" in draft.steps else "cron" if "settings_fingerprint" in draft.steps else "settings"
            add("repair", "repair", step=step)
        if can_handoff_delivery:
            view["text"] += "\n" + text("setup.unfinished_delivery", language)
            add("handoff_delivery", "acknowledge", current_fingerprint=current_fingerprint,
                allow_unfinished_delivery=True)
        pages()
        return finish()
    if state == "awaiting_confirmation":
        view["text"] += text("setup.disable_only" if disable_only else "setup.preview", language) + "\n" + _settings(candidate, language, page)
        tail = ""
        if draft.form.get("action") == "resume":
            tail += "\n" + text("setup.resume", language)
        if draft.steps.get("error_code"):
            tail += "\n" + text("telegram.error", language, code=draft.steps["error_code"])
        changes(tail=tail)
        add("confirm_resume" if draft.form.get("action") == "resume" else "confirm",
            "confirm_resume" if draft.form.get("action") == "resume" else "confirm")
        add("back", "back")
        add("cancel", "cancel")
        pages()
        return finish()
    if state in {"awaiting_secret", "needs_secret", "validating"} or draft.form.get("action") == "rotate":
        account = next((a for a in candidate.accounts if a.id == draft.form.get("account_id")), None)
        view["text"] += text("telegram.secret", language)
        if account:
            view["text"] += "\n" + bounded(account.username, 254) + "\n" + text("setup.provider." + account.provider, language)
        if draft.steps.get("error_code"):
            view["text"] += "\n" + text("telegram.error", language, code=draft.steps["error_code"])
        add("secure", "secure")
        add("guide", "guide")
        add("back", "back")
        add("cancel", "cancel")
        return finish()
    field = draft.form.get("input_field")
    if field:
        view["text"] += text("telegram.address" if field == "username" else "telegram.text_input", language)
        view["text"] += "\n" + text("setup." + field, language)
        account = next((a for a in candidate.accounts if a.id == draft.form.get("account_id")), None)
        current = getattr(account if field in {"label", "username", "mailbox"} else candidate, field, "")
        view["text"] += "\n" + text("telegram.existing_value", language, value=_value(current))
        view["input_field"] = field
        if field in EDIT_SETTINGS:
            add("clear", "clear", field=field)
        add("back", "back")
        add("cancel", "cancel")
        return finish()
    section = draft.form.get("action", "")
    if section == "account":
        account = next(a for a in candidate.accounts if a.id == draft.form.get("account_id"))
        view["text"] += (_account(account, language) + "\n" + bounded(account.username, 254) + "\n"
                         + text("telegram." + account.provider, language) + " · " + bounded(account.mailbox))
        for field in ("label", "username", "mailbox"):
            view["buttons"].append(button(draft, text("setup." + field, language), "input", field=field))
        menu("connection_provider")
        add("rotate", "rotate")
        add("disable" if account.enabled else "enable", "disable" if account.enabled else "enable")
    elif section == "connection_provider":
        view["text"] += text("setup.identity_changed", language)
        for provider in ("naver", "daum", "gmail"):
            view["buttons"].append(button(draft, text("telegram." + provider, language), "provider", provider=provider))
    elif section in {"settings", "schedule", "language", "ui_language", "policy", "recovery"}:
        view["text"] += _settings(candidate, language, page)
        if section == "settings":
            for key in ("schedule", "ui_language", "language", "policy"):
                menu(key)
            add("current_target", "current_target")
        elif section == "schedule":
            for minutes in (5, 10, 15, 30, 60):
                view["buttons"].append(button(draft, text("telegram.minutes", language, minutes=minutes), "setting",
                                              field="schedule", value=f"every {minutes}m"))
        elif section in {"language", "ui_language"}:
            for locale, label in (("ko", "한국어"), ("en", "English")):
                view["buttons"].append(button(draft, label, "setting", field=section, value=locale))
        elif section == "policy":
            for field in EDIT_SETTINGS:
                view["buttons"].append(button(draft, text("setup." + field, language), "input", field=field))
        else:
            if status:
                view["text"] += "\n" + text("telegram.status", language, cron=bounded(status["cron"].get("state")), pending=status["pending"])
            view["text"] += "\n" + text("telegram.recovery_notice", language)
            add("test", "test")
            add("resume", "resume")
        add("back", "back")
        add("cancel", "cancel")
        return finish()
    if candidate.accounts and draft.form.get("action") != "connect":
        if section not in {"account", "connection_provider"}:
            view["text"] += _settings(candidate, language, page)
            for index, account in enumerate(candidate.accounts[page * 5:page * 5 + 5], page * 5 + 1):
                view["buttons"].append(button(draft, f"{index}. " + _account(account, language), "account", account_id=account.id))
            pages()
            add("add", "add")
            for key in ("settings", "schedule", "ui_language", "language", "policy", "recovery"):
                menu(key)
            if candidate != draft.original or draft.steps.get("credentials"):
                add("review", "review")
        else:
            add("back", "back")
    else:
        view["text"] += text("telegram.provider", language)
        for provider in ("naver", "daum", "gmail"):
            view["buttons"].append(button(draft, text("telegram." + provider, language), "provider", provider=provider))
    if section not in {"account", "connection_provider"}:
        for locale, label in (("ko", "한국어"), ("en", "English")):
            view["buttons"].append(button(draft, label, "language", language=locale))
    add("cancel", "cancel")
    return finish()


def _settings(settings, language, page=0):
    accounts = "\n".join(_account(a, language) + " · " + text("telegram." + a.provider, language) + " · " + bounded(a.mailbox, 35)
                         for a in settings.accounts[page * 5:page * 5 + 5])
    accounts += "\n" + text("telegram.page", language, page=page + 1, pages=max(1, (len(settings.accounts) + 4) // 5), count=len(settings.accounts))
    return accounts + "\n" + text("telegram.settings", language, target=bounded(settings.target, 150),
                                  schedule=bounded(settings.schedule, 150), summary_language=bounded(settings.language, 60))


def _changes(draft, language, *, original=None):
    base = draft.original if original is None else original
    lines = []
    for name in ("target", "schedule", "ui_language", "language", *EDIT_SETTINGS):
        before, after = getattr(base, name), getattr(draft.candidate, name)
        if before != after:
            lines.append(text("setup." + name, language) + ": " + _value(before) + " → " + _value(after))
    accounts = {a.id: a for a in base.accounts}
    candidates = {a.id: a for a in draft.candidate.accounts}
    for identifier in dict.fromkeys([*accounts, *candidates]):
        old, account = accounts.get(identifier), candidates.get(identifier)
        if old and old != account:
            if account is None:
                lines.append(text("telegram.current_only_connection", language) + " " + _account(old, language)
                             + " · " + text("telegram." + old.provider, language) + " · " + bounded(old.mailbox))
                continue
            for name in ("label", "provider", "username", "mailbox", "enabled"):
                if getattr(old, name) != getattr(account, name):
                    label = text("telegram.account_state" if name == "enabled" else "setup." + name, language)
                    before, after = getattr(old, name), getattr(account, name)
                    if name == "enabled":
                        before = text("status.enabled" if before else "status.disabled", language)
                        after = text("status.enabled" if after else "status.disabled", language)
                    lines.append(_account(account, language) + "\n" + label + ": " + _value(before) + " → " + _value(after))
        elif account is not None and not old:
            lines.append(text("telegram.new_connection" if original is None else "telegram.candidate_only_connection", language) + " " + _account(account, language)
                         + " · " + text("telegram." + account.provider, language) + " · " + bounded(account.mailbox))
    if any(a.id not in accounts for a in draft.candidate.accounts) and any(not a.enabled and accounts.get(a.id, a).enabled for a in draft.candidate.accounts):
        lines.append(text("setup.identity_changed", language))
    if original is None and draft.steps.get("credentials"):
        lines.append(text("telegram.credentials_preview", language))
    lines.append(text("setup.retention", language))
    return lines
