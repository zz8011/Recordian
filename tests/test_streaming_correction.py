import threading
import time

import pytest

from recordian.streaming_correction import StreamingHotwordCorrector


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _Session:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> _Response:
        call = {"url": url, "json": json, "timeout": timeout}
        self.calls.append(call)
        return _Response(self.handler(call))

    def close(self) -> None:
        self.closed = True


def _peaked(choice: str, criteria: dict[str, str]) -> dict[str, object]:
    probabilities = dict.fromkeys(criteria, 0.0)
    probabilities[choice] = 1.0
    return {"answers": {"pick": {"type": "choice", "choice": choice, "probabilities": probabilities}}}


def _corrector(hotwords: list[str], session: _Session | None = None, **kwargs: object) -> StreamingHotwordCorrector:
    return StreamingHotwordCorrector(
        hotwords,
        endpoint=str(kwargs.get("endpoint", "http://192.168.5.111:42032/v1/systemone")),
        timeout_s=float(kwargs.get("timeout_s", 0.12)),
        enabled=bool(kwargs.get("enabled", False)),
        session=session,
    )


def test_explicit_alias_and_english_normalization_are_immediate() -> None:
    corrector = _corrector(["张征→张拯", "OpenClaw"], enabled=True)
    try:
        assert corrector.submit("找张征，用 open claw") == "找张拯，用 OpenClaw"
        assert corrector.poll("找张征，用 open claw") == "找张拯，用 OpenClaw"
    finally:
        corrector.close()


def test_short_pinyin_and_frequency_do_not_replace_when_disabled() -> None:
    session = _Session(lambda call: (_ for _ in ()).throw(AssertionError(call)))
    corrector = _corrector(["时期", "时期", "时期"], session=session, enabled=False)
    try:
        text = "今天十七度，屋里湿气很重"
        assert corrector.submit(text) == text
        assert corrector.poll(text) == text
        assert corrector.finish(text) == text
        assert session.calls == []
    finally:
        corrector.close()


def test_blank_endpoint_does_not_connect() -> None:
    session = _Session(lambda call: (_ for _ in ()).throw(AssertionError(call)))
    corrector = _corrector(["时期"], session=session, endpoint="", enabled=True)
    try:
        assert corrector.submit("屋里湿气很重") == "屋里湿气很重"
        assert session.calls == []
    finally:
        corrector.close()


def test_one_span_one_budget_and_number_stays() -> None:
    pytest.importorskip("pypinyin")

    def handler(call: dict[str, object]) -> dict[str, object]:
        assert call["timeout"] == 0.12
        time.sleep(0.04)
        criteria = call["json"]["questions"]["pick"]["criteria"]
        assert list(criteria) == ["keep", "c0"]
        assert "保持原文" in criteria["keep"]
        assert "应把石器替换为时期" in criteria["c0"]
        return _peaked("c0", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.12)
    try:
        started = time.monotonic()
        assert corrector.submit("今天十七度，桌上有石器和湿气") == "今天十七度，桌上有石器和湿气"
        finished = corrector.finish("今天十七度，桌上有石器和湿气")
        elapsed = time.monotonic() - started
        assert finished == "今天十七度，桌上有时期和湿气"
        assert len(session.calls) == 1
        assert elapsed < 0.12 + 0.2
        assert corrector.poll("今天十七度，桌上有石器和湿气") == finished
    finally:
        corrector.close()


def test_invalid_choice_keeps_surface() -> None:
    pytest.importorskip("pypinyin")

    def handler(call: dict[str, object]) -> dict[str, object]:
        del call
        return {"answers": {"pick": {"choice": "c0", "probabilities": {}}}}

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.2)
    try:
        text = "屋里湿气很重"
        corrector.submit(text)
        assert corrector.finish(text) == text
    finally:
        corrector.close()


