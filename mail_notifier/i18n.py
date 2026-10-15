"""Stable Korean/English messages, independent of the model's summary language."""

import re

MESSAGES: dict[str, dict[str, str]] = {
    "ko": {
        "notification.notify": "중요 메일",
        "notification.review": "원문 확인",
        "notification.header": "[{title} · {account}]",
        "notification.priority": "우선순위: {value}",
        "notification.category": "분류: {value}",
        "notification.sender": "보낸 사람: {value}",
        "notification.subject": "제목: {value}",
        "notification.reason": "이유: {value}",
        "notification.summary": "요약: {value}",
        "notification.action": "할 일: {value}",
        "notification.deadline": "기한: {value}",
        "notification.mail_id": "메일 ID: {value}",
        "notification.unknown_sender": "확인 불가",
        "notification.no_subject": "(제목 없음)",
        "notification.check_body": "본문 확인 필요",
        "notification.not_specified": "명시 없음",
        "notification.partial_body": "※ 본문 확인 필요: 메일의 일부 내용만 확인했습니다.",
        "priority.high": "높음",
        "priority.normal": "보통",
        "priority.low": "낮음",
        "category.work": "업무",
        "category.personal": "개인",
        "category.transactional": "거래·서비스",
        "category.promotion": "홍보",
        "category.newsletter": "뉴스레터",
        "category.unknown": "판단 불충분",
        "triage.excluded_sender": "사용자가 제외한 발신자",
        "triage.always_notify": "사용자 지정 필수 알림 · {reason}",
        "triage.incomplete_body": "본문이 불완전해 원문 확인 필요 · {reason}",
        "triage.invalid_attempts": "AI 판단 응답을 5회 검증하지 못했습니다. 원문 확인이 필요합니다.",
        "guidance.account": "hermes mail-notifier setup에서 계정·앱 비밀번호·메일함을 확인하세요.",
        "guidance.credentials": "현재 Hermes 프로필에 모델·Telegram 자격증명과 기본 채널을 저장하세요.",
        "guidance.cron": "hermes mail-notifier setup으로 cron을 복구하거나 hermes cron resume JOB_ID로 재개하세요.",
        "guidance.queue": "hermes mail-notifier queue list로 확인한 뒤 queue retry MAIL_ID 또는 --account ID를 사용하세요.",
        "guidance.uidvalidity": "메일 서버 상태를 확인한 뒤 hermes mail-notifier reset ACCOUNT_ID로 새 기준점을 설정하세요.",
        "guidance.running": "현재 실행이 끝난 뒤 다시 실행하세요.",
        "guidance.no_accounts": "hermes mail-notifier accounts add로 계정을 연결하세요.",
        "guidance.first_check": "hermes mail-notifier check로 최초 확인을 실행하세요.",
        "guidance.default": "hermes mail-notifier doctor --json으로 상태를 확인하고 해당 설정을 수정한 뒤 재시도하세요.",
        "diagnostic.subject": "회의 일정 확인 요청",
        "diagnostic.body": "내일 회의에 참석 가능한지 회신해 주세요.",
        "diagnostic.notification": "메일 알림 진단 테스트 · 실제 메일 내용은 포함되지 않습니다.",
        "setup.notification": "메일 알림 설정 완료. 연결 시점 이후의 중요한 새 메일을 요약해 알려드립니다.",
    },
    "en": {
        "notification.notify": "Important email",
        "notification.review": "Review email",
        "notification.header": "[{title} · {account}]",
        "notification.priority": "Priority: {value}",
        "notification.category": "Category: {value}",
        "notification.sender": "From: {value}",
        "notification.subject": "Subject: {value}",
        "notification.reason": "Reason: {value}",
        "notification.summary": "Summary: {value}",
        "notification.action": "Action: {value}",
        "notification.deadline": "Deadline: {value}",
        "notification.mail_id": "Mail ID: {value}",
        "notification.unknown_sender": "Unknown",
        "notification.no_subject": "(No subject)",
        "notification.check_body": "Check the email body",
        "notification.not_specified": "Not specified",
        "notification.partial_body": "※ Check the email body: Only part of the email was available.",
        "priority.high": "High",
        "priority.normal": "Normal",
        "priority.low": "Low",
        "category.work": "Work",
        "category.personal": "Personal",
        "category.transactional": "Transactional",
        "category.promotion": "Promotion",
        "category.newsletter": "Newsletter",
        "category.unknown": "Unknown",
        "triage.excluded_sender": "Sender excluded by the user",
        "triage.always_notify": "Required notification by user rule · {reason}",
        "triage.incomplete_body": "Incomplete body; check the original email · {reason}",
        "triage.invalid_attempts": "Model output failed validation 5 times. Check the original email.",
        "guidance.account": "Check the account, app password and mailbox with hermes mail-notifier setup.",
        "guidance.credentials": "Save the model and Telegram credentials and home channel in the current Hermes profile.",
        "guidance.cron": "Repair cron with hermes mail-notifier setup or resume it with hermes cron resume JOB_ID.",
        "guidance.queue": "Inspect pending work with hermes mail-notifier queue list, then use queue retry MAIL_ID or --account ID.",
        "guidance.uidvalidity": "Check the mail server, then set a new baseline with hermes mail-notifier reset ACCOUNT_ID.",
        "guidance.running": "Wait for the current run to finish, then try again.",
        "guidance.no_accounts": "Connect an account with hermes mail-notifier accounts add.",
        "guidance.first_check": "Run the first check with hermes mail-notifier check.",
        "guidance.default": "Check the status with hermes mail-notifier doctor --json, correct the relevant settings and retry.",
        "diagnostic.subject": "Meeting attendance request",
        "diagnostic.body": "Please reply to confirm whether you can attend tomorrow's meeting.",
        "diagnostic.notification": "Mail notification diagnostic test · No actual email content is included.",
        "setup.notification": "Mail notifications are configured. Important new emails arriving after connection will be summarized.",
    },
}


def resolve_ui_language(*, explicit: str = "", saved: str = "", detected: str = "") -> str:
    """Resolve the first selected locale; unsupported or missing locales use English."""
    selected = explicit or saved or detected
    if not isinstance(selected, str):
        return "en"
    language = re.split(r"[-_.@]", selected.strip().casefold(), maxsplit=1)[0]
    return language if language in MESSAGES else "en"


def text(key: str, language: str, **values) -> str:
    return MESSAGES[resolve_ui_language(explicit=language)][key].format(**values)
