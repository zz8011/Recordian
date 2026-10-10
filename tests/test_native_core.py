"""Exercise the compiled ABI through the production Python adapter."""

import importlib.util
import math
import struct
from pathlib import Path

import numpy as np
import pytest


def test_native_adapter_is_available():
    assert importlib.util.find_spec("recordian.native_core") is not None


@pytest.fixture
def native(monkeypatch):
    from recordian import native_core

    library = Path(__file__).resolve().parents[1] / "native/recordian-core/target/release/librecordian_core.so"
    if not library.exists():
        pytest.skip("build native/recordian-core in release mode to exercise the ABI")
    monkeypatch.setenv("RECORDIAN_NATIVE_LIBRARY", str(library))
    monkeypatch.setenv("RECORDIAN_NATIVE_CORE", "required")
    assert native_core.load_library() is not None
    return native_core


def test_pcm_quantization_preserves_edges_and_raw_rms(native):
    raw = struct.pack("<8f", 0, 0.5, -0.5, 1, -1, math.nan, math.inf, -math.inf)
    pcm, level = native.convert_audio(raw)
    assert struct.unpack("<8h", pcm) == (0, 16384, -16384, 32767, -32767, 0, 32767, -32767)
    assert math.isnan(level)
    assert native.audio_rms(struct.pack("<2f", 2, -2)) == 2
    assert native.audio_rms(struct.pack("<2f", 0.5, -0.5) + b"123") == 0.5
    assert native.convert_audio(b"") == (b"", 0)
    with pytest.raises(ValueError):
        native.convert_audio(b"123")


def test_native_buffer_wraps_preserves_output_ownership_and_rejects_overflow(native):
    with native.PcmBuffer(6) as buffer:
        buffer.feed(struct.pack("<2h", 16384, -16384))
        first = buffer.pop_float(1)
        buffer.feed(struct.pack("<2h", 32767, -32768))
        assert len(buffer) == 6
        with pytest.raises(ValueError):
            buffer.feed(b"\x00\x00")
        assert len(buffer) == 6
        rest = buffer.pop_float(3)
        np.testing.assert_array_equal(rest, np.array([-0.5, 32767 / 32768, -1], dtype=np.float32))
        np.testing.assert_array_equal(first, [0.5])
        assert len(buffer) == 0
        with pytest.raises(ValueError):
            buffer.feed(b"1")
        with pytest.raises(ValueError):
            buffer.pop_float(1)
    with pytest.raises(RuntimeError):
        buffer.feed(b"\x00\x00")


def test_native_conversion_matches_python_on_random_samples(native, monkeypatch):
    from recordian.providers.confucius_asr import f32le_to_pcm16le

    raw = np.random.default_rng(7).uniform(-1.1, 1.1, 10001).astype("<f4").tobytes()
    got, _ = native.convert_audio(raw)
    monkeypatch.setenv("RECORDIAN_NATIVE_CORE", "python")
    assert got == f32le_to_pcm16le(raw)


def test_float32_to_pcm16le_is_byte_identical_on_native_and_python_paths(native, monkeypatch):
    from recordian.providers.confucius_asr import f32le_to_pcm16le

    # 舍入边界（±0.5 LSB）、削波、非有限值与随机样本一起比较，三条路径必须逐字节一致。
    edges = [0.0, 0.5 / 32767, -0.5 / 32767, 1.5 / 32767, -1.5 / 32767, 0.5, -0.5, 1.0, -1.0, 1.2, -1.2, math.nan, math.inf, -math.inf]
    samples = np.concatenate(
        [np.array(edges, dtype="<f4"), np.random.default_rng(11).uniform(-1.1, 1.1, 10001).astype("<f4")]
    )
    got = native.float32_to_pcm16le(samples)
    monkeypatch.setenv("RECORDIAN_NATIVE_CORE", "python")
    assert native.float32_to_pcm16le(samples) == got
    assert got == f32le_to_pcm16le(samples.tobytes())
    assert native.float32_to_pcm16le(np.empty(0, dtype="<f4")) == b""


def test_python_buffer_fallback_is_bounded_and_equivalent(monkeypatch):
    from recordian.native_core import PcmBuffer

    monkeypatch.setenv("RECORDIAN_NATIVE_CORE", "python")
    with PcmBuffer(4) as buffer:
        buffer.feed(struct.pack("<2h", 16384, -32768))
        with pytest.raises(ValueError):
            buffer.feed(b"\x00\x00")
        np.testing.assert_array_equal(buffer.pop_float(2), [0.5, -1.0])


def test_required_mode_fails_before_processing_and_python_mode_is_explicit(monkeypatch, tmp_path):
    from recordian.native_core import load_library

    monkeypatch.setenv("RECORDIAN_NATIVE_LIBRARY", str(tmp_path / "missing.so"))
    monkeypatch.setenv("RECORDIAN_NATIVE_CORE", "required")
    with pytest.raises(RuntimeError):
        load_library()
    monkeypatch.setenv("RECORDIAN_NATIVE_CORE", "python")
    assert load_library() is None
