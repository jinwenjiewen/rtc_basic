"""The RTC callback boundary; provider history never becomes runtime authority.

M0 observed a numeric ``round_id`` in CustomLLM requests and ``RoundID`` in
``conv`` events. ``UserID`` in those events is the target/client user, not the
agent. Run ownership and callback authentication belong to the HTTP adapter.
"""

from __future__ import annotations

from typing import Any


class ContractError(ValueError):
    """Safe machine code and HTTP status, without reflected request content."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _round_id(value: Any) -> str:
    if isinstance(value, bool):
        raise ContractError("invalid_round_id")
    if isinstance(value, int):
        # Test the range before str(): arbitrarily large JSON integers must
        # not cause Python's integer-to-string limit to escape as a 500.
        if not 0 <= value < 10**128:
            raise ContractError("invalid_round_id")
        return str(value)
    if isinstance(value, str):
        try:
            valid = (
                1 <= len(value.encode("utf-8")) <= 128
                and value == value.strip()
                and all(character.isprintable() for character in value)
            )
        except UnicodeError:
            valid = False
        if valid:
            return value
    raise ContractError("invalid_round_id")


def validate_callback(payload: Any) -> tuple[str, str]:
    """Extract one final user input, ignoring all upstream execution settings.

    Do not search backwards for a user message: when the final message is an
    assistant/tool message, doing so would replay an earlier user input. The
    runtime builds its own trusted instructions and historical messages.
    """
    if not isinstance(payload, dict):
        raise ContractError("invalid_callback_object")
    round_id = _round_id(payload.get("round_id"))
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 128:
        raise ContractError("invalid_callback_messages")
    final = messages[-1]
    if not isinstance(final, dict) or final.get("role") != "user":
        raise ContractError("callback_requires_final_user")
    content = final.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ContractError("invalid_user_text")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeError:
        raise ContractError("invalid_user_text") from None
    if size > 8192:
        raise ContractError("user_text_too_large", status=413)
    return round_id, content


def extract_interrupt(
    tag: Any,
    payload: Any,
    expected_task_id: str,
    expected_user_id: str,
) -> str | None:
    """Return an exactly scoped interrupted round, or no control action.

    A missing, malformed or mismatched field is only an observation; it never
    means "cancel whatever is current". The caller still binds the query run
    to its server-owned session and preserves pre-admission cancellation.
    """
    if tag != "conv" or not isinstance(payload, dict):
        return None
    if not isinstance(expected_task_id, str) or not expected_task_id:
        return None
    if not isinstance(expected_user_id, str) or not expected_user_id:
        return None
    if payload.get("TaskId") != expected_task_id or payload.get("UserID") != expected_user_id:
        return None
    stage = payload.get("Stage")
    code = stage.get("Code") if isinstance(stage, dict) else None
    if not isinstance(code, int) or isinstance(code, bool) or code != 4:
        return None
    try:
        return _round_id(payload.get("RoundID"))
    except ContractError:
        return None
