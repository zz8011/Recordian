"""adapter/confucius_openai_adapter.py：用假 Confucius WS 服务跑通 HTTP multipart -> WS 协议 -> {"text"}。"""

import importlib.util
import io
import json
import shutil
import threading
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.sync.server import serve

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
SPEC = importlib.util.spec_from_file_location(
    "confucius_openai_adapter", Path(__file__).parent.parent / "adapter" / "confucius_openai_adapter.py"
)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)  # 故意非 16k，验证 ffmpeg 归一
        w.writeframes(b"\x01\x00" * int(8000 * seconds))
    return buf.getvalue()


def _multipart(audio: bytes, **fields: str) -> tuple[bytes, str]:
    b = "XBOUNDX"
    out = b""
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'.encode()
    out += b"Content-Type: audio/wav\r\n\r\n" + audio + f"\r\n--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


@pytest.fixture
def stack(tmp_path, monkeypatch):
    seen = {"headers": [], "bytes": [], "eos": 0}
    state = {"busy": False}

    def fake_confucius(ws):
        if state["busy"]:
            ws.close(4429)
            return
        seen["headers"].append(json.loads(ws.recv()))
        ws.send(json.dumps({"status": "connected"}))
        n = 0
        for m in ws:
            if m == adapter.EOS:
                seen["eos"] += 1
                break
            n += len(m)
        seen["bytes"].append(n)
        ws.send(json.dumps({"status": "success", "msg": {"text": f"seg{len(seen['bytes'])}", "reset": False}}))
        ws.send(json.dumps({"status": "success", "msg": {"text": "", "reset": True}}))

    ws_srv = serve(fake_confucius, "127.0.0.1", 0)
    threading.Thread(target=ws_srv.serve_forever, daemon=True).start()
    token = tmp_path / "tok"
    token.write_text("secret-token\n")
    monkeypatch.setattr(adapter, "WS_URL", f"ws://127.0.0.1:{ws_srv.socket.getsockname()[1]}/x")
    monkeypatch.setattr(adapter, "TOKEN_FILE", token)
    monkeypatch.setattr(adapter.time, "sleep", lambda s: None)
    http = ThreadingHTTPServer(("127.0.0.1", 0), adapter.Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{http.server_address[1]}", seen, state
    http.shutdown()
    ws_srv.shutdown()


def _post(base, audio, **fields):
    body, ctype = _multipart(audio, **fields)
    req = urllib.request.Request(base + "/v1/audio/transcriptions", body, {"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_models(stack):
    with urllib.request.urlopen(stack[0] + "/v1/models") as r:
        assert json.load(r)["data"][0]["id"] == adapter.MODEL_ID


def test_transcribe_normalizes_and_sends_protocol(stack):
    base, seen, _ = stack
    status, body = _post(base, _wav(1.0), language="zh", model="whatever")
    assert (status, body) == (200, {"text": "seg1"})
    h = seen["headers"][0]
    assert h["secret_key"] == "secret-token" and h["language"] == "Chinese" and h["sample_rate"] == 16000
    assert seen["bytes"] == [16000 * 2] and seen["eos"] == 1  # 8k -> 16k mono s16le，1s


def test_long_audio_is_segmented(stack):
    base, seen, _ = stack
    status, body = _post(base, _wav(60.0))
    assert (status, body) == (200, {"text": "seg1 seg2 seg3"})  # 28+28+4s，ASCII 分段补空格
    assert all(n <= adapter.SEGMENT_S * 32000 for n in seen["bytes"])


def test_errors(stack):
    base, _, state = stack
    assert _post(base, b"not audio")[0] == 400
    state["busy"] = True
    assert _post(base, _wav(0.5))[0] == 503
