"""Regression cases for provider response, timeout and config audit findings."""

from __future__ import annotations

import base64
import io
import json
import queue
import subprocess
import sys
import threading
import time
import wave
from contextlib import nullcontext
from shutil import which
from types import ModuleType, SimpleNamespace

import pytest

from recordian.providers.cloud_llm_refiner import CloudLLMRefiner, _IncompleteRefinementError
from recordian.providers.http_cloud import HttpCloudProvider
from recordian.providers.llamacpp_text_refiner import LlamaCppTextRefiner
from recordian.providers.qwen_text_refiner import Qwen3TextRefiner
from recordian.settings_draft import SettingsDraft


class Response:
    status_code = 200

    def __init__(self, *, body=None, events=()):
        self.body = body
        self.events = events

    def json(self):
        return self.body

    def raise_for_status(self):
        pass

    def iter_lines(self, **kwargs):
        yield from (event.encode("utf-8") for event in self.events)

    def close(self):
        pass


def openai_events(chunks, *, reason="stop", done=True):
    events = ["data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}) for chunk in chunks]
    if reason is not None:
        events.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]}))
    if done:
        events.append("data: [DONE]")
    return events


def cloud_response(fmt, chunks, *, reason="stop", done=True):
    if fmt == "openai":
        return Response(events=openai_events(chunks, reason=reason, done=done))
    events = [json.dumps({"message": {"content": chunk}, "done": False}) for chunk in chunks]
    if done:
        events.append(json.dumps({"message": {"content": ""}, "done": True, "done_reason": reason}))
    return Response(events=events)


def partitions(text):
    yield [text]
    yield list(text)
    for split in range(1, len(text)):
        yield [text[:split], text[split:]]


@pytest.mark.parametrize("fmt", ["openai", "ollama"])
def test_cloud_stream_filters_thinking_at_every_split(monkeypatch, fmt):
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt, enable_thinking=False)
    for chunks in partitions("前文<think>不可泄露</think>正文<think>第二次推理</think>尾文"):
        monkeypatch.setattr("requests.post", lambda *a, parts=chunks, **kw: cloud_response(fmt, parts))
        assert "".join(refiner.refine_stream("原文")) == "前文正文尾文"


@pytest.mark.parametrize("fmt", ["openai", "ollama"])
def test_cloud_stream_preserves_plain_text(monkeypatch, fmt):
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt)
    for chunks in partitions("正常正文，含小于符号 a < b。"):
        monkeypatch.setattr("requests.post", lambda *a, parts=chunks, **kw: cloud_response(fmt, parts))
        assert "".join(refiner.refine_stream("原文")) == "正常正文，含小于符号 a < b。"


@pytest.mark.parametrize("fmt", ["openai", "ollama"])
def test_cloud_stream_never_emits_unclosed_thinking(monkeypatch, fmt):
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt)
    monkeypatch.setattr("requests.post", lambda *a, **kw: cloud_response(fmt, list("<think>不可泄露")))
    emitted = []
    with pytest.raises(RuntimeError):
        for piece in refiner.refine_stream("原文"):
            emitted.append(piece)
    assert "不可泄露" not in "".join(emitted)


@pytest.mark.parametrize("fmt", ["openai", "ollama"])
@pytest.mark.parametrize("reason,done", [("length", True), (None, False)])
def test_cloud_stream_rejects_incomplete_result(monkeypatch, fmt, reason, done):
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt)
    monkeypatch.setattr(
        "requests.post", lambda *a, **kw: cloud_response(fmt, ["仅有前半句"], reason=reason, done=done)
    )
    with pytest.raises(RuntimeError):
        list(refiner.refine_stream("完整原文"))


@pytest.mark.parametrize(
    "fmt,body",
    [
        ("openai", {"choices": [{"message": {"content": "半句"}, "finish_reason": "length"}]}),
        ("openai", {"choices": [{"message": {"content": "半句"}}]}),
        ("anthropic", {"content": [{"type": "text", "text": "半句"}], "stop_reason": "max_tokens"}),
        ("ollama", {"message": {"content": "半句"}, "done": True, "done_reason": "length"}),
        ("ollama", {"message": {"content": "半句"}, "done": False}),
    ],
)
def test_cloud_sync_rejects_incomplete_result(monkeypatch, fmt, body):
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt)
    monkeypatch.setattr("requests.post", lambda *a, **kw: Response(body=body))
    with pytest.raises(RuntimeError):
        refiner.refine("完整原文")


