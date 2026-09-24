"""Tests for server/confucius_streaming_server.py.

Two layers, no GPU required:
- Pure unit tests: module import without heavy deps, parser defaults, header
  validation, context resolution (pinned upstream semantics), token file
  handling.
- Real loopback tests (skipped without ``websockets``): the actual server
  coroutines over a real TCP loopback with a fake duck-typed model — hotword
  context passthrough (asserting the real init_streaming_state arguments),
  full EOS flow, auth rejection, bad headers, malformed PCM, backpressure,
  session audio budget, single concurrency, and disconnect cleanup.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

SERVER_PATH = Path(__file__).parent.parent / "server" / "confucius_streaming_server.py"


def _load_server_module():
    spec = importlib.util.spec_from_file_location("confucius_streaming_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def srv():
    return _load_server_module()


# ---------------------------------------------------------------------------
# Pure unit tests
# ---------------------------------------------------------------------------

def test_module_imports_without_model_deps(srv):
    assert srv.active_model is None  # model handle stays lazy


def test_parser_defaults(srv):
    args = srv.build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8321
    assert args.gpu_memory_utilization == 0.80
    assert args.max_model_len == 4096
    assert args.max_session_seconds == 30.0
    assert args.max_queued_frames == 32
    assert args.model_dir  # non-empty default
    assert not args.model_dir.startswith("/home/")  # no machine-specific paths
    assert not args.r2t2_source  # explicit opt-in


def test_parser_overrides(srv):
    args = srv.build_parser().parse_args([
        "--model-dir", "/models/x", "--port", "9000", "--host", "127.0.0.2",
        "--gpu-memory-utilization", "0.7", "--max-model-len", "2048",
        "--max-session-seconds", "10", "--token-file", "/tmp/tok",
    ])
    assert args.model_dir == "/models/x"
    assert args.port == 9000
    assert args.gpu_memory_utilization == 0.7
    assert args.max_session_seconds == 10.0


def test_validate_header_ok(srv):
    header = srv.validate_header({"requestId": "r1", "channels": 1, "sample_rate": 16000})
    assert header["requestId"] == "r1"


@pytest.mark.parametrize("header", [
    "not-a-dict",
    {},
    {"requestId": ""},
    {"requestId": 42},
    {"requestId": "r", "channels": 2},
    {"requestId": "r", "sample_rate": 8000},
    {"requestId": "r", "language": 3},
    {"requestId": "r", "system_prompt": 5},
    {"requestId": "r", "system_prompt": "x" * 4001},
])
def test_validate_header_rejects(srv, header):
    with pytest.raises(srv.HeaderError):
        srv.validate_header(header)


def test_resolve_context_hotwords_pass_whole(srv):
    hotwords = "热词参考: 孔子、孟子"
    ctx = srv.resolve_context({"system_prompt": hotwords})
    assert ctx == hotwords  # hotwords reach the model verbatim


def test_resolve_context_smooth_prefix(srv):
    assert srv.resolve_context({"system_prompt": "abc", "smooth": True}) == "Smooth the text\nabc"
    assert srv.resolve_context({}) == ""


def test_resolve_language(srv):
    assert srv.resolve_language({"language": "zhen"}) is None
    assert srv.resolve_language({"language": "auto"}) is None
    assert srv.resolve_language({}) is None
    assert srv.resolve_language({"language": "Chinese"}) == "Chinese"


def test_token_file_created_0600_never_overwritten(srv, tmp_path):
    path = tmp_path / "tok"
    token = srv.load_or_create_token(str(path))
    assert token
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert srv.load_or_create_token(str(path)) == token  # same token, no overwrite


def test_token_file_empty_refused(srv, tmp_path):
    path = tmp_path / "tok"
    path.write_text("  \n")
    with pytest.raises(ValueError, match="empty"):
        srv.load_or_create_token(str(path))


# ---------------------------------------------------------------------------
# Fake model + real loopback
# ---------------------------------------------------------------------------

class FakeState:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.context = kwargs["context"]
        self.text = ""
        self.chunk_size_sec = 0.16
        self.chunk_size_samples = 2560


class FakeModel:
    """Duck-typed stand-in for R2T2ASRModel; records real init arguments."""

    def __init__(self, infer_delay_s: float = 0.0):
        self.init_calls: list[dict] = []
        self.stream_calls = 0
        self.finish_calls = 0
        self.infer_delay_s = infer_delay_s

    def init_streaming_state(self, **kwargs):
        self.init_calls.append(kwargs)
        return FakeState(kwargs)

    def streaming_transcribe(self, seg, state, max_new_tokens):
        if self.infer_delay_s:
            time.sleep(self.infer_delay_s)
        self.stream_calls += 1
        state.text = state.text + "字"
        return state.text, state.text  # (partial, fixed) — fixed grows by 字

    def finish_streaming_transcribe(self, state, max_new_tokens):
        if self.infer_delay_s:
            time.sleep(self.infer_delay_s)
        self.finish_calls += 1
        state.text = state.text + "。"


class BlockingFakeModel(FakeModel):
    """Model whose streaming_transcribe blocks until released, tracking how
    many concurrent calls are inside the model (to prove no overlap)."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.call_returned = threading.Event()
        self.depth = 0
        self.max_depth = 0
        self._depth_lock = threading.Lock()

    def streaming_transcribe(self, seg, state, max_new_tokens):
        with self._depth_lock:
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
        self.entered.set()
        self.release.wait(timeout=30)
        with self._depth_lock:
            self.depth -= 1
        self.call_returned.set()
        self.stream_calls += 1
        state.text = state.text + "字"
        return state.text, state.text


