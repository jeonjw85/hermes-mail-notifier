"""Test-only helpers; no real credentials or network connections."""

import importlib


def feature(case, name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("mail_notifier"):
            case.fail(f"Feature not implemented: {name}")
        raise


def account_data(account_id="a1", provider="naver", **changes):
    result = {
        "id": account_id,
        "label": "개인 메일",
        "provider": provider,
        "username": f"{account_id}@example.com",
        "password_secret": f"HERMES_MAIL_{account_id.upper()}_APP_PASSWORD",
        "mailbox": "INBOX",
        "enabled": True,
    }
    result.update(changes)
    return result


def deeply_nested_message():
    parts = [b"From: actual@example.com\r\nSubject: Review this mail\r\n"]
    for depth in range(1100):
        parts.append(f'Content-Type: multipart/mixed; boundary="b{depth}"\r\n\r\n--b{depth}\r\n'.encode())
    parts.append(b"Content-Type: text/plain\r\n\r\nDeep body\r\n")
    for depth in reversed(range(1100)):
        parts.append(f"--b{depth}--\r\n".encode())
    return b"".join(parts)