@pytest.mark.parametrize(
    "fmt,body",
    [
        ("openai", {"choices": [{"message": {"content": {"wrong": "type"}}, "finish_reason": "stop"}]}),
        ("anthropic", {"content": [{"type": "text", "text": ["wrong"]}], "stop_reason": "end_turn"}),
        ("ollama", {"message": {"content": ["wrong"]}, "done": True, "done_reason": "stop"}),
    ],
)
def test_cloud_sync_rejects_malformed_content(monkeypatch, fmt, body):
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt)
    monkeypatch.setattr("requests.post", lambda *a, **kw: Response(body=body))
    with pytest.raises(RuntimeError):
        refiner.refine("完整原文")


@pytest.mark.parametrize("fmt", ["openai", "ollama"])
def test_cloud_stream_rejects_nontext_content(monkeypatch, fmt):
    if fmt == "openai":
        events = ['data: {"choices":[{"delta":{"content":123},"finish_reason":"stop"}]}']
    else:
        events = ['{"message":{"content":123},"done":true,"done_reason":"stop"}']
    monkeypatch.setattr("requests.post", lambda *a, **kw: Response(events=events))
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt)
    with pytest.raises(RuntimeError):
        list(refiner.refine_stream("完整原文"))


@pytest.mark.parametrize(
    "fmt,body",
    [
        ("openai", {"choices": [{"message": {"content": "正文"}, "finish_reason": "stop"}]}),
        ("anthropic", {"content": [{"type": "text", "text": "正文"}], "stop_reason": "end_turn"}),
        ("ollama", {"message": {"content": "正文"}, "done": True, "done_reason": "stop"}),
    ],
)
def test_cloud_sync_accepts_complete_text(monkeypatch, fmt, body):
    refiner = CloudLLMRefiner("http://example.invalid/v1", "", api_format=fmt)
    monkeypatch.setattr("requests.post", lambda *a, **kw: Response(body=body))
    assert refiner.refine("原文") == "正文"


def test_llamacpp_preserves_paragraphs(monkeypatch):
    refiner = LlamaCppTextRefiner("unused.gguf")
    monkeypatch.setattr(refiner, "_lazy_load", lambda: None)
    monkeypatch.setattr(refiner, "_run_inference", lambda text: "第一段内容。\n\n第二段内容。")
    assert refiner.refine("第一段内容。\n\n第二段内容。") == "第一段内容。\n\n第二段内容。"


def test_llamacpp_timeout_returns_without_waiting_and_bounds_inflight(monkeypatch):
    refiner = LlamaCppTextRefiner("unused.gguf", timeout=0.03)
    monkeypatch.setattr(refiner, "_lazy_load", lambda: None)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def inference(text):
        calls.append(text)
        entered.set()
        try:
            assert release.wait(2.0)
            return "迟到结果"
        finally:
            finished.set()

    monkeypatch.setattr(refiner, "_run_inference", inference)
    # The timer only makes the broken pre-fix implementation terminate.
    timer = threading.Timer(0.5, release.set)
    timer.start()
    try:
        start = time.monotonic()
        assert refiner.refine("第一句原文") == "第一句原文"
        assert time.monotonic() - start < 0.25
        assert entered.is_set()
        for _ in range(10):
            assert refiner.refine("第二句原文") == "第二句原文"
        assert calls == ["第一句原文"]
        assert not finished.is_set()
    finally:
        release.set()
        timer.cancel()
        assert finished.wait(1.0)


def test_llamacpp_accepts_new_task_after_timed_out_native_task_exits(monkeypatch):
    refiner = LlamaCppTextRefiner("unused.gguf", timeout=0.01)
    monkeypatch.setattr(refiner, "_lazy_load", lambda: None)
    calls = []

    def inference(text):
        calls.append(text)
        if len(calls) == 1:
            time.sleep(0.04)
        return "新结果"

    monkeypatch.setattr(refiner, "_run_inference", inference)
    assert refiner.refine("第一句") == "第一句"
    time.sleep(0.06)
    assert refiner.refine("第二句") == "新结果"
    assert calls == ["第一句", "第二句"]