class FakeWS:
    """Minimal in-memory websocket for driving _handle_connection directly."""

    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed: tuple | None = None
        self.fail_send = False

    async def recv(self):
        return await self.incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise RuntimeError("client-disconnected")
        return item

    async def send(self, data):
        if self.fail_send:
            raise RuntimeError("send-failed-client-gone")
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


try:
    import websockets  # noqa: F401
    import websockets.asyncio.client  # noqa: F401
    import websockets.asyncio.server  # noqa: F401

    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False

pytestmark_loopback = pytest.mark.skipif(
    not _HAS_WEBSOCKETS, reason="websockets not installed (loopback test skipped)"
)

TOKEN = "f" * 32


def _frame(ms: int = 160) -> bytes:
    return b"\x00\x00" * (16000 * ms // 1000)


def _args(srv, tmp_path, **over):
    defaults = {
        "model_dir": "unused", "r2t2_source": "", "host": "127.0.0.1", "port": 0,
        "gpu_memory_utilization": 0.8, "max_model_len": 4096,
        "token_file": str(tmp_path / "tok"), "warmup_wav": "",
        "max_session_seconds": 30.0, "max_queued_frames": 32, "idle_timeout_s": 10.0,
        "ready_file": "",
    }
    defaults.update(over)
    return argparse.Namespace(**defaults)


class RunningServer:
    def __init__(self, srv, args, model):
        self.srv = srv
        self.args = args
        self.model = model
        self.port = None
        self._thread = None

    def __enter__(self):
        import threading

        ready = threading.Event()

        def _run():
            asyncio.set_event_loop(asyncio.new_event_loop())
            loop = asyncio.get_event_loop()

            async def _main():
                from websockets.asyncio.server import serve

                busy_lock = asyncio.Lock()
                async with serve(
                    lambda ws: self.srv._handle_connection(ws, self.model, TOKEN, self.args, busy_lock),
                    self.args.host, 0, ping_interval=None,
                ) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    ready.set()
                    await asyncio.Future()

            loop.run_until_complete(_main())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        assert ready.wait(timeout=10), "server did not start"
        return self

    def __exit__(self, *exc):
        # daemon thread dies with the test process; nothing to join here
        return False


def _sync_client(port, actions, timeout=10):
    """Drive one sync websocket-client session; returns (frames, close_code)."""
    import websocket

    ws = websocket.create_connection(f"ws://127.0.0.1:{port}/asr_stream_api_v1", timeout=timeout)
    frames = []
    close_code = None
    try:
        for action in actions:
            kind = action[0]
            if kind == "send_header":
                ws.send(json.dumps(action[1]))
            elif kind == "send_audio":
                ws.send_binary(action[1])
            elif kind == "send_text":
                ws.send(action[1])
            elif kind == "recv":
                opcode, frame = ws.recv_data_frame(control_frame=True)
                if opcode == 0x8:
                    close_code = int.from_bytes(frame.data[:2], "big") if len(frame.data) >= 2 else 1005
                    break
                data = frame.data
                if isinstance(data, (bytes, bytearray)):
                    data = bytes(data).decode("utf-8")
                frames.append(json.loads(data))
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    return frames, close_code


def _recv_until_close(ws, timeout=10):
    import websocket as _w  # noqa: F401

    frames = []
    close_code = None
    while True:
        opcode, frame = ws.recv_data_frame(control_frame=True)
        if opcode == 0x8:
            close_code = int.from_bytes(frame.data[:2], "big") if len(frame.data) >= 2 else 1005
            break
        data = frame.data
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        frames.append(json.loads(data))
    return frames, close_code


@pytestmark_loopback
def test_loopback_hotwords_reach_model_and_full_eos(srv, tmp_path):
    model = FakeModel()
    args = _args(srv, tmp_path)
    with RunningServer(srv, args, model) as server:
        import websocket

        ws = websocket.create_connection(f"ws://127.0.0.1:{server.port}/asr_stream_api_v1", timeout=10)
        ws.send(json.dumps({
            "requestId": "t1", "channels": 1, "sample_rate": 16000,
            "language": "Chinese", "secret_key": TOKEN,
            "system_prompt": "热词参考: 孔子、孟子",
        }))
        greeting = json.loads(ws.recv())
        assert greeting["status"] == "connected"
        # ~1 s of audio in 160 ms frames, then EOS
        for _ in range(6):
            ws.send_binary(_frame())
        ws.send(srv.EOS_MESSAGE)
        frames, close_code = _recv_until_close(ws)
        ws.close()

    # hotwords really entered the model init, not just the client header
    assert model.init_calls, "model was never initialized"
    init = model.init_calls[0]
    assert init["context"] == "热词参考: 孔子、孟子"
    assert init["language"] == "Chinese"
    assert init["unfixed_token_num"] == 1
    assert init["unfixed_chunk_num"] == 0
    # full protocol: deltas -> final reset -> CLOSE 1000
    assert close_code == 1000
    successes = [f for f in frames if f.get("status") == "success"]
    assert successes and successes[-1]["msg"]["reset"] is True
    text = "".join(f["msg"].get("text", "") for f in successes)
    assert text.endswith("。")  # finish flush committed
    assert model.finish_calls == 1


@pytestmark_loopback
def test_loopback_wrong_key_closed_4401(srv, tmp_path):
    with RunningServer(srv, _args(srv, tmp_path), FakeModel()) as server:
        frames, close_code = _sync_client(server.port, [
            ("send_header", {"requestId": "bad", "secret_key": "wrong"}),
            ("recv",),
        ])
    assert close_code == 4401
    assert frames == []  # no error payload leaks on auth failure


@pytestmark_loopback
def test_loopback_bad_header_1008(srv, tmp_path):
    with RunningServer(srv, _args(srv, tmp_path), FakeModel()) as server:
        frames, close_code = _sync_client(server.port, [
            ("send_header", {"channels": 1}),  # no requestId
            ("recv",),
            ("recv",),
        ])
    assert close_code == 1008
    assert frames and frames[0]["status"] == "error"


@pytestmark_loopback
def test_loopback_odd_bytes_pcm_rejected(srv, tmp_path):
    with RunningServer(srv, _args(srv, tmp_path), FakeModel()) as server:
        frames, close_code = _sync_client(server.port, [
            ("send_header", {"requestId": "r", "secret_key": TOKEN}),
            ("recv",),
            ("send_audio", b"\x00\x00\x00"),  # odd byte count
            ("recv",),
            ("recv",),
        ])
    assert close_code == 1008
    assert any(f.get("status") == "error" for f in frames)


@pytestmark_loopback
def test_loopback_session_audio_budget(srv, tmp_path):
    # 0.3 s budget; client sends ~0.96 s -> explicit error, never silent.
    with RunningServer(srv, _args(srv, tmp_path, max_session_seconds=0.3), FakeModel()) as server:
        actions = [("send_header", {"requestId": "r", "secret_key": TOKEN}), ("recv",)]
        actions += [("send_audio", _frame()) for _ in range(6)]
        actions += [("recv",)] * 12
        frames, close_code = _sync_client(server.port, actions)
    assert close_code == 1008
    assert any("budget" in str(f.get("msg", "")) for f in frames if f.get("status") == "error")


@pytestmark_loopback
def test_loopback_backpressure_queue_full(srv, tmp_path):
    # Slow model (80 ms/chunk) + 4-frame queue + instant blast of 40 frames.
    model = FakeModel(infer_delay_s=0.08)
    args = _args(srv, tmp_path, max_queued_frames=4)
    with RunningServer(srv, args, model) as server:
        actions = [("send_header", {"requestId": "r", "secret_key": TOKEN}), ("recv",)]
        actions += [("send_audio", _frame()) for _ in range(40)]
        actions += [("recv",)] * 60
        frames, close_code = _sync_client(server.port, actions)
    assert close_code == 1008
    assert any("queue full" in str(f.get("msg", "")) for f in frames if f.get("status") == "error")


@pytestmark_loopback
def test_loopback_single_concurrency_and_cleanup(srv, tmp_path):
    import websocket

    model = FakeModel(infer_delay_s=0.25)
    with RunningServer(srv, _args(srv, tmp_path), model) as server:
        # session 1: hold the connection open (no EOS)
        ws1 = websocket.create_connection(f"ws://127.0.0.1:{server.port}/asr_stream_api_v1", timeout=10)
        ws1.send(json.dumps({"requestId": "s1", "secret_key": TOKEN}))
        assert json.loads(ws1.recv())["status"] == "connected"

        # session 2 while busy -> 4429
        _, close_code = _sync_client(server.port, [
            ("send_header", {"requestId": "s2", "secret_key": TOKEN}),
            ("recv",),
        ])
        assert close_code == 4429

        # disconnect session 1 abruptly; server must clean up and accept again
        ws1.close()
        deadline = time.time() + 5
        close_code2 = None
        while time.time() < deadline:
            frames2, close_code2 = _sync_client(server.port, [
                ("send_header", {"requestId": "s3", "secret_key": TOKEN}),
                ("recv",),
            ])
            if frames2 and frames2[0].get("status") == "connected":
                break
            time.sleep(0.1)
        assert frames2 and frames2[0]["status"] == "connected"
        assert close_code2 != 4429


# ---------------------------------------------------------------------------
# r2 regression tests (independent-review counterexamples)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--max-queued-frames", "0"],
    ["--max-queued-frames", "-1"],
    ["--gpu-memory-utilization", "0"],
    ["--gpu-memory-utilization", "1.5"],
    ["--idle-timeout-s", "0"],
    ["--idle-timeout-s", "-5"],
    ["--max-session-seconds", "0"],
    ["--max-model-len", "0"],
    ["--port", "70000"],
])
def test_parser_rejects_invalid_limits(srv, argv):
    with pytest.raises(SystemExit):
        srv.build_parser().parse_args(argv)


