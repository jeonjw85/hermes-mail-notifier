"""Bounded MIME extraction without executing HTML or reading attachments."""

import re
from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.header import decode_header
from email.parser import BytesHeaderParser, BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser

from .config import MAX_BODY_CHARS, MAX_RAW_BYTES


@dataclass(frozen=True)
class MailContent:
    sender_address: str
    sender_name: str
    subject: str
    date: str
    text: str
    truncated: bool
    needs_body_review: bool


def _decode(value: bytes, charset=None) -> str:
    for encoding in (charset, "utf-8", "cp949"):
        if encoding:
            try:
                return value.decode(encoding)
            except (UnicodeError, LookupError):
                pass
    return value.decode("utf-8", errors="replace")


def _header(value) -> str:
    try:
        chunks = decode_header(str(value or ""))
        text = "".join(_decode(part, charset) if isinstance(part, bytes) else part for part, charset in chunks)
    except (ValueError, UnicodeError):
        text = str(value or "")
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "li", "tr", "h1", "h2"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def _body(part, depth=0):
    if depth > 24:
        return "", True
    if part.get_content_disposition() == "attachment" or part.get_filename() is not None:
        return "", False
    kind = part.get_content_type()
    if kind.startswith("message/"):
        return "", False
    if part.is_multipart():
        children = part.get_payload()
        if kind == "multipart/alternative":
            children = sorted(children, key=lambda child: child.get_content_type() != "text/plain")
        texts, incomplete = [], bool(part.defects)
        for child in children:
            text, bad = _body(child, depth + 1)
            incomplete |= bad
            if text.strip():
                texts.append(text)
                if kind == "multipart/alternative":
                    break
        return "\n".join(texts), incomplete
    if kind not in {"text/plain", "text/html"}:
        return "", False
    payload = part.get_payload(decode=True)
    text = _decode(payload, part.get_content_charset()) if isinstance(payload, bytes) else str(part.get_payload() or "")
    if kind == "text/html":
        parser = _HTMLText()
        parser.feed(text)
        parser.close()
        text = "".join(parser.chunks)
    return text, bool(part.defects)


def _metadata(mail):
    # Encoded words belong to the display name. Decoding before parsing lets a
    # displayed address become address syntax and hide the actual mailbox.
    name, address = parseaddr(str(mail.get("From") or ""))
    return address.casefold(), _header(name), _header(mail.get("Subject")), _header(mail.get("Date"))


def parse_message(raw: bytes, *, truncated: bool = False) -> MailContent:
    truncated = truncated or len(raw) > MAX_RAW_BYTES
    raw = raw[:MAX_RAW_BYTES]
    parse_errors = (ValueError, UnicodeError, RecursionError, MessageError)
    try:
        mail = BytesParser(policy=policy.compat32).parsebytes(raw)
        address, name, subject, date = _metadata(mail)
        text, incomplete = _body(mail)
    except parse_errors:
        # BytesParser can exhaust its recursion budget before our MIME depth
        # guard runs. Header-only parsing never descends into those MIME parts.
        try:
            headers = BytesHeaderParser(policy=policy.compat32).parsebytes(raw)
            address, name, subject, date = _metadata(headers)
        except parse_errors:
            address, name, subject, date = "", "", "", ""
        text, incomplete = "", True
    text = text.strip()
    truncated = truncated or len(text) > MAX_BODY_CHARS
    return MailContent(
        sender_address=address, sender_name=name, subject=subject, date=date,
        text=text[:MAX_BODY_CHARS], truncated=truncated,
        needs_body_review=truncated or incomplete or not text,
    )
