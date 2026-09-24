import sys

import requests

from recordian import semif_judge
from recordian.semif_judge import (
    SemIfClient,
    choose_continuation,
    interpret_choice,
    trim_new_piece,
)

_OPTIONS = {"keep": "十七", "c0": "时期"}


def _picked(choice: str, probabilities: dict[str, float]) -> dict[str, object]:
    return {"choice": choice, "probabilities": probabilities}


def test_missing_distribution_does_not_accept_choice() -> None:
    assert interpret_choice(_OPTIONS, {"choice": "c0"}) is None
    assert interpret_choice(_OPTIONS, {"choice": "c0", "probabilities": {}}) is None
    assert interpret_choice(_OPTIONS, {"choice": "c0", "probabilities": None}) is None


def test_invalid_distributions_keep_original() -> None:
    assert interpret_choice(_OPTIONS, _picked("other", {"keep": 0.2, "c0": 0.8})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": float("nan"), "c0": 1.0})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.0, "c0": float("inf")})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.9, "c0": 0.9})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.8, "c0": 0.2})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.5, "c0": 0.5})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.49, "c0": 0.51})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.40, "c0": 0.60})) is None
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.10, "c0": 0.90})) == "c0"
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.4, "c0": 0.6, "extra": 0.0})) is None
    assert interpret_choice(_OPTIONS, _picked("keep", {"keep": 0.93, "c0": 0.07})) == "keep"
    assert interpret_choice(_OPTIONS, _picked("c0", {"keep": 0.0, "c0": 1.0})) == "c0"


def test_trim_new_piece_does_not_drop_or_connect() -> None:
    class _Session:
        def post(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("network")

        def close(self) -> None:
            return None

    assert trim_new_piece("现在可以", "流逝") == "流逝"
    assert choose_continuation("现在可以", "流逝", "流式", session=_Session()) is None


def test_disabled_or_blank_endpoint_does_not_post() -> None:
    class _Session:
        def post(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("network")

        def close(self) -> None:
            return None

    session = _Session()
    off = SemIfClient("http://192.168.5.111:42032/v1/systemone", enabled=False, session=session)
    blank = SemIfClient("", enabled=True, session=session)
    assert off.choose("今天十七度", _OPTIONS) is None
    assert blank.choose("今天十七度", _OPTIONS) is None
    off.close()
    blank.close()


def test_client_reuses_session_and_rejects_bad_payload() -> None:
    calls: list[float] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"answers": {"pick": {"choice": "c0"}}}

    class _Session:
        def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> _Response:
            del url, json
            calls.append(float(timeout or 0))
            return _Response()

        def close(self) -> None:
            return None

    client = SemIfClient(
        "http://192.168.5.111:42032/v1/systemone",
        timeout_s=0.12,
        enabled=True,
        session=_Session(),
    )
    try:
        assert client.choose("今天十七度", _OPTIONS) is None
        assert calls == [0.12]
    finally:
        client.close()


def test_close_without_live_owned_session_is_harmless() -> None:
    endpoint = "http://192.168.5.111:42032/v1/systemone"
    saved = sys.modules.pop("requests", None)
    try:
        disabled = SemIfClient(endpoint, enabled=False)
        assert disabled.choose("今天十七度", _OPTIONS) is None
        disabled.close()
        disabled.close()
        unused = SemIfClient(endpoint, enabled=True)
        unused.close()
        unused.close()
        assert "requests" not in sys.modules
    finally:
        if saved is not None:
            sys.modules["requests"] = saved

    closes = {"owned": 0, "injected": 0, "boom": 0}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"answers": {"pick": _picked("keep", {"keep": 0.93, "c0": 0.07})}}

    class _Session:
        def __init__(self, key: str) -> None:
            self.key = key

        def post(self, *args: object, **kwargs: object) -> _Response:
            del args, kwargs
            return _Response()

        def close(self) -> None:
            closes[self.key] += 1

    class _Boom(_Session):
        def close(self) -> None:
            closes[self.key] += 1
            raise OSError("close failed")

    class _Requests:
        class RequestException(Exception):
            pass

        def __init__(self, session: _Session) -> None:
            self._session = session

        def Session(self) -> _Session:
            return self._session

    owned = _Session("owned")
    injected = _Session("injected")
    boom = _Boom("boom")
    original = semif_judge.require_requests
    semif_judge.require_requests = lambda: _Requests(owned)
    try:
        client = SemIfClient(endpoint, enabled=True)
        assert client.choose("今天十七度", _OPTIONS) == "keep"
        client.close()
        client.close()
        assert closes["owned"] == 1

        caller = SemIfClient(endpoint, enabled=True, session=injected)
        assert caller.choose("今天十七度", _OPTIONS) == "keep"
        caller.close()
        caller.close()
        assert closes["injected"] == 0

        semif_judge.require_requests = lambda: _Requests(boom)
        failing = SemIfClient(endpoint, enabled=True)
        assert failing.choose("今天十七度", _OPTIONS) == "keep"
        try:
            failing.close()
        except OSError as exc:
            assert str(exc) == "close failed"
        else:
            raise AssertionError("owned close error was swallowed")
        failing.close()
        assert closes["boom"] == 1
    finally:
        semif_judge.require_requests = original