def test_validate_config_rejects_programmatic_namespace(srv, tmp_path):
    args = _args(srv, tmp_path, max_queued_frames=0)
    with pytest.raises(ValueError, match="max_queued_frames"):
        srv.validate_config(args)
    # run_server boundary: rejected before the token file / model is created
    with pytest.raises(ValueError, match="max_queued_frames"):
        srv.run_server(args, model=FakeModel())
    assert not (tmp_path / "tok").exists()


def test_validate_config_preserves_valid_overrides(srv, tmp_path):
    args = _args(srv, tmp_path, max_queued_frames=4, gpu_memory_utilization=0.7,
                 max_session_seconds=5.0, idle_timeout_s=3.0, max_model_len=2048)
    srv.validate_config(args)  # must not raise
    assert args.max_queued_frames == 4
    assert args.gpu_memory_utilization == 0.7


def test_warmup_uses_both_language_paths(srv, monkeypatch):
    class RecordingModel:
        def __init__(self):
            self.languages = []

        def init_streaming_state(self, **kwargs):
            self.languages.append(kwargs.get("language"))
            return FakeState(kwargs)

        def streaming_transcribe(self, seg, state, n):
            state.text = "x"
            return "x", "x"

        def finish_streaming_transcribe(self, state, n):
            state.text = "xy"

    # silence path (no warmup wav)
    model = RecordingModel()
    srv._warmup(model, "")
    assert model.languages == ["Chinese", None]

    # wav path with a stubbed soundfile (no real audio file needed)
    import types

    import numpy as np

    fake_sf = types.ModuleType("soundfile")
    fake_sf.read = lambda path, dtype="float32", always_2d=False: (
        np.zeros(1600, dtype=np.float32), 16000)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    model2 = RecordingModel()
    srv._warmup(model2, "unused.wav")
    assert model2.languages == ["Chinese", None]