@pytest.mark.parametrize("chat", [True, False])
def test_llamacpp_rejects_token_truncated_result(chat):
    refiner = LlamaCppTextRefiner("unused.gguf", prompt_template="{text}" if chat else None)

    class Llama:
        def create_chat_completion(self, **kwargs):
            return {"choices": [{"message": {"content": "半句"}, "finish_reason": "length"}]}

        def __call__(self, *args, **kwargs):
            return {"choices": [{"text": "半句", "finish_reason": "length"}]}

    refiner._llm = Llama()
    with pytest.raises(RuntimeError):
        refiner.refine("完整原文")


def test_llamacpp_rejects_malformed_response():
    refiner = LlamaCppTextRefiner("unused.gguf")
    refiner._llm = lambda *a, **kw: {"choices": [{"text": {"wrong": "type"}, "finish_reason": "stop"}]}
    with pytest.raises(RuntimeError):
        refiner.refine("完整原文")


def test_llamacpp_completion_does_not_stop_at_paragraph_break():
    refiner = LlamaCppTextRefiner("unused.gguf")

    class Llama:
        def __call__(self, *args, **kwargs):
            text = "第一段。\n\n第二段。"
            if "\n\n" in kwargs["stop"]:
                text = "第一段。"
            return {"choices": [{"text": text, "finish_reason": "stop"}]}

    refiner._llm = Llama()
    assert refiner.refine("完整原文") == "第一段。\n\n第二段。"


def qwen_with_chunks(monkeypatch, chunks, *, tokens=None, output_format="tensor", error=None):
    class Tensor:
        def __init__(self, values):
            self.values = values

        def tolist(self):
            return self.values

        def __iter__(self):
            return iter(self.values)

    class Batch:
        input_ids = Tensor([[1]])
        attention_mask = [[1]]

        def to(self, device):
            return self

    class Tokenizer:
        pad_token_id = 0
        eos_token_id = 2

        def apply_chat_template(self, *args, **kwargs):
            assert kwargs["enable_thinking"] is False
            return "模板"

        def __call__(self, *args, **kwargs):
            return Batch()

        def batch_decode(self, ids, **kwargs):
            return ["".join(chunks)]

    class Streamer:
        def __init__(self, *args, **kwargs):
            self.values = queue.Queue()

        def __iter__(self):
            while True:
                value = self.values.get(timeout=1.0)
                if value is None:
                    return
                yield value

        def end(self):
            self.values.put(None)

        def on_finalized_text(self, text, stream_end=False):
            if text:
                self.values.put(text)
            if stream_end:
                self.end()

    def generate(*args, **kwargs):
        if "streamer" in kwargs:
            for text in chunks:
                kwargs["streamer"].values.put(text)
        if error is not None:
            raise error
        if "streamer" in kwargs:
            kwargs["streamer"].end()
        sequences = Tensor([[1] + ([10, 2] if tokens is None else tokens)])
        if output_format == "object":
            return SimpleNamespace(sequences=sequences)
        if output_format == "mapping":
            return {"sequences": sequences}
        return sequences

    transformers = ModuleType("transformers")
    transformers.TextIteratorStreamer = Streamer
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    torch = ModuleType("torch")
    torch.no_grad = nullcontext
    monkeypatch.setitem(sys.modules, "torch", torch)
    refiner = Qwen3TextRefiner(enable_thinking=False)
    refiner._tokenizer = Tokenizer()
    refiner._model = SimpleNamespace(generate=generate)
    monkeypatch.setattr(refiner, "_max_output_tokens_for_text", lambda text: 3)
    return refiner


def test_qwen_stream_filters_thinking_at_every_split(monkeypatch):
    for chunks in partitions("<think>不可泄露</think>正文"):
        refiner = qwen_with_chunks(monkeypatch, chunks)
        assert "".join(refiner.refine_stream("原文")) == "正文"


def test_qwen_stream_never_emits_unclosed_thinking(monkeypatch):
    refiner = qwen_with_chunks(monkeypatch, list("<think>不可泄露"))
    emitted = []
    with pytest.raises(RuntimeError):
        for text in refiner.refine_stream("原文"):
            emitted.append(text)
    assert "不可泄露" not in "".join(emitted)


@pytest.mark.parametrize("text", ["正常正文", "你好<世界", "a < b"])
def test_qwen_stream_preserves_plain_text(monkeypatch, text):
    refiner = qwen_with_chunks(monkeypatch, list(text))
    assert "".join(refiner.refine_stream("原文")) == text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("output_format", ["tensor", "object", "mapping"])
