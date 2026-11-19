"""Shared RTC SSE/TLV codecs and bounded offline M0 protocol fixtures."""

from __future__ import annotations

import base64
import codecs
import json
from dataclasses import dataclass
from typing import Iterable

MAX_PAYLOAD_BYTES = 65_536


def encode_sse_chunk(text: str, *, stream_id: str, created: int = 0, model: str = "m0-protocol-probe") -> str:
    payload = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def encode_sse_end(*, stream_id: str, created: int = 0, model: str = "m0-protocol-probe") -> str:
    payload = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\ndata: [DONE]\n\n"


def decode_sse(chunks: Iterable[bytes | str]) -> list[dict | str]:
    """Decode bounded SSE fixtures, including byte-fragmented UTF-8 and CRLF."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    data_lines: list[str] = []
    events: list[dict | str] = []
    total = 0
    done = False

    def flush() -> None:
        nonlocal done
        if not data_lines:
            return
        raw = "\n".join(data_lines)
        data_lines.clear()
        if done:
            raise ValueError("SSE data appeared after DONE")
        if raw == "[DONE]":
            done = True
            events.append(raw)
            return
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("SSE JSON must be an object")
        events.append(parsed)

    def consume_line(line: str) -> None:
        if not line:
            flush()
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
        # Comments and other SSE fields do not enter the JSON payload.

    for chunk in chunks:
        total += len(chunk) if isinstance(chunk, bytes) else len(chunk.encode("utf-8"))
        if total > MAX_PAYLOAD_BYTES:
            raise ValueError("SSE fixture exceeds size limit")
        buffer += decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            consume_line(line.removesuffix("\r"))
    buffer += decoder.decode(b"", final=True)
    if buffer:
        consume_line(buffer.removesuffix("\r"))
    flush()
    return events


def decode_rts_message(encoded: str) -> tuple[str, dict]:
    """Decode the documented Base64 + four-byte tag + big-endian length TLV."""
    if not isinstance(encoded, str) or len(encoded) > ((MAX_PAYLOAD_BYTES + 2) // 3) * 4:
        raise ValueError("RTS message exceeds size limit")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) < 8 or len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("Invalid RTS message size")
    tag = raw[:4].decode("ascii")
    if tag not in {"conv", "subv"}:
        raise ValueError("Unsupported RTS message tag")
    declared = int.from_bytes(raw[4:8], "big")
    if declared != len(raw) - 8:
        raise ValueError("RTS declared length mismatch")
    payload = json.loads(raw[8:].decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("RTS payload must be an object")
    return tag, payload


@dataclass(frozen=True)
class ExecutionTarget:
    rtc_run_id: str
    turn_id: str
    epoch: int


class OutputFence:
    """Synthetic single-owner fixture for validating stale-control behavior."""

    def __init__(self) -> None:
        self.target: ExecutionTarget | None = None
        self.connection_id: str | None = None
        self._controls: set[str] = set()
        self._highest_epoch = -1

    def bind(self, target: ExecutionTarget, connection_id: str) -> None:
        if target.epoch < 0 or not target.rtc_run_id or not target.turn_id or not connection_id:
            raise ValueError("Invalid execution target")
        if target.epoch < self._highest_epoch or (target.epoch == self._highest_epoch and target != self.target):
            raise ValueError("A stale or cancelled epoch cannot acquire output ownership")
        self.target = target
        self.connection_id = connection_id
        self._highest_epoch = target.epoch

    def can_emit(self, target: ExecutionTarget, connection_id: str) -> bool:
        return target == self.target and connection_id == self.connection_id

    def interrupt(self, target: ExecutionTarget, control_event_id: str) -> bool:
        if not control_event_id or control_event_id in self._controls or target != self.target:
            return False
        if len(self._controls) >= 1024:
            raise ValueError("Fixture control limit reached")
        self._controls.add(control_event_id)
        self.target = None
        self.connection_id = None
        return True

    def disconnected(self, target: ExecutionTarget, connection_id: str) -> bool:
        if not self.can_emit(target, connection_id):
            return False
        self.target = None
        self.connection_id = None
        return True


def run_protocol_probe() -> dict:
    """Run deterministic synthetic checks, never claim cloud correlation proof."""
    checks: dict[str, bool] = {}
    try:
        wire = (encode_sse_chunk("验证中文🙂", stream_id="synthetic") + encode_sse_end(stream_id="synthetic")).encode()
        parsed = decode_sse(wire[index : index + 1] for index in range(len(wire)))
        checks["fragmented_utf8_sse"] = parsed[0]["choices"][0]["delta"]["content"] == "验证中文🙂"
        checks["single_done_and_stream_id"] = parsed[-1] == "[DONE]" and all(
            item["id"] == "synthetic" for item in parsed if isinstance(item, dict)
        )
        body = json.dumps({"TaskId": "synthetic", "RoundID": 7, "Stage": {"Code": 4}}).encode()
        tag, payload = decode_rts_message(base64.b64encode(b"conv" + len(body).to_bytes(4, "big") + body).decode())
        checks["rts_conv_tlv"] = tag == "conv" and payload["RoundID"] == 7
        old = ExecutionTarget("synthetic-run", "turn-old", 1)
        new = ExecutionTarget("synthetic-run", "turn-new", 2)
        fence = OutputFence()
        fence.bind(old, "connection-old")
        fence.bind(new, "connection-new")
        checks["late_interrupt_is_ignored"] = not fence.interrupt(old, "late-control") and fence.can_emit(new, "connection-new")
        checks["old_disconnect_is_ignored"] = not fence.disconnected(old, "connection-old") and fence.can_emit(new, "connection-new")
        fence.bind(new, "connection-replacement")
        checks["same_turn_old_connection_is_ignored"] = not fence.disconnected(new, "connection-new") and fence.can_emit(new, "connection-replacement")
        checks["matching_interrupt_is_idempotent"] = fence.interrupt(new, "matching-control") and not fence.interrupt(new, "matching-control")
    except (ValueError, KeyError, TypeError, UnicodeError):
        return {"name": "protocol", "status": "failed", "summary": "Offline protocol fixture failed", "evidence": {"synthetic": True, "checks": checks}}
    return {
        "name": "protocol",
        "status": "passed" if all(checks.values()) else "failed",
        "summary": "Offline protocol fixtures passed; cloud round association remains unverified",
        "evidence": {"synthetic": True, "checks": checks, "cloud_round_association": "not_verified"},
    }