def test_cancel_during_inference_keeps_model_ownership(srv, tmp_path):
    """cancel() on the handler while run_in_executor inference is in flight:
    the busy lock must stay held until the model thread REALLY returns.
    (Cancelling an asyncio future never stops a Python thread.)"""
    import asyncio as aio

    async def scenario():
        model = BlockingFakeModel()
        lock = aio.Lock()
        ws = FakeWS()
        await ws.incoming.put(json.dumps({
            "requestId": "c1", "channels": 1, "sample_rate": 16000,
            "secret_key": TOKEN, "language": "Chinese",
        }))
        args = _args(srv, tmp_path)
        task = aio.create_task(srv._handle_connection(ws, model, TOKEN, args, lock))
        # wait for greeting, then feed one first chunk (320 ms) -> model blocks
        deadline = time.monotonic() + 5
        while not ws.sent and time.monotonic() < deadline:
            await aio.sleep(0.02)
        assert ws.sent, "no greeting"
        await ws.incoming.put(_frame(320))
        await aio.to_thread(model.entered.wait, 5)
        assert model.entered.is_set()

        task.cancel()
        with pytest.raises(aio.TimeoutError):
            # handler must NOT finish while the model thread is still inside
            await aio.wait_for(aio.shield(task), timeout=0.5)
        assert lock.locked()
        assert not model.call_returned.is_set()

        # a second session is rejected while the first still owns the model
        ws2 = FakeWS()
        await ws2.incoming.put(json.dumps({"requestId": "c2", "secret_key": TOKEN}))
        await srv._handle_connection(ws2, model, TOKEN, args, lock)
        assert ws2.closed and ws2.closed[0] == 4429
        assert model.max_depth == 1  # no concurrent model execution

        model.release.set()
        # the cancelled handler task finishes (as cancelled) once the model
        # thread has really returned and the lock is released
        deadline = time.monotonic() + 10
        while not task.done() and time.monotonic() < deadline:
            await aio.sleep(0.02)
        assert task.done()
        assert task.cancelled()
        assert not lock.locked()
        assert model.call_returned.is_set()

    asyncio.run(scenario())


