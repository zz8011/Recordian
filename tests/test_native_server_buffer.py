"""Production server buffering and executor admission, without model/GPU IO."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from test_confucius_server import TOKEN, BlockingFakeModel, FakeModel, FakeWS, _load_server_module


@pytest.fixture
def srv():
    return _load_server_module()


class RecordingModel(FakeModel):
    """Keep model inputs alive to catch reused native output storage."""

    def __init__(self, replies=None):
        super().__init__()
        self.segments = []
        self.prefixes = []
        self.budgets = []
        self.chunk_sizes = []
        self.finish_budgets = []
        self.replies = iter(replies) if replies is not None else None

    def streaming_transcribe(self, seg, state, max_new_tokens):
        self.segments.append(seg)
        self.prefixes.append(np.concatenate(self.segments))
        self.budgets.append(max_new_tokens)
        self.chunk_sizes.append((state.chunk_size_samples, state.chunk_size_sec))
        if self.replies is None:
            return super().streaming_transcribe(seg, state, max_new_tokens)
        self.stream_calls += 1
        partial, fixed = next(self.replies)
        state.text = partial.split("|")[0]
        return partial, fixed

    def finish_streaming_transcribe(self, state, max_new_tokens):
        self.finish_budgets.append(max_new_tokens)
        super().finish_streaming_transcribe(state, max_new_tokens)


def _pcm(samples):
    # Repeat signed extrema and exact binary fractions; PCM is explicitly LE.
    return np.resize(np.array([-32768, -1234, -1, 0, 1, 1234, 32767], dtype="<i2"), samples).tobytes()


@pytest.mark.parametrize("samples", [0, 1, 2559, 2560, 2561, 5119, 5120, 5121, 7680, 7681, 480000])
@pytest.mark.parametrize("split", ["whole", "frames", "odd_sample_frames"])
def test_every_sample_and_inference_prefix_preserved(srv, samples, split):
    model = RecordingModel()
    session = srv.StreamingSession(model, "English", "public test context")
    pcm = _pcm(samples)
    stride = len(pcm) or 1
    if split == "frames":
        stride = 5120
    elif split == "odd_sample_frames":
        stride = 10238  # 5119 complete samples; wire PCM remains even bytes.
    for start in range(0, len(pcm), stride):
        session.feed(pcm[start:start + stride])
        session.process_ready()
    session.finish()

    expected_sizes = []
    remaining = samples
    if remaining >= 5120:
        expected_sizes.append(5120)
        remaining -= 5120
        while remaining >= 2560:
            expected_sizes.append(2560)
            remaining -= 2560
    if remaining:
        expected_sizes.append(remaining)
    assert [len(seg) for seg in model.segments] == expected_sizes
    expected = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    covered = 0
    for size, segment, prefix in zip(expected_sizes, model.segments, model.prefixes, strict=True):
        assert segment.dtype == np.float32
        np.testing.assert_array_equal(segment, expected[covered:covered + size])
        covered += size
        np.testing.assert_array_equal(prefix, expected[:covered])
    assert covered == samples
    assert session.samples_fed == samples
    assert len(session.buffer) == 0
    assert model.finish_budgets == [4]
    assert model.init_calls == [{
        "context": "public test context", "language": "English",
        "unfixed_chunk_num": 0, "unfixed_token_num": 1, "chunk_size_sec": 0.16,
    }]
    full_calls = len(expected_sizes) - bool(remaining)
    assert model.budgets[:full_calls] == ([4] + [2] * (full_calls - 1) if full_calls else [])
    if remaining:
        assert model.budgets[-1] == 4
    if full_calls:
        assert model.chunk_sizes[:full_calls] == [(5120, 0.32)] + [(2560, 0.16)] * (full_calls - 1)


def test_fixed_deltas_and_adaptive_token_budgets_remain_original(srv):
    model = RecordingModel(replies=[
        ("hello|metadata", "he|metadata"),
        ("hello|metadata", "hell|metadata"),
        ("hell|metadata", "he|metadata"),
        ("hello|metadata", "hello|metadata"),
        ("hello|metadata", "hello|metadata"),
        ("hello world|metadata", "hello world|metadata"),
    ])
    session = srv.StreamingSession(model, None, "")
    session.feed(_pcm(5120 + 5 * 2560))
    deltas = session.process_ready()
    assert [delta for delta, _ in deltas] == ["he", "ll", "", "o", "", " world"]
    assert model.budgets == [4, 2, 3, 4, 4, 4]
    assert session.max_new_tokens == 2
    assert session.last_fixed == "hello world"
    assert session.finish()[0] == "。"
    assert model.finish_budgets == [4]


def test_chinese_token_adaptation_keeps_original_floor(srv):
    session = srv.StreamingSession(FakeModel(), "Chinese", "")
    session.total_new_asr_tokens.append("中")
    session._adapt("中文")
    assert session.max_new_tokens == 4
    session._adapt("中文")
    assert session.max_new_tokens == 4


def test_final_odd_sample_tail_reaches_model_in_full(srv):
    model = RecordingModel()
    session = srv.StreamingSession(model, None, "")
    session.feed(_pcm(5120) + b"\x2e\xfb")
    session.process_ready()
    assert len(session.buffer) == 2
    session.finish()
    assert [len(seg) for seg in model.segments] == [5120, 1]
    np.testing.assert_array_equal(model.segments[-1], [-1234 / 32768.0])
    assert session.samples_fed == 5121
    assert len(session.buffer) == 0
    assert model.finish_calls == 1


@pytest.mark.parametrize("seconds,capacity", [
    (30.0, 960000), (0.16, 5120), (0.3200625, 10242), (3600.0, 960000), (1e308, 960000),
])
def test_session_buffer_capacity_is_bounded(srv, seconds, capacity):
    session = srv.StreamingSession(FakeModel(), None, "", max_session_seconds=seconds)
    session.feed(b"\0" * capacity)
    with pytest.raises((ValueError, BufferError, OverflowError)):
        session.feed(b"\0\0")
    assert len(session.buffer) == capacity
    assert session.samples_fed == capacity // 2


def test_default_session_capacity_is_thirty_seconds(srv):
    session = srv.StreamingSession(FakeModel(), None, "")
    session.feed(b"\0" * 960000)
    with pytest.raises((ValueError, BufferError, OverflowError)):
        session.feed(b"\0\0")
    assert len(session.buffer) == 960000
    assert session.samples_fed == 480000


def test_decoded_arrays_survive_buffer_reuse_clear_and_close(srv):
    model = RecordingModel()
    session = srv.StreamingSession(model, None, "")
    session.feed(_pcm(5120))
    session.process_ready()
    expected = np.frombuffer(_pcm(5120), dtype="<i2").astype(np.float32) / 32768.0
    session.feed(b"\xff\x7f" * 2560)
    session.process_ready()
    session.feed(b"\x00\x80")
    session.finish()
    session.close()
    np.testing.assert_array_equal(model.segments[0], expected)
    np.testing.assert_array_equal(model.segments[1], np.full(2560, 32767 / 32768.0, dtype=np.float32))
    np.testing.assert_array_equal(model.segments[2], [-1.0])


async def _drive_connection(srv, monkeypatch, audio, *, seconds=30.0):
    model = RecordingModel()
    ws = FakeWS()
    ws.incoming.put_nowait(json.dumps({"requestId": "buffer-test", "secret_key": TOKEN}))
    for frame in audio:
        ws.incoming.put_nowait(frame)
    ws.incoming.put_nowait(srv.EOS_MESSAGE)
    loop = asyncio.get_running_loop()
    submit = loop.run_in_executor
    submissions = []

    def record_submit(executor, func, *args):
        submissions.append(func.__name__)
        return submit(executor, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", record_submit)
    args = srv.build_parser().parse_args([])
    args.max_session_seconds = seconds
    lock = asyncio.Lock()
    await asyncio.wait_for(srv._handle_connection(ws, model, TOKEN, args, lock), timeout=5)
    assert not lock.locked()
    return model, ws, submissions


@pytest.mark.parametrize("frame_samples,expected_submissions", [
    ([], ["finish"]),
    ([2560], ["finish"]),
    ([2560, 2560], ["process_ready", "finish"]),
    ([1280] * 6, ["process_ready", "process_ready", "finish"]),
    ([5120, 1280, 1280, 1], ["process_ready", "process_ready", "finish"]),
    ([10240], ["process_ready", "finish"]),
])
def test_no_executor_submission_until_a_complete_chunk(srv, monkeypatch, frame_samples, expected_submissions):
    pcm = _pcm(sum(frame_samples))
    audio = []
    offset = 0
    for samples in frame_samples:
        audio.append(pcm[offset:offset + samples * 2])
        offset += samples * 2
    model, ws, submissions = asyncio.run(_drive_connection(srv, monkeypatch, audio))
    assert submissions == expected_submissions
    assert ws.closed[0] == 1000
    messages = [json.loads(frame) for frame in ws.sent]
    assert messages[0]["status"] == "connected"
    assert messages[-1] == {"status": "success", "requestId": "buffer-test", "msg": {"text": "", "reset": True}}
    if pcm:
        np.testing.assert_array_equal(np.concatenate(model.segments), np.frombuffer(pcm, dtype="<i2") / 32768.0)
    assert model.finish_calls == 1


@pytest.mark.parametrize("audio,seconds,code,reason", [
    ([b""], 30.0, 1008, "bad PCM16 frame"),
    ([b"\0"], 30.0, 1008, "bad PCM16 frame"),
    ([b"\0" * 20482], 30.0, 1009, "audio frame too large"),
    ([_pcm(2560), _pcm(1)], 0.16, 1008, "session audio budget exceeded"),
])
def test_admission_errors_do_not_submit_executor_work(srv, monkeypatch, audio, seconds, code, reason):
    model, ws, submissions = asyncio.run(_drive_connection(srv, monkeypatch, audio, seconds=seconds))
    assert submissions == []
    assert ws.closed == (code, reason)
    assert any(json.loads(frame)["status"] == "error" for frame in ws.sent)
    assert model.stream_calls == model.finish_calls == 0


def test_sub_sample_session_budget_can_finish_without_admitting_audio(srv, monkeypatch):
    model, ws, submissions = asyncio.run(_drive_connection(srv, monkeypatch, [], seconds=0.00001))
    assert ws.closed[0] == 1000
    assert submissions == ["finish"]
    assert model.finish_calls == 1


def test_required_native_library_checked_before_model_startup(srv, monkeypatch, tmp_path):
    monkeypatch.setenv("RECORDIAN_NATIVE_CORE", "required")
    monkeypatch.setenv("RECORDIAN_NATIVE_LIBRARY", str(tmp_path / "missing-native.so"))
    startup = []
    monkeypatch.setattr(srv, "load_model", lambda args: startup.append("model") or FakeModel())
    monkeypatch.setattr(srv, "_warmup", lambda model, wav: startup.append("warmup"))

    async def serve(*args):
        startup.append("serve")

    monkeypatch.setattr(srv, "_serve", serve)
    args = srv.build_parser().parse_args(["--token-file", str(tmp_path / "token")])
    with pytest.raises(RuntimeError, match="Rust core unavailable"):
        srv.run_server(args)
    assert startup == []
    assert not (tmp_path / "token").exists()


@pytest.mark.parametrize("fail_after_return", [False, True])
def test_buffer_remains_open_until_cancelled_inference_really_returns(srv, monkeypatch, fail_after_return):
    sessions = []
    original = srv.StreamingSession

    class TrackedSession(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    monkeypatch.setattr(srv, "StreamingSession", TrackedSession)

    class Model(BlockingFakeModel):
        def streaming_transcribe(self, seg, state, max_new_tokens):
            result = super().streaming_transcribe(seg, state, max_new_tokens)
            if fail_after_return:
                raise RuntimeError("model failed after cancellation")
            return result

    async def scenario():
        model = Model()
        ws = FakeWS()
        ws.incoming.put_nowait(json.dumps({"requestId": "cancel-buffer", "secret_key": TOKEN}))
        ws.incoming.put_nowait(_pcm(5120))
        lock = asyncio.Lock()
        args = srv.build_parser().parse_args([])
        task = asyncio.create_task(srv._handle_connection(ws, model, TOKEN, args, lock))
        try:
            # Do not use executor-based polling: this test tracks one real
            # model thread, while the event loop controls its cancellation.
            async def entered():
                while not model.entered.is_set():
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(entered(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
            assert lock.locked()
            assert not model.call_returned.is_set()
            # A prematurely closed native handle must fail this real access.
            assert len(sessions[0].buffer) == 0
            ws2 = FakeWS()
            await srv._handle_connection(ws2, model, TOKEN, args, lock)
            assert ws2.closed[0] == srv.CLOSE_BUSY
        finally:
            model.release.set()
            try:
                await asyncio.wait_for(task, timeout=3)
            except asyncio.CancelledError:
                pass
        assert model.call_returned.is_set()
        assert not lock.locked()
        with pytest.raises(RuntimeError, match="closed"):
            len(sessions[0].buffer)
        # A failed old model must not leave the single-user service busy.
        next_ws = FakeWS()
        next_ws.incoming.put_nowait(json.dumps({"requestId": "after-cancel", "secret_key": TOKEN}))
        next_ws.incoming.put_nowait(srv.EOS_MESSAGE)
        await asyncio.wait_for(srv._handle_connection(next_ws, FakeModel(), TOKEN, args, lock), timeout=2)
        assert json.loads(next_ws.sent[0])["status"] == "connected"
        assert next_ws.closed[0] == 1000
        assert not lock.locked()

    asyncio.run(scenario())


def test_one_hour_admission_streams_beyond_thirty_second_buffer(srv, monkeypatch):
    sessions = []
    original = srv.StreamingSession

    class TrackedSession(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    monkeypatch.setattr(srv, "StreamingSession", TrackedSession)

    async def scenario():
        model = RecordingModel()
        ws = FakeWS()
        ws.incoming.put_nowait(json.dumps({"requestId": "long-budget", "secret_key": TOKEN}))
        args = srv.build_parser().parse_args(["--max-session-seconds", "3600"])
        srv.validate_config(args)
        lock = asyncio.Lock()
        task = asyncio.create_task(srv._handle_connection(ws, model, TOKEN, args, lock))
        pcm = _pcm(496000)  # 31 seconds, longer than the FIFO capacity.

        async def streamed(calls):
            while model.stream_calls < calls:
                if task.done():
                    await task
                    pytest.fail(f"connection ended before {calls} streaming calls")
                await asyncio.sleep(0.001)

        try:
            # Feed legal 640 ms frames, waiting for each batch's inference.
            # Retain the production queue bound; no socket or audio clock.
            for start in range(0, len(pcm), 20480):
                frame = pcm[start:start + 20480]
                ws.incoming.put_nowait(frame)
                admitted_samples = (start + len(frame)) // 2
                calls = 1 + (admitted_samples - 5120) // 2560
                await asyncio.wait_for(streamed(calls), timeout=2)
            ws.incoming.put_nowait(srv.EOS_MESSAGE)
            await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        assert ws.closed[0] == 1000
        assert not lock.locked()
        assert sessions[0].samples_fed == 496000
        np.testing.assert_array_equal(np.concatenate(model.segments), np.frombuffer(pcm, dtype="<i2") / 32768.0)
        assert model.budgets == [4] + [2] * 191 + [4]
        assert [len(segment) for segment in model.segments] == [5120] + [2560] * 191 + [1920]
        assert model.finish_budgets == [4]
        messages = [json.loads(frame) for frame in ws.sent]
        assert sum(frame.get("msg", {}).get("reset", False) for frame in messages if isinstance(frame.get("msg"), dict)) == 1
        with pytest.raises(RuntimeError, match="closed"):
            len(sessions[0].buffer)

    asyncio.run(scenario())