def test_request_exception_keeps_original() -> None:
    class _Session:
        def post(self, *args: object, **kwargs: object) -> object:
            raise requests.ConnectionError("down")

        def close(self) -> None:
            return None

    client = SemIfClient(
        "http://192.168.5.111:42032/v1/systemone",
        enabled=True,
        session=_Session(),
    )
    assert client.choose("今天十七度", _OPTIONS) is None


class _MultiSession:
    def __init__(self, answers: object) -> None:
        self.answers = answers
        self.payloads: list[dict[str, object]] = []

    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> object:
        self.payloads.append({"url": url, "json": json, "timeout": timeout})

        class _Response:
            def __init__(self, body: object) -> None:
                self._body = body

            def raise_for_status(self) -> None:
                return None

            def json(self) -> object:
                return self._body

        if isinstance(self.answers, Exception):
            raise self.answers
        return _Response({"answers": self.answers})


def test_request_choices_gates_each_question_independently() -> None:
    criteria = {"tool": "工具", "person": "人", "unclear": "不清楚"}
    questions = {
        "role1": {"type": "choice", "instructions": "i1", "criteria": criteria},
        "role2": {"type": "choice", "instructions": "i2", "criteria": criteria},
        "role3": {"type": "choice", "instructions": "i3", "criteria": criteria},
    }
    answers = {
        "role1": {"choice": "tool", "probabilities": {"tool": 0.9, "person": 0.05, "unclear": 0.05}},
        # Weak top score: abstains even though tool leads.
        "role2": {"choice": "tool", "probabilities": {"tool": 0.6, "person": 0.3, "unclear": 0.1}},
        # role3 missing from answers: abstains.
    }
    session = _MultiSession(answers)
    results = semif_judge.request_choices(session, "http://localhost:1/v1/systemone", 0.3, "state", questions)
    assert results == {"role1": "tool", "role2": None, "role3": None}
    assert len(session.payloads) == 1
    assert set(session.payloads[0]["json"]["questions"]) == {"role1", "role2", "role3"}


def test_request_choices_failure_returns_empty() -> None:
    session = _MultiSession(OSError("down"))
    questions = {"role1": {"type": "choice", "instructions": "i", "criteria": {"a": "a", "b": "b"}}}
    assert semif_judge.request_choices(session, "http://localhost:1", 0.3, "s", questions) == {}
    assert semif_judge.request_choices(None, "http://localhost:1", 0.3, "s", questions) == {}
    assert semif_judge.request_choices(session, "", 0.3, "s", questions) == {}
    assert semif_judge.request_choices(session, "http://localhost:1", 0.0, "s", questions) == {}