def test_queue_full_abort_cleanup_terminates(srv, tmp_path):
    """Full queue + client abort: receiver cleanup must not deadlock on a
    blocking queue put; the handler finishes and releases the busy lock."""
    import asyncio as aio

    async def scenario():
        model = BlockingFakeModel()
        lock = aio.Lock()
        ws = FakeWS()
        await ws.incoming.put(json.dumps({
            "requestId": "q1", "channels": 1, "sample_rate": 16000,
            "secret_key": TOKEN, "language": "Chinese",
        }))
        args = _args(srv, tmp_path, max_queued_frames=2)
        task = aio.create_task(srv._handle_connection(ws, model, TOKEN, args, lock))
        deadline = time.monotonic() + 5
        while not ws.sent and time.monotonic() < deadline:
            await aio.sleep(0.02)
        await ws.incoming.put(_frame(320))  # one first chunk -> model blocks
        await aio.to_thread(model.entered.wait, 5)
        for _ in range(6):  # overflow the 2-frame queue while blocked
            await ws.incoming.put(_frame(160))
        await ws.incoming.put(None)  # abrupt client disconnect
        model.release.set()
        # cleanup must terminate on its own — no external queue drain
        await aio.wait_for(aio.shield(task), timeout=10)
        assert not lock.locked()
        # server is usable again
        ws2 = FakeWS()
        await ws2.incoming.put(json.dumps({"requestId": "q2", "secret_key": TOKEN}))
        task2 = aio.create_task(srv._handle_connection(ws2, model, TOKEN, args, lock))
        deadline = time.monotonic() + 5
        while not ws2.sent and time.monotonic() < deadline:
            await aio.sleep(0.02)
        assert any("connected" in s for s in ws2.sent)
        task2.cancel()
        with pytest.raises(aio.CancelledError):
            await task2
        assert not lock.locked()

    asyncio.run(scenario())


