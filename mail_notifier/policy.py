"""Versioned work/personal-mail policy and bounded explicit user feedback."""

import re

POLICY_VERSION = "work-personal-v1"
CATEGORIES = ("work", "personal", "transactional", "promotion", "newsletter", "unknown")
DECISIONS = ("notify", "suppress", "review")
PRIORITIES = ("high", "normal", "low")

INSTRUCTIONS = (
    "Prioritize meaningful work and personal correspondence that the recipient should read or act on. "
    "Notify for work requests, relevant personal contact, schedule/contract changes, deadlines, "
    "reservation cancellations, payment failures and significant account security changes. "
    "Suppress advertisements, promotions, mass sales outreach, routine receipts and newsletters by default. "
    "An automated sender can carry an important service change; a personal-looking sender can be marketing. "
    "Judge purpose, relevance to the recipient and the need to read or act, not isolated words or headers. "
    "Sender/keyword preferences and past corrections are context, not automatic decisions. "
    "Use review when evidence is insufficient. High priority needs explicit urgency or significant impact. "
)


def valid_sender(sender: str) -> bool:
    return isinstance(sender, str) and re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", sender) is not None


def select_feedback(sender: str, examples) -> list[dict]:
    if not valid_sender(sender):
        return []
    matching = [item for item in examples if item.get("sender_address", "").casefold() == sender.casefold()]
    matching.sort(key=lambda item: item.get("created_at", 0))
    limits = {"subject": 200, "summary": 300, "decision": 20, "note": 300}
    return [{key: str(item.get(key, ""))[:limit] for key, limit in limits.items()} for item in matching[-5:]]