def test_qwen_rejects_token_budget_without_eos(monkeypatch, stream, output_format):
    refiner = qwen_with_chunks(monkeypatch, ["仅有前半句"], tokens=[10, 11, 12], output_format=output_format)
    with pytest.raises(_IncompleteRefinementError):
        if stream:
            list(refiner.refine_stream("完整原文"))
        else:
            refiner.refine("完整原文")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("output_format", ["tensor", "object", "mapping"])
@pytest.mark.parametrize("tokens", [[10, 2], [10, 11, 2], [10, 2, 0]])
def test_qwen_accepts_eos_in_actual_generation_result(monkeypatch, stream, output_format, tokens):
    refiner = qwen_with_chunks(monkeypatch, ["完整正文"], tokens=tokens, output_format=output_format)
    result = "".join(refiner.refine_stream("原文")) if stream else refiner.refine("原文")
    assert result == "完整正文"


@pytest.mark.parametrize("stream", [False, True])
def test_qwen_accepts_model_generation_config_termination_token(monkeypatch, stream):
    refiner = qwen_with_chunks(monkeypatch, ["完整正文"], tokens=[10, 11, 3])
    generate = refiner._model.generate
    refiner._model.generation_config = SimpleNamespace(eos_token_id=[2, 3])

    def check_eos(*args, **kwargs):
        assert kwargs["eos_token_id"] == [2, 3]
        return generate(*args, **kwargs)

    refiner._model.generate = check_eos
    result = "".join(refiner.refine_stream("原文")) if stream else refiner.refine("原文")
    assert result == "完整正文"


@pytest.mark.parametrize("stream", [False, True])
def test_qwen_generation_error_reaches_caller(monkeypatch, stream):
    refiner = qwen_with_chunks(monkeypatch, ["半句"], error=ValueError("mock generate failed"))
    with pytest.raises(ValueError, match="mock generate failed"):
        if stream:
            list(refiner.refine_stream("完整原文"))
        else:
            refiner.refine("完整原文")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tokens", [[10], [10, 11, 12]])
def test_qwen_requires_eos_when_configured(monkeypatch, stream, tokens):
    refiner = qwen_with_chunks(monkeypatch, ["半句"], tokens=tokens)
    with pytest.raises(_IncompleteRefinementError):
        if stream:
            list(refiner.refine_stream("完整原文"))
        else:
            refiner.refine("完整原文")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tokens,complete", [([10], True), ([10, 11, 12], False)])
def test_qwen_without_eos_checks_generated_token_count(monkeypatch, stream, tokens, complete):
    refiner = qwen_with_chunks(monkeypatch, ["正文"], tokens=tokens)
    refiner._tokenizer.eos_token_id = None
    if complete:
        result = "".join(refiner.refine_stream("原文")) if stream else refiner.refine("原文")
        assert result == "正文"
    else:
        with pytest.raises(_IncompleteRefinementError):
            if stream:
                list(refiner.refine_stream("原文"))
            else:
                refiner.refine("原文")


@pytest.mark.parametrize("stream", [False, True])
def test_qwen_rejects_missing_actual_generation_result(monkeypatch, stream):
    refiner = qwen_with_chunks(monkeypatch, ["正文"])
    generate = refiner._model.generate

    def no_result(*args, **kwargs):
        generate(*args, **kwargs)
        return None

    refiner._model.generate = no_result
    with pytest.raises(_IncompleteRefinementError):
        if stream:
            list(refiner.refine_stream("原文"))
        else:
            refiner.refine("原文")


@pytest.mark.parametrize("stream", [False, True])
def test_qwen_truncation_keeps_complete_asr_in_pipeline(monkeypatch, stream):
    import argparse

    from recordian.postprocess_pipeline import _run_refinement

    refiner = qwen_with_chunks(monkeypatch, ["仅有前半句"], tokens=[10, 11, 12])
    events = []
    original = "完整原文，后半句也必须保留。"
    result, _ = _run_refinement(
        args=argparse.Namespace(
            refine_max_len_llm=800, enable_streaming_refine=stream,
            debug_diagnostics=False, config_path="",
        ),
        refiner=refiner, text=original, effective_hotwords=[],
        refine_postprocess_rule="none", on_state=events.append,
    )
    assert result == original
    assert any("_IncompleteRefinementError" in event.get("message", "") for event in events)
    if stream:
        assert any(event.get("chunk") == "仅有前半句" for event in events)


