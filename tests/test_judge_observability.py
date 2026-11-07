"""Judge outcome events: every outcome class emits exactly one judge_event.

Observability only — these tests also pin that results (corrections and
non-corrections) are byte-identical to the uninstrumented behavior, and that
a broken logger can never change a correction result.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import pytest
import requests

from recordian import jev_judge, semif_judge, streaming_correction
from recordian import particle_correction as pc
from recordian.judge_observability import emit_judge_event, text_len_bucket
from recordian.semif_judge import SemIfClient, request_choice, request_choices
from recordian.streaming_correction import StreamingHotwordCorrector

_OPTIONS = {"keep": "十七", "c0": "时期"}
_QUESTIONS = {"pick": {"type": "choice", "instructions": "选一个", "criteria": _OPTIONS}}


def _events(caplog: pytest.LogCaptureFixture, site: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "judge_event" in record.getMessage() and f"site={site}" in record.getMessage()
    ]


def _picked(choice: str, keep: float, c0: float) -> dict[str, object]:
    return {"choice": choice, "probabilities": {"keep": keep, "c0": c0}}


class _Response:
    def __init__(self, body: object = None, json_exc: Exception | None = None) -> None:
        self._body = body
        self._json_exc = json_exc

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        if self._json_exc is not None:
            raise self._json_exc
        return self._body


class _Session:
    def __init__(self, response: _Response | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc

    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> _Response:
        del url, json, timeout
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response

    def close(self) -> None:
        return None


def _ok_session(choice: str, keep: float, c0: float) -> _Session:
    return _Session(_Response({"answers": {"pick": _picked(choice, keep, c0)}}))


# --- SemIf single choice -------------------------------------------------


def test_semif_answered_with_correction_event(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        result = request_choice(
            _ok_session("c0", 0.05, 0.95), "http://x/v1/systemone", 0.35,
            "今天十七度", _OPTIONS, instructions="选",
        )
    assert result == "c0"
    events = _events(caplog, "semif_request")
    assert len(events) == 1
    assert "outcome=answered" in events[0]
    assert "correction_applied=true" in events[0]
    assert "timeout_budget_ms=350" in events[0]
    assert "今天十七度" not in events[0]  # text itself is never logged


def test_semif_answered_keep_is_not_a_correction(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        result = request_choice(
            _ok_session("keep", 0.95, 0.05), "http://x/v1/systemone", 0.35,
            "今天十七度", _OPTIONS, instructions="选",
        )
    assert result == "keep"
    events = _events(caplog, "semif_request")
    assert len(events) == 1 and "outcome=answered" in events[0]
    assert "correction_applied=false" in events[0]


def test_semif_gate_rejected_event(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        result = request_choice(
            _ok_session("c0", 0.5, 0.5), "http://x/v1/systemone", 0.35,
            "今天十七度", _OPTIONS, instructions="选",
        )
    assert result is None
    events = _events(caplog, "semif_request")
    assert len(events) == 1 and "outcome=gate_rejected" in events[0]


def test_semif_timeout_event(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        result = request_choice(
            _Session(exc=requests.Timeout()), "http://x/v1/systemone", 0.35,
            "今天十七度", _OPTIONS, instructions="选",
        )
    assert result is None
    events = _events(caplog, "semif_request")
    assert len(events) == 1 and "outcome=timeout" in events[0]


def test_semif_transport_error_event(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        result = request_choice(
            _Session(exc=requests.ConnectionError("down")), "http://x/v1/systemone", 0.35,
            "今天十七度", _OPTIONS, instructions="选",
        )
    assert result is None
    events = _events(caplog, "semif_request")
    assert len(events) == 1 and "outcome=transport_error" in events[0]


def test_semif_payload_error_event(caplog: pytest.LogCaptureFixture) -> None:
    bad = _Session(_Response(json_exc=ValueError("not json")))
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        result = request_choice(bad, "http://x/v1/systemone", 0.35, "今天十七度", _OPTIONS, instructions="选")
    assert result is None
    events = _events(caplog, "semif_request")
    assert len(events) == 1 and "outcome=payload_error" in events[0]


def test_semif_unexpected_exception_reraises_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        with pytest.raises(RuntimeError):
            request_choice(
                _Session(exc=RuntimeError("boom")), "http://x/v1/systemone", 0.35,
                "今天十七度", _OPTIONS, instructions="选",
            )
    events = _events(caplog, "semif_request")
    assert len(events) == 1 and "outcome=exception" in events[0]


def test_semif_guard_skip_event(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        assert request_choice(None, "http://x/v1/systemone", 0.35, "今天十七度", _OPTIONS, instructions="选") is None
    events = _events(caplog, "semif_request")
    assert len(events) == 1 and "outcome=skipped_guard" in events[0]


def test_semif_client_disabled_skip_event(caplog: pytest.LogCaptureFixture) -> None:
    client = SemIfClient("http://x/v1/systemone", enabled=False, session=_Session())
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        assert client.choose("今天十七度", _OPTIONS) is None
    events = _events(caplog, "semif_client")
    assert len(events) == 1 and "outcome=skipped_disabled" in events[0]


# --- SemIf batch ----------------------------------------------------------


def test_semif_batch_answered_and_timeout_events(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.semif_judge"):
        ok = request_choices(_ok_session("c0", 0.05, 0.95), "http://x/v1/systemone", 0.35, "今天十七度", _QUESTIONS)
        down = request_choices(_Session(exc=requests.Timeout()), "http://x/v1/systemone", 0.35, "今天十七度", _QUESTIONS)
    assert ok == {"pick": "c0"}
    assert down == {}
    events = _events(caplog, "semif_request_batch")
    assert len(events) == 2
    assert "outcome=answered" in events[0] and "questions=1" in events[0]
    assert "outcome=timeout" in events[1]


# --- Jev transport ---------------------------------------------------------

_JEV_QUESTIONS = {"pick": {"type": "choice", "instructions": "选", "criteria": {"keep": "k", "c0": "c"}}}


def _jev_script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "fake_jev.py"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return [sys.executable, str(path)]


def _jev_answer_script(choice: str, keep: float, c0: float) -> str:
    return f"""
    import json, sys
    sys.stdin.buffer.read()
    answer = {{"choice": "{choice}", "probabilities": {{"keep": {keep}, "c0": {c0}}}}}
    sys.stdout.write(json.dumps({{"answers": {{"pick": answer}}}}))
    """


def test_jev_answered_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    argv = _jev_script(tmp_path, _jev_answer_script("c0", 0.05, 0.95))
    with caplog.at_level(logging.INFO, logger="recordian.jev_judge"):
        result = jev_judge.request_jev_choices(0.8, "状态文本", _JEV_QUESTIONS, argv=argv)
    assert result == {"pick": "c0"}
    events = _events(caplog, "jev_request")
    assert len(events) == 1 and "outcome=answered" in events[0]
    assert "correction_applied=true" in events[0]


def test_jev_gate_rejected_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    argv = _jev_script(tmp_path, _jev_answer_script("c0", 0.5, 0.5))
    with caplog.at_level(logging.INFO, logger="recordian.jev_judge"):
        result = jev_judge.request_jev_choices(0.8, "状态文本", _JEV_QUESTIONS, argv=argv)
    assert result == {"pick": None}
    events = _events(caplog, "jev_request")
    assert len(events) == 1 and "outcome=gate_rejected" in events[0]


def test_jev_timeout_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    argv = _jev_script(tmp_path, """
    import time
    time.sleep(5)
    """)
    with caplog.at_level(logging.INFO, logger="recordian.jev_judge"):
        result = jev_judge.request_jev_choices(0.3, "状态文本", _JEV_QUESTIONS, argv=argv)
    assert result == {}
    events = _events(caplog, "jev_request")
    assert len(events) == 1 and "outcome=timeout" in events[0]


def test_jev_process_error_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    argv = _jev_script(tmp_path, """
    import sys
    sys.exit(3)
    """)
    with caplog.at_level(logging.INFO, logger="recordian.jev_judge"):
        result = jev_judge.request_jev_choices(0.8, "状态文本", _JEV_QUESTIONS, argv=argv)
    assert result == {}
    events = _events(caplog, "jev_request")
    assert len(events) == 1 and "outcome=process_error" in events[0]


def test_jev_payload_error_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    argv = _jev_script(tmp_path, """
    import sys
    sys.stdout.write("not json at all")
    """)
    with caplog.at_level(logging.INFO, logger="recordian.jev_judge"):
        result = jev_judge.request_jev_choices(0.8, "状态文本", _JEV_QUESTIONS, argv=argv)
    assert result == {}
    events = _events(caplog, "jev_request")
    assert len(events) == 1 and "outcome=payload_error" in events[0]


def test_jev_guard_skip_event(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="recordian.jev_judge"):
        assert jev_judge.request_jev_choices(0, "状态文本", _JEV_QUESTIONS, argv=["/bin/true"]) == {}
    events = _events(caplog, "jev_request")
    assert len(events) == 1 and "outcome=skipped_guard" in events[0]


# --- Corrector-level outcomes ----------------------------------------------

_ALIAS = [{"heard": "jeff", "word": "jev", "meaning": "软件工具"}]

_ROLE_SCRIPT = """
import json, sys
raw = sys.stdin.buffer.read()
data = json.loads(raw)
state = data["state"]
choice = "tool" if "工具" in state else "person"
probs = {"tool": 0.0, "person": 0.0, "unclear": 0.0}
probs[choice] = 0.9
probs["unclear"] = 0.1
answer = {"type": "choice", "choice": choice, "probabilities": probs, "confidence": 0.9}
sys.stdout.write(json.dumps({"answers": {"role": answer}}))
"""

_PERSON_SCRIPT = """
import json, sys
sys.stdin.buffer.read()
probs = {"tool": 0.05, "person": 0.9, "unclear": 0.05}
answer = {"type": "choice", "choice": "person", "probabilities": probs, "confidence": 0.9}
sys.stdout.write(json.dumps({"answers": {"role": answer}}))
"""


def _corrector(tmp_path: Path, body: str) -> StreamingHotwordCorrector:
    return StreamingHotwordCorrector(
        [], endpoint="", timeout_s=0.8, enabled=True, provider="jev",
        contextual_aliases=_ALIAS, jev_argv=_jev_script(tmp_path, body),
    )


def test_corrector_corrected_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    corrector = _corrector(tmp_path, _ROLE_SCRIPT)
    try:
        with caplog.at_level(logging.INFO, logger="recordian.streaming_correction"), \
                caplog.at_level(logging.INFO, logger="recordian.jev_judge"):
            text = "打开jeff工具检查这个项目"
            assert corrector.submit(text) == text
            assert corrector.finish(text) == "打开jev工具检查这个项目"
    finally:
        corrector.close()
    events = _events(caplog, "streaming_corrector")
    assert len(events) == 1 and "outcome=corrected" in events[0]
    assert "correction_applied=true" in events[0]
    assert _events(caplog, "jev_request"), "adapter event must accompany the job event"


def test_corrector_no_change_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    corrector = _corrector(tmp_path, _PERSON_SCRIPT)
    try:
        with caplog.at_level(logging.INFO, logger="recordian.streaming_correction"):
            text = "打开jeff工具检查这个项目"
            assert corrector.submit(text) == text
            assert corrector.finish(text) == text
    finally:
        corrector.close()
    events = _events(caplog, "streaming_corrector")
    assert len(events) == 1 and "outcome=no_change" in events[0]
    assert "correction_applied=false" in events[0]


def test_particle_corrected_and_skipped_events(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def ask(timeout: float, state: str, questions: dict, **options: object) -> dict:
        del timeout, state, options
        return dict.fromkeys(questions, "地")

    monkeypatch.setattr(pc, "request_jev_choices", ask)

    class _Identity:
        def submit(self, text: str) -> str:
            return text

        poll = submit
        finish = submit

    corrector = pc.ParticleCorrector(_Identity(), timeout_s=1.5)  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="recordian.particle_correction"):
        assert corrector.finish("认真仔细的检查，认真仔细的检查。") == "认真仔细地检查，认真仔细的检查。"
        assert corrector.finish("你好") == "你好"
    events = _events(caplog, "particle_corrector")
    assert len(events) == 2
    assert "outcome=corrected" in events[0]
    assert "outcome=skipped_no_questions" in events[1]


# --- Logging failure can never change a result ------------------------------


class _BrokenLogger:
    def info(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("log boom")

    def warning(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("log boom")


def test_emit_swallows_broken_logger() -> None:
    emit_judge_event(
        _BrokenLogger(), site="x", outcome="y", latency_ms=1.0, text_len=5,  # type: ignore[arg-type]
    )


def test_broken_logger_keeps_semif_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semif_judge, "logger", _BrokenLogger())
    result = request_choice(
        _ok_session("c0", 0.05, 0.95), "http://x/v1/systemone", 0.35,
        "今天十七度", _OPTIONS, instructions="选",
    )
    assert result == "c0"


def test_broken_loggers_keep_corrector_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streaming_correction, "logger", _BrokenLogger())
    monkeypatch.setattr(jev_judge, "logger", _BrokenLogger())
    corrector = _corrector(tmp_path, _ROLE_SCRIPT)
    try:
        text = "打开jeff工具检查这个项目"
        assert corrector.submit(text) == text
        assert corrector.finish(text) == "打开jev工具检查这个项目"
    finally:
        corrector.close()


def test_text_len_bucket() -> None:
    assert text_len_bucket(0) == "0"
    assert text_len_bucket(1) == "1-20"
    assert text_len_bucket(20) == "1-20"
    assert text_len_bucket(21) == "21-60"
    assert text_len_bucket(120) == "61-120"
    assert text_len_bucket(121) == "121+"
