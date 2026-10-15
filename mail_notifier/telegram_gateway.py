"""Telegram transport admission; SDK imports stay lazy and inputs stay off events."""

# Hermes modules are supplied by the host, not plugin runtime dependencies.
# pyright: reportMissingImports=false
import time

from .onboarding_auth import PREFIX, AdmissionBroker, NativeInput, acquire_broker


def native_input(update, bot, *, kind: str, value: str) -> NativeInput | None:
    """Read server/SDK identity, never Web App self-reported user information."""
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if (message is None or user is None or chat is None or chat.type != "private"
            or user.is_bot or message.sender_chat is not None):
        return None
    try:
        item = NativeInput(kind, str(bot.id), str(user.id), str(chat.id),
                           str(message.message_thread_id or ""), str(message.message_id),
                           str(update.update_id), user.language_code or "", value)
        # Validation is shared with escrow; no SDK object or rejected value escapes.
        from .onboarding_auth import _native
        return _native(item)
    except (AttributeError, RuntimeError, ValueError):
        return None


def observe_native(broker: AdmissionBroker, update, bot):
    broker.observe(native_input(update, bot, kind="origin", value=""), now=time.monotonic())


async def admit_native(adapter, broker: AdmissionBroker, item: NativeInput):
    from gateway.platforms.event import MessageEvent

    reference = broker.offer(item, now=time.monotonic())
    try:
        source = adapter.build_source(chat_id=item.chat_id, chat_type="dm", user_id=item.user_id,
                                      thread_id=item.thread_id or None, message_id=item.message_id,
                                      scope_id=item.bot_id)
        event = MessageEvent(text=PREFIX + reference, source=source, message_id=item.message_id,
                             raw_message=None, internal=False, allow_gateway_control=False)
        with broker.native_scope(reference):
            await adapter.handle_message(event)
        receipt = await broker.wait(reference)
        if receipt is None:
            return None
        return receipt, broker.take(reference, receipt)
    finally:
        with broker._state.lock:
            broker._discard(reference)


def register_admission(ctx):
    """Task 7 wires this callback alongside the native factory on the real context."""
    broker = acquire_broker()
    ctx.on_unload(broker.close)

    def post_admission(*, platform, source, message_id, session_key, text, **kwargs):
        return on_admission(broker, platform=platform, source=source, message_id=message_id,
                            session_key=session_key, text=text)

    ctx.register_hook("post_gateway_admission", post_admission)
    return broker


def on_admission(broker, *, platform, source, message_id, session_key, text):
    """Shared admission callback, also used by the lazy public registration."""
    from hermes_constants import get_hermes_home
    if str(get_hermes_home().resolve()) != broker.profile_home:
        return None
    broker.admit(platform=platform, source=source, message_id=message_id, session_key=session_key,
                 text=text, now=time.monotonic())
    if not text.startswith(PREFIX):
        broker.record_origin(platform=platform, source=source, message_id=message_id,
                             session_key=session_key, now=time.monotonic())
    # A copied marker is also consumed, but can never release a native input.
    return {"action": "handled"} if text.startswith(PREFIX) else None