def asr_stream(monkeypatch, tmp_path, chunks, *, reason="stop", done=True, emitted=None):
    audio = tmp_path / "mock.wav"
    audio.write_bytes(b"mock audio")
    provider = HttpCloudProvider("http://example.invalid/v1/audio/transcriptions")
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response(body={"data": [{"id": "mock"}]}))
    monkeypatch.setattr("requests.post", lambda *a, **kw: Response(events=openai_events(chunks, reason=reason, done=done)))
    pieces = emitted if emitted is not None else []
    for piece in provider.transcribe_file_stream(audio, hotwords=[]):
        pieces.append(piece)
    return "".join(pieces)


def test_http_asr_filters_end_tag_at_every_split(monkeypatch, tmp_path):
    for chunks in partitions("language None<asr_text>你好</asr_text>"):
        assert asr_stream(monkeypatch, tmp_path, chunks) == "你好"


@pytest.mark.parametrize("text", ["你好", "无标签正文" * 30])
def test_http_asr_preserves_untagged_transcript(monkeypatch, tmp_path, text):
    assert asr_stream(monkeypatch, tmp_path, list(text)) == text


@pytest.mark.parametrize("text", [
    "language models are useful for dictation.",
    "language models are useful for dictation. They also help with punctuation and formatting.",
])
def test_http_asr_preserves_language_word_in_tagless_text_at_every_split(monkeypatch, tmp_path, text):
    for chunks in partitions(text):
        assert asr_stream(monkeypatch, tmp_path, chunks) == text


@pytest.mark.parametrize("reason,done", [("length", True), (None, True), (None, False)])
@pytest.mark.parametrize("text", ["language Chinese<asr_te", "正常短正文", "无标签正文" * 30, "<asr_text>正文</asr_text>", "language models are useful for dictation."])
def test_http_asr_rejects_unconfirmed_completion(monkeypatch, tmp_path, reason, done, text):
    with pytest.raises(RuntimeError):
        asr_stream(monkeypatch, tmp_path, list(text), reason=reason, done=done)


@pytest.mark.parametrize("text", ["language Chinese<asr_te", "<asr_te", "language " + "Chinese" * 12 + "<asr_te", "正文" * 40 + "<asr_te"])
def test_http_asr_never_flushes_incomplete_protocol_prefix(monkeypatch, tmp_path, text):
    emitted = []
    with pytest.raises(RuntimeError):
        asr_stream(monkeypatch, tmp_path, list(text), emitted=emitted)
    assert "<asr_te" not in "".join(emitted)
    assert "language " not in "".join(emitted)


@pytest.mark.parametrize("text", [
    "<asr_text>正文",
    "<asr_text>正文</asr_te",
    "<asr_text>第一段</asr_text><asr_text>最后一段",
    "<asr_text>第一段</asr_text><asr_text>最后一段</asr_te",
])
def test_http_asr_rejects_unclosed_tagged_result_at_every_split(monkeypatch, tmp_path, text):
    for chunks in partitions(text):
        with pytest.raises(RuntimeError):
            asr_stream(monkeypatch, tmp_path, chunks)


def test_http_asr_accepts_multiple_closed_tags_at_every_split(monkeypatch, tmp_path):
    text = "language Chinese<asr_text>第一段</asr_text><asr_text>第二段</asr_text>"
    for chunks in partitions(text):
        assert asr_stream(monkeypatch, tmp_path, chunks) == "第一段第二段"


