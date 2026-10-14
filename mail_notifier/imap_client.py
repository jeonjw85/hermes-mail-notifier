"""Read-only, TLS-verified IMAP acquisition."""

import base64
import imaplib
import re
import ssl
from dataclasses import dataclass

from .config import Account, MAX_RAW_BYTES, PROVIDERS
from .message import MailContent, parse_message


class MailboxError(RuntimeError):
    def __init__(self, code="imap_connection_failed"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Discovery:
    uids: list[int]
    scanned_through: int


def _mailbox_argument(name: str) -> str:
    if name.upper() == "INBOX":
        return "INBOX"
    chunks, non_ascii = [], []

    def flush():
        if non_ascii:
            encoded = base64.b64encode("".join(non_ascii).encode("utf-16-be"))
            chunks.append("&" + encoded.rstrip(b"=").replace(b"/", b",").decode("ascii") + "-")
            non_ascii.clear()

    # IMAP4rev1 modified UTF-7; we deliberately do not negotiate UTF8=ACCEPT.
    for char in name:
        if 32 <= ord(char) <= 126:
            flush()
            chunks.append("&-" if char == "&" else char)
        else:
            non_ascii.append(char)
    flush()
    encoded = "".join(chunks).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + encoded + '"'


class ImapMailbox:
    def __init__(self, account: Account, password: str, *, timeout=20, remaining=None):
        self.account = account
        self.password = password
        self.connection = None
        self.timeout = timeout
        self.remaining = remaining

    def _refresh_timeout(self):
        self.set_timeout(min(self.timeout, self.remaining()) if self.remaining else self.timeout)

    def set_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise MailboxError("imap_timeout")
        self.timeout = seconds
        if self.connection is not None and getattr(self.connection, "sock", None) is not None:
            self.connection.sock.settimeout(seconds)

    def __enter__(self):
        try:
            self._refresh_timeout()
            self.connection = imaplib.IMAP4_SSL(
                PROVIDERS[self.account.provider], port=993,
                ssl_context=ssl.create_default_context(), timeout=self.timeout,
            )
            self._call("login", self.account.username, self.password, code="imap_authentication_failed")
            self._call("select", _mailbox_argument(self.account.mailbox), readonly=True, code="imap_mailbox_unavailable")
            return self
        except MailboxError:
            self.__exit__(None, None, None)
            raise
        except (OSError, imaplib.IMAP4.error):
            self.__exit__(None, None, None)
            raise MailboxError("imap_connection_failed") from None

    def __exit__(self, *args):
        self.password = ""
        if self.connection is not None:
            try:
                self._refresh_timeout()
                self.set_timeout(min(self.timeout, 2))
                self.connection.logout()
            except (OSError, imaplib.IMAP4.error, MailboxError):
                pass
            finally:
                if hasattr(self.connection, "shutdown"):
                    try:
                        self.connection.shutdown()
                    except OSError:
                        pass
            self.connection = None

    def _call(self, method, *args, code="imap_request_failed", **kwargs):
        try:
            self._refresh_timeout()
            status, data = getattr(self.connection, method)(*args, **kwargs)
            if status != "OK":
                raise MailboxError(code)
            return data
        except (OSError, imaplib.IMAP4.abort):
            raise MailboxError("imap_connection_failed") from None
        except (imaplib.IMAP4.error, UnicodeError):
            raise MailboxError(code) from None

    def snapshot(self) -> tuple[int, int]:
        if self.connection is None:
            raise MailboxError("imap_connection_failed")
        try:
            epoch = int(self.connection.response("UIDVALIDITY")[1][0])
            if epoch <= 0:
                raise ValueError
            next_uid = self.connection.response("UIDNEXT")[1][0]
            if next_uid is not None:
                highest = int(next_uid) - 1
                if highest < 0:
                    raise ValueError
            else:
                data = self._call("uid", "FETCH", "*", "(UID)")
                metadata = b" ".join(item[0] if isinstance(item, tuple) else item for item in data if item)
                matches = re.findall(rb"\bUID (\d+)\b", metadata)
                if len(matches) > 1 or (metadata and not matches):
                    raise ValueError
                highest = int(matches[0]) if matches else 0
            return epoch, highest
        except (TypeError, ValueError, IndexError, OSError, imaplib.IMAP4.error):
            raise MailboxError("imap_invalid_uid_state") from None

    def new_uids(self, after_uid: int, limit: int) -> list[int]:
        return self.discover(after_uid, limit, self.snapshot()[1]).uids

    def discover(self, after_uid: int, limit: int, upper_uid: int) -> Discovery:
        if limit <= 0 or after_uid >= upper_uid:
            return Discovery([], after_uid)
        end = min(after_uid + 1000, upper_uid)
        data = self._call("uid", "SEARCH", None, f"UID {after_uid + 1}:{end}")
        try:
            found = sorted({int(uid) for uid in (data[0] or b"").split()})
            if len(found) > 1000 or any(uid <= 0 or uid > end for uid in found):
                raise ValueError
            found = [uid for uid in found if uid > after_uid]
            selected = found[:limit]
            return Discovery(selected, selected[-1] if len(selected) < len(found) else end)
        except (ValueError, IndexError, TypeError):
            raise MailboxError("imap_invalid_uid_response") from None

    def fetch(self, uid: int) -> MailContent | None:
        data = self._call("uid", "FETCH", str(uid), f"(UID RFC822.SIZE BODY.PEEK[]<0.{MAX_RAW_BYTES}>)")
        literals = [item for item in data if isinstance(item, tuple)]
        if not literals:
            return None
        metadata = b" ".join(item[0] if isinstance(item, tuple) else item for item in data if item is not None)
        returned = re.findall(rb"\bUID (\d+)\b", metadata)
        size = re.search(rb"\bRFC822.SIZE (\d+)\b", metadata)
        if len(literals) != 1 or not returned or any(int(value) != uid for value in returned) or size is None:
            raise MailboxError("imap_invalid_fetch_response")
        raw = literals[0][1]
        if not isinstance(raw, bytes):
            raise MailboxError("imap_invalid_fetch_response")
        return parse_message(raw, truncated=int(size.group(1)) > len(raw))
