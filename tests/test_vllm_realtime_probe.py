from __future__ import annotations

import base64

import numpy as np

from recordian.vllm_realtime_probe import (
    _build_append_event,
    _build_client_frame,
    _build_commit_event,
    _build_session_update,
    _compute_websocket_accept,
    _decode_close_payload,
    _float_to_pcm16le,
    normalize_realtime_url,
)


def test_normalize_realtime_url_appends_path_and_upgrades_scheme() -> None:
    assert normalize_realtime_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/v1/realtime"
    assert normalize_realtime_url("https://demo.local/base") == "wss://demo.local/base/v1/realtime"
    assert normalize_realtime_url("ws://localhost:9000/v1/realtime") == "ws://localhost:9000/v1/realtime"


def test_float_to_pcm16le_and_append_event_roundtrip() -> None:
    samples = np.array([0.0, 0.5, -1.0], dtype=np.float32)
    pcm = _float_to_pcm16le(samples)
    assert pcm == b"\x00\x00\xff?\x01\x80"

    event = _build_append_event(samples)
    assert event["type"] == "input_audio_buffer.append"
    assert base64.b64decode(event["audio"]) == pcm


def test_build_client_frame_masks_payload() -> None:
    payload = b"hello"
    frame = _build_client_frame(0x1, payload)

    assert frame[0] == 0x81
    assert frame[1] & 0x80
    assert frame[1] & 0x7F == len(payload)

    mask = frame[2:6]
    masked_payload = frame[6:]
    unmasked = bytes(value ^ mask[index % 4] for index, value in enumerate(masked_payload))
    assert unmasked == payload


def test_session_and_commit_event_shape() -> None:
    assert _build_session_update("Qwen3-ASR-0.6B") == {
        "type": "session.update",
        "model": "Qwen3-ASR-0.6B",
    }
    assert _build_commit_event(final=False) == {
        "type": "input_audio_buffer.commit",
        "final": False,
    }


def test_accept_and_close_payload_helpers() -> None:
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    assert _compute_websocket_accept(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert _decode_close_payload(b"\x03\xe8bye") == (1000, "bye")