def test_http_asr_unclosed_tag_cancels_bound_pipeline_session_without_replay(monkeypatch, tmp_path):
    from recordian.postprocess_pipeline import _run_asr_streaming_commit

    path = tmp_path / "mock.wav"
    path.write_bytes(b"mock audio")
    provider = HttpCloudProvider("http://example.invalid/v1/audio/transcriptions")
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response(body={"data": [{"id": "mock"}]}))
    monkeypatch.setattr("requests.post", lambda *a, **kw: Response(events=openai_events(list("<asr_text>正文"))))

    def no_replay(*args, **kwargs):
        pytest.fail("failed bound stream must not retry or use a fallback commit")

    monkeypatch.setattr(provider, "transcribe_file", no_replay)
    operations = []
    token = "synthetic-bound-session"

    class Session:
        def update_preedit(self, text):
            operations.append((token, "preedit", text))
            return SimpleNamespace(committed=True)

        def cancel(self):
            operations.append((token, "cancel", ""))

        commit = staticmethod(no_replay)

    class Committer:
        def begin_composition(self, text):
            operations.append((token, "begin", text))
            return Session()

        commit = staticmethod(no_replay)

    events = []
    context = SimpleNamespace(
        provider=provider, audio_path=path, committer=Committer(),
        args=SimpleNamespace(enable_hotword_correction=False),
        normalize_final_text=lambda text: text, on_state=events.append,
    )
    text, _, info = _run_asr_streaming_commit(context=context, effective_hotwords=[], auto_hard_enter=False)
    assert text == "正文"  # Retain the preview for diagnostics, never commit it.
    assert info["committed"] is False and info["outcome"] == "cancelled"
    assert operations[0] == (token, "begin", "")
    assert operations[-1] == (token, "cancel", "")
    assert any(operation == (token, "preedit", "正文") for operation in operations)


@pytest.fixture
def legacy_asr_route(monkeypatch):
    """Real Flask route, guards and WAV decoder; only GPU inference is replaced."""
    import requests

    from server import asr_server

    uploads = []
    calls = []
    token = "synthetic-provider-audit-token"

    class Model:
        max_new_tokens = 64

        def transcribe(self, **kwargs):
            with wave.open(kwargs["audio"], "rb") as wav:
                assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
                frames = wav.getnframes()
                assert 0 < frames <= 16000 * 120
                assert len(wav.readframes(frames + 1)) == frames * 2
            calls.append(kwargs)
            return [SimpleNamespace(text="默认 OGG 识别成功")]

    monkeypatch.setattr(asr_server, "asr_model", Model())
    monkeypatch.setattr(asr_server, "model_name", "audit-fake-model")
    monkeypatch.setattr(asr_server, "auth_token", token)
    client = asr_server.app.test_client()

    def post(url, *, json, headers, timeout):
        assert url == "http://localhost/transcribe"
        uploads.append(base64.b64decode(json["audio_base64"], validate=True))
        reply = client.post("/transcribe", json=json, headers=headers)
        response = requests.Response()
        response.status_code = reply.status_code
        response._content = reply.get_data()
        response.url = url
        return response

    monkeypatch.setattr(requests, "post", post)
    provider = HttpCloudProvider("http://localhost/transcribe", api_key=token)
    return provider, uploads, calls


def write_real_ogg(tmp_path, *, seconds=0.12):
    ffmpeg = which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("real ffmpeg is required for OGG integration")
    path = tmp_path / "recording.ogg"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         f"sine=frequency=440:sample_rate=48000:duration={seconds}",
         "-ac", "2", "-c:a", "libopus", str(path)],
        check=True, capture_output=True, timeout=10,
    )
    assert path.read_bytes().startswith(b"OggS")
    return path


def test_legacy_default_ogg_roundtrip_real_ffmpeg_flask(tmp_path, legacy_asr_route):
    from recordian.arg_parser import build_parser

    assert build_parser().parse_args([]).record_format == "ogg"
    path = write_real_ogg(tmp_path)
    provider, uploads, calls = legacy_asr_route
    result = provider.transcribe_file(path, hotwords=["Recordian"])
    assert result.text == "默认 OGG 识别成功"
    assert uploads[0].startswith(b"RIFF") and uploads[0][8:12] == b"WAVE"
    assert len(calls) == 1


def test_audio_conversion_returns_finite_wav_header_real_ffmpeg(tmp_path):
    from recordian.audio_budget import validate_wav

    path = write_real_ogg(tmp_path)
    provider = HttpCloudProvider("http://localhost/transcribe")
    data, name, mime = provider._prepare_openai_audio_file(path)
    validate_wav(data)
    assert name == "recording.wav" and mime == "audio/wav"
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnframes() == 1920


def test_legacy_converted_oversize_ogg_still_rejected_before_model(tmp_path, legacy_asr_route):
    import requests

    path = write_real_ogg(tmp_path, seconds=121)
    provider, uploads, calls = legacy_asr_route
    with pytest.raises(requests.HTTPError, match="400"):
        provider.transcribe_file(path, hotwords=[])
    with wave.open(io.BytesIO(uploads[0]), "rb") as wav:
        assert wav.getnframes() > 16000 * 120
    assert calls == []


