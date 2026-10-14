"""Plain notifications with bounded UTF-16 size and inert media syntax."""

import re
import unicodedata

from .config import Account, MAX_NOTIFICATION_UNITS
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


def render_notification(account: Account, mail: MailContent, result: TriageResult, *, mail_id="") -> str:
    sender = f"{mail.sender_name} <{mail.sender_address}>" if mail.sender_name else mail.sender_address
    priority = {"high": "높음", "normal": "보통", "low": "낮음"}[result.priority]
    category = {"work": "업무", "personal": "개인", "transactional": "거래·서비스",
                "promotion": "홍보", "newsletter": "뉴스레터", "unknown": "판단 불충분"}[result.category]
    lines = [
        f"[{'원문 확인' if result.decision == 'review' else '중요 메일'} · {_field(account.label, 100)}]",
        f"우선순위: {priority}",
        f"분류: {category}",
        f"보낸 사람: {_field(sender or '확인 불가', 300)}",
        f"제목: {_field(mail.subject or '(제목 없음)', 420)}",
        f"이유: {_field(result.reason, 400)}",
        f"요약: {_field(result.summary or '본문 확인 필요', 1200)}",
        f"할 일: {_field(result.action or '명시 없음', 500)}",
        f"기한: {_field(result.deadline or '명시 없음', 200)}",
    ]
    if mail_id:
        lines.append(f"메일 ID: {_field(mail_id, 64)}")
    if mail.needs_body_review or mail.truncated or result.decision == "review":
        lines.append("※ 본문 확인 필요: 메일의 일부 내용만 확인했습니다.")
    text = "\n".join(lines)
    assert utf16_length(text) <= MAX_NOTIFICATION_UNITS
    return text