@pytestmark_loopback
def test_loopback_aborted_blocked_inference_then_reuse(srv, tmp_path):
    """Real TCP abort while the model is blocked mid-inference: a second
    connection gets 4429 until the old call returns; afterwards the server
    accepts new sessions (lock released only after real completion)."""
    import websocket

    model = BlockingFakeModel()
    args = _args(srv, tmp_path, max_queued_frames=4)
    with RunningServer(srv, args, model) as server:
        ws1 = websocket.create_connection(f"ws://127.0.0.1:{server.port}/asr_stream_api_v1", timeout=10)
        ws1.send(json.dumps({"requestId": "a1", "secret_key": TOKEN}))
        assert json.loads(ws1.recv())["status"] == "connected"
        ws1.send_binary(_frame(320))
        assert model.entered.wait(timeout=5)
        ws1.sock.close()  # abrupt TCP abort, no CLOSE frame

        # while the old inference is still blocked, new sessions are rejected
        _, code = _sync_client(server.port, [
            ("send_header", {"requestId": "a2", "secret_key": TOKEN}), ("recv",)])
        assert code == 4429
        assert model.max_depth == 1

        model.release.set()
        deadline = time.time() + 10
        frames = []
        while time.time() < deadline:
            frames, code = _sync_client(server.port, [
                ("send_header", {"requestId": "a3", "secret_key": TOKEN}), ("recv",)])
            if frames and frames[0].get("status") == "connected":
                break
            time.sleep(0.1)
        assert frames and frames[0]["status"] == "connected"


# ---------------------------------------------------------------------------
# r3 regression tests
# ---------------------------------------------------------------------------