def test_stale_result_does_not_touch_the_next_sentence() -> None:
    pytest.importorskip("pypinyin")
    started = threading.Event()
    release = threading.Event()

    def handler(call: dict[str, object]) -> dict[str, object]:
        if not started.is_set():
            started.set()
            release.wait(2)
        criteria = call["json"]["questions"]["pick"]["criteria"]
        return _peaked("c0", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期", "Codex"], session=session, enabled=True, timeout_s=0.3)
    try:
        first = "桌上有石器"
        assert corrector.submit(first) == first
        assert started.wait(1)
        began = time.monotonic()
        assert corrector.submit("打开 CodeX") == "打开 Codex"
        corrector.cancel()
        assert time.monotonic() - began < 0.1
        release.set()
        assert corrector.poll(first) == first
        assert corrector.poll("打开 CodeX") == "打开 Codex"
        later = "屋里湿气很重"
        assert corrector.submit(later) == later
        assert corrector.finish(later) == "屋里时期很重"
        assert corrector.poll(first) == first
    finally:
        release.set()
        corrector.close()


def test_finish_timeout_abandons_late_result() -> None:
    pytest.importorskip("pypinyin")
    release = threading.Event()

    def handler(call: dict[str, object]) -> dict[str, object]:
        release.wait(2)
        criteria = call["json"]["questions"]["pick"]["criteria"]
        return _peaked("c0", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.08)
    try:
        text = "屋里湿气很重"
        corrector.submit(text)
        started = time.monotonic()
        assert corrector.finish(text) == text
        assert time.monotonic() - started < 0.25
        release.set()
        time.sleep(0.05)
        assert corrector.poll(text) == text
    finally:
        release.set()
        corrector.close()


def test_only_one_request_is_in_flight() -> None:
    pytest.importorskip("pypinyin")
    release = threading.Event()
    entered = threading.Event()

    def handler(call: dict[str, object]) -> dict[str, object]:
        if len(session.calls) == 1:
            entered.set()
            release.wait(2)
        criteria = call["json"]["questions"]["pick"]["criteria"]
        return _peaked("keep", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.3)
    try:
        first = corrector.submit("桌上有石器")
        second_started = time.monotonic()
        second = corrector.submit("屋里湿气很重")
        assert time.monotonic() - second_started < 0.05
        assert first == "桌上有石器"
        assert second == "屋里湿气很重"
        assert entered.wait(1)
        assert len(session.calls) == 1
        release.set()
        assert corrector.finish("屋里湿气很重") == "屋里湿气很重"
        assert corrector.poll("桌上有石器") == "桌上有石器"
    finally:
        release.set()
        corrector.close()


def test_edit_distance_is_not_a_streaming_correction() -> None:
    corrector = _corrector(["github"], enabled=False)
    try:
        assert corrector.submit("把代码提交到 Githup 上") == "把代码提交到 Githup 上"
    finally:
        corrector.close()


def test_hotword_cap_and_cache_cap() -> None:
    pytest.importorskip("pypinyin")
    plains = [f"术语{chr(0x4E00 + index)}" for index in range(8)]
    corrector = _corrector([*plains, "错甲→正甲", "OpenClaw"], enabled=False)
    try:
        assert corrector._thread is None
        assert corrector.submit("错甲，用 open claw") == "正甲，用 OpenClaw"
        assert corrector._thread is None
    finally:
        corrector.close()

    pairs = [f"错{chr(0x5000 + index)}→正{chr(0x5000 + index)}" for index in range(65)]
    corrector = _corrector(pairs, enabled=False)
    try:
        first = f"错{chr(0x5000)}"
        dropped = f"错{chr(0x5000 + 64)}"
        assert corrector.submit(first + dropped) == f"正{chr(0x5000)}" + dropped
    finally:
        corrector.close()

    def handler(call: dict[str, object]) -> dict[str, object]:
        criteria = call["json"]["questions"]["pick"]["criteria"]
        return _peaked("keep", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.2)
    try:
        for index in range(34):
            text = f"第{index}句湿气"
            corrector.submit(text)
            corrector.finish(text)
        assert len(corrector._cache) <= 32
    finally:
        corrector.close()


def test_same_snapshot_submit_is_idempotent_and_keeps_ready_text() -> None:
    pytest.importorskip("pypinyin")

    def handler(call: dict[str, object]) -> dict[str, object]:
        criteria = call["json"]["questions"]["pick"]["criteria"]
        return _peaked("c0", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.3)
    try:
        text = "桌上有石器"
        assert corrector.submit(text) == text
        assert corrector.submit(text) == text
        assert len(session.calls) == 1
        assert corrector.finish(text) == "桌上有时期"
        assert corrector.submit(text) == text
        assert len(session.calls) == 1
        assert corrector.poll(text) == "桌上有时期"
    finally:
        corrector.close()


def test_finish_without_submit_uses_one_budget() -> None:
    pytest.importorskip("pypinyin")

    def handler(call: dict[str, object]) -> dict[str, object]:
        assert call["timeout"] == 0.12
        criteria = call["json"]["questions"]["pick"]["criteria"]
        assert len(criteria) <= 8
        assert "keep" in criteria
        return _peaked("keep", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.12)
    try:
        assert corrector._thread is None
        started = time.monotonic()
        assert corrector.finish("今天十七度，桌上有石器和湿气") == "今天十七度，桌上有石器和湿气"
        assert time.monotonic() - started < 0.12 + 0.2
        assert len(session.calls) == 1
        posted = session.calls[0]["json"]["questions"]["pick"]["criteria"]
        assert "保持原文" in posted["keep"]
        assert "石器" in posted["keep"]
    finally:
        corrector.close()
        assert time.monotonic() >= 0


def test_close_does_not_wait_out_a_slow_request() -> None:
    pytest.importorskip("pypinyin")
    release = threading.Event()
    entered = threading.Event()

    def handler(call: dict[str, object]) -> dict[str, object]:
        entered.set()
        release.wait(2)
        criteria = call["json"]["questions"]["pick"]["criteria"]
        return _peaked("c0", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.3)
    try:
        corrector.submit("桌上有石器")
        assert entered.wait(1)
        started = time.monotonic()
        corrector.close()
        assert time.monotonic() - started < 0.05
    finally:
        release.set()


def test_judge_criteria_are_sentences_and_weak_choice_keeps_span() -> None:
    pytest.importorskip("pypinyin")
    seen: list[dict[str, str]] = []

    def handler(call: dict[str, object]) -> dict[str, object]:
        criteria = call["json"]["questions"]["pick"]["criteria"]
        seen.append(criteria)
        assert criteria["keep"] != "登陆"
        assert "保持原文" in criteria["keep"]
        assert "台风今晚在福建登陆" in criteria["keep"]
        assert "替换为" in criteria["c0"]
        assert "台风今晚在福建登录" in criteria["c0"]
        return {
            "answers": {
                "pick": {
                    "choice": "c0",
                    "probabilities": {"keep": 0.47, "c0": 0.53},
                }
            }
        }

    session = _Session(handler)
    corrector = _corrector(["登录"], session=session, enabled=True, timeout_s=0.3)
    try:
        text = "台风今晚在福建登陆"
        assert corrector.finish(text) == text
        assert seen
    finally:
        corrector.close()


def test_ready_cache_survives_a_sentence_in_between() -> None:
    pytest.importorskip("pypinyin")

    def handler(call: dict[str, object]) -> dict[str, object]:
        criteria = call["json"]["questions"]["pick"]["criteria"]
        return _peaked("c0", criteria)

    session = _Session(handler)
    corrector = _corrector(["时期"], session=session, enabled=True, timeout_s=0.3)
    try:
        first = "桌上有石器"
        second = "屋里湿气很重"
        assert corrector.finish(first) == "桌上有时期"
        assert corrector.finish(second) == "屋里时期很重"
        assert len(session.calls) == 2
        assert corrector.submit(first) == first
        assert corrector.poll(first) == "桌上有时期"
        assert len(session.calls) == 2
    finally:
        corrector.close()


def test_disabled_import_does_not_need_requests() -> None:
    import os
    import subprocess
    import sys

    script = """
import sys
class _Block:
    def find_spec(self, fullname, path, target=None):
        if fullname == "requests" or fullname.startswith("requests."):
            raise ModuleNotFoundError("requests blocked")
        return None
sys.meta_path.insert(0, _Block())
from recordian.streaming_correction import StreamingHotwordCorrector
from recordian.semif_judge import SemIfClient, trim_new_piece
corrector = StreamingHotwordCorrector(["Codex", "时期"], enabled=False, endpoint="http://127.0.0.1:9")
assert corrector.submit("打开 CodeX") == "打开 Codex"
assert corrector.finish("屋里湿气很重") == "屋里湿气很重"
assert corrector._thread is None
corrector.close()
client = SemIfClient("http://127.0.0.1:9", enabled=False)
assert client.choose("屋里湿气", {"keep": "湿气", "c0": "时期"}) is None
assert trim_new_piece("现在", "流逝") == "流逝"
raised = False
try:
    StreamingHotwordCorrector(
        ["时期"], endpoint="http://127.0.0.1:9", enabled=True, timeout_s=0.2
    ).finish("屋里湿气很重")
except ImportError as exc:
    raised = "requests" in str(exc)
assert raised, "enabled path must report a missing requests install"
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_concurrent_submit_poll_cancel() -> None:
    corrector = _corrector(["Codex", "时期"], enabled=False)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for index in range(15):
                text = f"第{index}次打开 CodeX"
                corrected = corrector.submit(text)
                assert corrected == f"第{index}次打开 Codex"
                assert corrector.poll(text) == corrected
                assert corrector.finish("屋里湿气很重") == "屋里湿气很重"
                if index == 7:
                    corrector.cancel()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    corrector.close()
    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
