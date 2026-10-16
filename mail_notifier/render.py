"""Plain notifications with bounded UTF-16 size and inert media syntax."""

import re
import unicodedata

from .config import Account, MAX_NOTIFICATION_UNITS
from .i18n import text
from .message import MailContent
from .triage import TriageResult


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _field(text: str, budget: int) -> str:
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    # Full-width separators keep paths readable but inert to Hermes's local-file
    # and MEDIA/markdown extraction. Apply to every external field, not just body.
    text = text.translate(str.maketrans({"/": "／", "\\": "＼", "[": "［", "]": "］"}))
    text = re.sub(r"(?i)media\s*:", "MEDIA：", text).strip()
    if utf16_length(text) <= budget:
        return text
    size, chunks = 0, []
    for char in text:
        size += 2 if ord(char) > 0xFFFF else 1
        if size > budget - 1:
            break
        chunks.append(char)
    return "".join(chunks) + "…"


def render_notification(account: Account, mail: MailContent, result: TriageResult, *, mail_id="", language="ko") -> str:
    sender = f"{mail.sender_name} <{mail.sender_address}>" if mail.sender_name else mail.sender_address
    priority = text(f"priority.{result.priority}", language)
    category = text(f"category.{result.category}", language)
    lines = [
        text("notification.header", language, title=text("notification.review" if result.decision == "review" else "notification.notify", language),
             account=_field(account.label, 100)),
        text("notification.priority", language, value=priority),
        text("notification.category", language, value=category),
        text("notification.sender", language, value=_field(sender or text("notification.unknown_sender", language), 300)),
        text("notification.subject", language, value=_field(mail.subject or text("notification.no_subject", language), 420)),
        text("notification.reason", language, value=_field(result.reason, 400)),
        text("notification.summary", language, value=_field(result.summary or text("notification.check_body", language), 1200)),
        text("notification.action", language, value=_field(result.action or text("notification.not_specified", language), 500)),
        text("notification.deadline", language, value=_field(result.deadline or text("notification.not_specified", language), 200)),
    ]
    if mail_id:
        lines.append(text("notification.mail_id", language, value=_field(mail_id, 64)))
    if mail.needs_body_review or mail.truncated or result.decision == "review":
        lines.append(text("notification.partial_body", language))
    notification = "\n".join(lines)
    assert utf16_length(notification) <= MAX_NOTIFICATION_UNITS
    return notification