def test_full_queue_with_eos_drops_no_audio(srv, tmp_path):
    """r3 blocker: queue at capacity containing accepted audio + EOS. The old
    in-band stop sentinel dropped the oldest queued audio to make room for
    itself, turning a healthy EOS into silent audio loss. Control now travels
    out-of-band: every accepted frame must reach the model before finish."""
    import asyncio as aio

    async def scenario():
        model = BlockingFakeModel()
        lock = aio.Lock()
        ws = FakeWS()
        await ws.incoming.put(json.dumps({
            "requestId": "e1", "channels": 1, "sample_rate": 16000,
            "secret_key": TOKEN, "language": "Chinese",
        }))
        args = _args(srv, tmp_path, max_queued_frames=2)
        task = aio.create_task(srv._handle_connection(ws, model, TOKEN, args, lock))
        deadline = time.monotonic() + 5
        while not ws.sent and time.monotonic() < deadline:
            await aio.sleep(0.02)
        # frame 1 (320 ms) -> first chunk -> model blocks. frame 2 fills the
        # queue (cap 2). EOS arrives while the queue is FULL.
        await ws.incoming.put(_frame(320))
        await aio.to_thread(model.entered.wait, 5)
        await ws.incoming.put(_frame(160))
        await ws.incoming.put(srv.EOS_MESSAGE)
        await aio.sleep(0.2)  # let the receiver run into the full queue
        model.release.set()
        await aio.wait_for(aio.shield(task), timeout=10)
        return model, ws

    model, ws = asyncio.run(scenario())
    # 480 ms fed in total; every accepted sample must have been processed:
    # first chunk 320 ms decoded in streaming_transcribe, the queued 160 ms
    # tail must reach the model (finish flushes the session buffer through
    # streaming_transcribe when shorter than a chunk).
    assert model.stream_calls == 2  # first chunk + drained tail, none lost
    assert model.finish_calls == 1
    assert ws.closed and ws.closed[0] == 1000
    successes = [json.loads(s) for s in ws.sent
                 if json.loads(s).get("status") == "success"]
    assert successes[-1]["msg"]["reset"] is True
    text = "".join(f["msg"].get("text", "") for f in successes)
    assert text.endswith("。")  # finish flush ran on the full input


def test_default_token_path_uses_xdg_and_creates_private(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = srv.default_token_path()
    assert path == str(tmp_path / "xdg" / "recordian" / "confucius_server_token.txt")
    token = srv.load_or_create_token(path)
    assert token
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "xdg" / "recordian").st_mode) & 0o077 == 0
    # second read preserves the same token (no overwrite)
    assert srv.load_or_create_token(path) == token


def test_default_token_path_home_fallback(srv, tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = srv.default_token_path()
    assert path == str(tmp_path / "home" / ".config" / "recordian" / "confucius_server_token.txt")


def test_explicit_token_path_preserved(srv, tmp_path):
    path = tmp_path / "custom" / "tok"
    token = srv.load_or_create_token(str(path))
    assert srv.load_or_create_token(str(path)) == token
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytestmark_loopback
def test_serve_reports_real_bound_port(srv, tmp_path):
    """--port 0: READY/ready-file must carry the socket's real bound port,
    and that port must actually accept the protocol."""
    import asyncio as aio

    async def scenario():
        import contextlib

        ready = str(tmp_path / "ready.json")
        args = _args(srv, tmp_path, port=0, ready_file=ready)
        task = aio.create_task(srv._serve(args, FakeModel(), TOKEN))
        deadline = time.monotonic() + 10
        while not os.path.exists(ready) and time.monotonic() < deadline:
            await aio.sleep(0.02)
        assert os.path.exists(ready), "ready file not written"
        body = json.loads(open(ready).read())
        assert body["port"] > 0, f"ready file must not say port 0: {body}"
        assert TOKEN not in open(ready).read()

        frames, close_code = await aio.to_thread(_sync_client, body["port"], [
            ("send_header", {"requestId": "p1", "secret_key": TOKEN}),
            ("recv",),
            ("send_audio", _frame(320)),
            ("send_text", srv.EOS_MESSAGE),
            ("recv",), ("recv",), ("recv",), ("recv",),
        ])
        assert frames[0]["status"] == "connected"
        assert close_code == 1000

        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        assert not os.path.exists(ready), "ready file must be removed on exit"

    asyncio.run(scenario())