def test_legacy_malformed_audio_still_rejected_before_model(tmp_path, legacy_asr_route):
    import requests

    path = tmp_path / "malformed.ogg"
    path.write_bytes(b"not an audio container")
    provider, _uploads, calls = legacy_asr_route
    with pytest.raises(requests.HTTPError, match="400"):
        provider.transcribe_file(path, hotwords=[])
    assert calls == []


def test_audio_conversion_has_deadline_before_upload(monkeypatch, tmp_path):
    path = tmp_path / "recording.ogg"
    path.write_bytes(b"OggS synthetic input")
    provider = HttpCloudProvider("http://localhost/transcribe", timeout_s=0.05)
    monkeypatch.setattr("recordian.providers.http_cloud.which", lambda name: "/synthetic/ffmpeg")

    def stalled_conversion(cmd, **kwargs):
        assert kwargs.get("timeout") == 0.05
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    def no_upload(*args, **kwargs):
        pytest.fail("conversion failure must not upload an incomplete audio result")

    monkeypatch.setattr("recordian.providers.http_cloud.subprocess.run", stalled_conversion)
    monkeypatch.setattr("requests.post", no_upload)
    with pytest.raises(subprocess.TimeoutExpired):
        provider.transcribe_file(path, hotwords=[])


@pytest.mark.parametrize("value", ["16000.0", "1.6e4"])
def test_settings_accepts_and_persists_integral_numeric_notation(tmp_path, value):
    from recordian.config import ConfigManager

    path = tmp_path / "config.json"
    ConfigManager.save(path, {"sample_rate": 16000})
    draft = SettingsDraft({"sample_rate": "8000"}, {"sample_rate": "8000"})
    draft.set("sample_rate", value)
    assert draft.errors() == {}
    draft.persist(path, apply_now=False)
    assert ConfigManager.load(path)["sample_rate"] == 16000


@pytest.mark.parametrize("value", ["1.5", "NaN", "Inf", "-2", "1e309"])
def test_settings_rejects_invalid_integer(value):
    draft = SettingsDraft({"sample_rate": "16000"}, {"sample_rate": "16000"})
    draft.set("sample_rate", value)
    assert "sample_rate" in draft.errors()


@pytest.mark.parametrize("key", ["remote_paste_token_file", "remote_paste_tls_ca_file"])
def test_auth_path_config_load_save_reload_is_stable(monkeypatch, tmp_path, key):
    from recordian.arg_parser import _parse_args_with_config, _save_runtime_config, build_parser
    from recordian.config import ConfigManager

    directory = tmp_path / "config"
    directory.mkdir()
    config = directory / "hotkey.json"
    ConfigManager.save(config, {key: "auth/file"})
    monkeypatch.setattr(sys, "argv", ["recordian-hotkey-dictate", "--config-path", str(config)])
    monkeypatch.chdir(tmp_path)
    args = _parse_args_with_config(build_parser())
    assert getattr(args, key) == str(directory / "auth/file")
    _save_runtime_config(args)
    assert ConfigManager.load(config)[key] == str(directory / "auth/file")
    monkeypatch.chdir(directory)
    reloaded = _parse_args_with_config(build_parser())
    assert getattr(reloaded, key) == str(directory / "auth/file")


@pytest.mark.parametrize("key", ["remote_paste_token_file", "remote_paste_tls_ca_file"])
def test_auth_path_cli_saves_without_reading_file(monkeypatch, tmp_path, key):
    from recordian.arg_parser import _parse_args_with_config, _save_runtime_config, build_parser
    from recordian.config import ConfigManager

    config = tmp_path / "hotkey.json"
    token = tmp_path / "does-not-exist.token"
    monkeypatch.setattr(
        sys, "argv", ["recordian-hotkey-dictate", "--config-path", str(config), "--" + key.replace("_", "-"), str(token)]
    )
    args = _parse_args_with_config(build_parser())
    _save_runtime_config(args)
    assert ConfigManager.load(config)[key] == str(token)


def test_token_path_normalization_preserves_symlink_for_private_loader_to_reject(tmp_path):
    from recordian.runtime_config import normalize_runtime_config

    target = tmp_path / "target.token"
    target.write_text("synthetic-token")
    link = tmp_path / "link.token"
    link.symlink_to(target)
    result = normalize_runtime_config({"remote_paste_token_file": "link.token"}, config_base_dir=tmp_path)
    assert result["remote_paste_token_file"] == str(link)
