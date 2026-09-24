#!/usr/bin/env python3
"""Lightweight local tests for the startup-warmup wrapper (no model, no GPU, no fastapi).

The wrapper is imported against a stub ``decider.serve`` so the control flow under test is
exactly the one that runs on 111: official lifespan first, warmup second, ``yield`` only when
every warmup answer is a protocol-legal choice distribution; a failure must keep the lifespan
from yielding (which is what stops uvicorn from binding the port).

Legality is confidence-free on purpose.  Legal on purpose (must still become ready): a uniform
distribution, a mildly flat one, a saturated one, and ties between the top labels.  Rejected on
purpose: wrong labels, bad mass, NaN / inf / negative / >1 probabilities, non-numeric values, a
non-choice answer, a choice outside the labels, a choice that contradicts the argmax, an
official-route error, and policy drift.

    python3 test_serve_warmup_local.py            # every scenario, each in its own process
    python3 test_serve_warmup_local.py <scenario> # one scenario, prints its JSON result
"""

import asyncio
import json
import math
import os
import subprocess
import sys
import tempfile
import types
from contextlib import asynccontextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
LEGAL = {
    "choice": "tool",
    "type": "choice",
    "probabilities": {"tool": 0.9786, "person": 0.003, "unclear": 0.0184},
    "confidence": 0.9678,
}
LEGAL_PERSON = {
    "choice": "person",
    "type": "choice",
    "probabilities": {"tool": 0.003, "person": 0.9761, "unclear": 0.0209},
    "confidence": 0.9641,
}
UNIFORM = {
    "choice": "tool",
    "type": "choice",
    "probabilities": {"tool": 1 / 3, "person": 1 / 3, "unclear": 1 / 3},
    "confidence": 0.3333,
}
UNIFORM_TIE_PERSON = dict(UNIFORM, choice="person")
SATURATED = {
    "choice": "tool",
    "type": "choice",
    "probabilities": {"tool": 0.9999, "person": 0.00005, "unclear": 0.00005},
    "confidence": 0.9999,
}
MILDLY_FLAT = {
    "choice": "tool",
    "type": "choice",
    "probabilities": {"tool": 0.34, "person": 0.33, "unclear": 0.33},
    "confidence": 0.34,
}
SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def install_stub(answers, temp=1.935, sealed=True, graphs=0, events=None):
    """Install a stub decider.serve whose lifespan and route handler record what happens."""
    events = events if events is not None else []
    stub = types.ModuleType("decider.serve")
    calls = []

    class S1Req:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    async def systemone(req):
        calls.append(req)
        nxt = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(nxt, Exception):
            raise nxt
        return {"model": "decider-4b-v2", "answers": {"role": nxt}, "usage": {"input_tokens": 1, "output_tokens": 0}}

    class Engine:
        def __init__(self):
            self.sealed = sealed
            self.graphs = {} if not graphs else {i: object() for i in range(graphs)}
            self.cfg = {"compile": False, "fp8": False, "graphs": True}

    @asynccontextmanager
    async def official_lifespan(app):
        events.append("official_start")
        yield {"state": "official"}
        events.append("official_stop")

    stub.app = types.SimpleNamespace(router=types.SimpleNamespace(lifespan_context=official_lifespan))
    for name, value in (
        ("TEMP", temp),
        ("LAYOUT", "plain"),
        ("SCHEMA_FIRST", False),
        ("WARMUP", False),
        ("COMPILE", False),
        ("FP8", False),
        ("SHARED", False),
        ("MODEL", "/work/weights/decider-4b"),
        ("MODEL_NAME", "decider-4b-v2"),
        ("MAX_STATE_TOKENS", 2048),
    ):
        setattr(stub, name, value)
    stub.eng = Engine()
    stub.S1Req = S1Req
    stub.systemone = systemone

    pkg = types.ModuleType("decider")
    pkg.serve = stub
    sys.modules["decider"] = pkg
    sys.modules["decider.serve"] = stub
    sys.modules.pop("serve_warmup", None)
    return stub, calls, events


def import_wrapper(report_path):
    os.environ["DECIDER_WARMUP_REPORT"] = report_path
    import serve_warmup

    return serve_warmup


def drive(wrapper):
    """Enter the wrapped lifespan; returns the yielded value or raises like ASGI startup would."""
    cm = wrapper.warmup_lifespan(object())
    return asyncio.run(cm.__aenter__())


def assert_failure(wrapper, label, report_path):
    """A rejected answer must keep the lifespan from yielding: no yield == uvicorn never binds."""
    try:
        drive(wrapper)
    except Exception as exc:
        assert not isinstance(exc, AssertionError), f"{label}: unexpected assertion {exc}"
        report = json.load(open(report_path))
        assert report["status"] == "failed", "{}: report status {!r}".format(label, report["status"])
        assert report.get("failed_case"), f"{label}: no failed_case in report"
        cases = report.get("cases") or []
        assert cases and cases[-1]["ok"] is False, f"{label}: last case not marked failed"
        assert cases[-1]["problems"], f"{label}: no problems recorded"
        return {
            "raised": type(exc).__name__,
            "message": str(exc)[:200],
            "report_status": report["status"],
            "problems": cases[-1]["problems"],
        }
    raise AssertionError(f"{label}: lifespan yielded although the warmup was not legal")


def run_case(name, answers, assertion):
    """Each scenario runs in its own process so the frozen-style module import is clean."""
    with tempfile.TemporaryDirectory() as tmp:
        report_path = os.path.join(tmp, "warmup-report.json")
        stub, calls, events = install_stub(answers)
        wrapper = import_wrapper(report_path)
        result = assertion(wrapper, stub, calls, events, report_path)
        result["calls"] = len(calls)
        return result


def run_legal(name, answers):
    """A legal answer must yield, write status=ok and leave the official lifespan untouched."""

    def check(wrapper, stub, calls, events, report_path):
        yielded = drive(wrapper)
        assert yielded == {"state": "official"}, yielded
        assert stub.app.router.lifespan_context is wrapper.warmup_lifespan
        report = json.load(open(report_path))
        assert report["status"] == "ok", report["status"]
        assert len(report["cases"]) == 2 and all(c["ok"] for c in report["cases"]), report["cases"]
        assert events == ["official_start"], events
        return {
            "yielded": yielded,
            "report_status": report["status"],
            "choices": [c["choice"] for c in report["cases"]],
            "tops": [c["top"] for c in report["cases"]],
        }

    return run_case(name, answers, check)


def run_rejected(name, answers):
    def check(wrapper, stub, calls, events, report_path):
        return assert_failure(wrapper, name, report_path)

    return run_case(name, answers, check)


@scenario
def success():
    """The real warmup payload shape: two decisive answers, long case included."""

    def check(wrapper, stub, calls, events, report_path):
        yielded = drive(wrapper)
        assert yielded == {"state": "official"}, yielded
        report = json.load(open(report_path))
        assert report["status"] == "ok", report["status"]
        assert len(report["cases"]) == 2 and all(c["ok"] for c in report["cases"])
        assert events == ["official_start"], events
        # the warmup payload is the Recordian role choice, sent through the official handler
        req = calls[0]
        q = req.questions["role"]
        assert q["type"] == "choice" and "判断句子中标记的「jeff」" in q["instructions"]
        assert set(q["criteria"]) == {"tool", "person", "unclear"}
        assert "只判断这个跨度：「jeff」" in req.state
        # the long case really is long
        assert report["cases"][1]["state_chars"] > 400, report["cases"][1]["state_chars"]
        assert [c["choice"] for c in report["cases"]] == ["tool", "person"]
        # legality is described without any confidence bound in the report
        assert "No confidence or accuracy gate" in report["legality"], report["legality"]
        assert "min_top" not in report and "max_top" not in report
        return {
            "yielded": yielded,
            "report_status": report["status"],
            "long_state_chars": report["cases"][1]["state_chars"],
            "legality": report["legality"][:80],
        }

    return run_case("success", [LEGAL, LEGAL_PERSON], check)


@scenario
def uniform_is_legal():
    """A perfectly uniform distribution is legal: readiness is not a confidence threshold."""
    return run_legal("uniform_is_legal", [UNIFORM, UNIFORM_TIE_PERSON])


@scenario
def saturated_is_legal():
    """A high-confidence distribution is legal too (the old top>0.999 rejection was wrong)."""
    return run_legal(
        "saturated_is_legal",
        [
            SATURATED,
            dict(SATURATED, choice="person", probabilities={"tool": 0.00005, "person": 0.9999, "unclear": 0.00005}),
        ],
    )


@scenario
def mildly_flat_is_legal():
    """top < 0.40 is not a failure: 0.34/0.33/0.33 is a well-formed answer to a real forward."""
    return run_legal(
        "mildly_flat_is_legal",
        [
            MILDLY_FLAT,
            dict(MILDLY_FLAT, choice="person", probabilities={"tool": 0.33, "person": 0.34, "unclear": 0.33}),
        ],
    )


@scenario
def bad_labels():
    return run_rejected("bad_labels", [dict(LEGAL, probabilities={"tool": 0.5, "other": 0.5})])


@scenario
def mass_not_normalised():
    return run_rejected(
        "mass_not_normalised", [dict(LEGAL, probabilities={"tool": 0.5, "person": 0.2, "unclear": 0.1})]
    )


@scenario
def nan_probability():
    return run_rejected(
        "nan_probability", [dict(LEGAL, probabilities={"tool": float("nan"), "person": 0.5, "unclear": 0.5})]
    )


@scenario
def inf_probability():
    return run_rejected(
        "inf_probability", [dict(LEGAL, probabilities={"tool": float("inf"), "person": 0.5, "unclear": 0.5})]
    )


@scenario
def negative_probability():
    return run_rejected(
        "negative_probability", [dict(LEGAL, probabilities={"tool": -0.1, "person": 0.6, "unclear": 0.5})]
    )


@scenario
def above_one_probability():
    return run_rejected(
        "above_one_probability", [dict(LEGAL, probabilities={"tool": 1.5, "person": -0.25, "unclear": -0.25})]
    )


@scenario
def non_numeric_probability():
    return run_rejected(
        "non_numeric_probability", [dict(LEGAL, probabilities={"tool": "high", "person": 0.5, "unclear": 0.5})]
    )


@scenario
def not_a_choice():
    return run_rejected(
        "not_a_choice", [{"type": "scale", "probabilities": {"tool": 0.7, "person": 0.2, "unclear": 0.1}}]
    )


@scenario
def choice_not_a_label():
    return run_rejected("choice_not_a_label", [dict(LEGAL, choice="thing")])


@scenario
def choice_not_argmax():
    return run_rejected("choice_not_argmax", [dict(LEGAL, choice="person")])


@scenario
def official_route_error():
    # exactly the selftest case 2 on 111: the official renderer rejects the criteria map
    return run_rejected("official_route_error", [LEGAL, ValueError("choice criteria: a map of 2..255 options")])


@scenario
def policy_drift():
    def check(wrapper, stub, calls, events, report_path):
        stub.TEMP = 1.0
        try:
            drive(wrapper)
        except RuntimeError as exc:
            assert "policy drifted" in str(exc), str(exc)
            assert not calls, "warmup ran although the policy drifted"
            assert not os.path.exists(report_path), "no report should be written on policy drift"
            return {"raised": "RuntimeError", "message": str(exc)[:200], "calls": 0}
        raise AssertionError("lifespan yielded although the trial policy drifted")

    return run_case("policy_drift", [LEGAL], check)


def check_answer_units():
    """Direct unit checks of check_answer, independent of the lifespan plumbing."""
    with tempfile.TemporaryDirectory() as tmp:
        install_stub([LEGAL])
        w = import_wrapper(os.path.join(tmp, "unused.json"))
        case = w.CASES[0]
        uniform = {
            "answers": {
                "role": {
                    "choice": "unclear",
                    "type": "choice",
                    "probabilities": {"tool": 1 / 3, "person": 1 / 3, "unclear": 1 / 3},
                }
            }
        }
        ok, problems, summary = w.check_answer(case, uniform)
        assert ok and not problems, problems
        assert math.isclose(summary["top"], 1 / 3), summary
        ok, problems, _ = w.check_answer(
            case,
            {
                "answers": {
                    "role": {
                        "choice": "person",
                        "type": "choice",
                        "probabilities": {"tool": 0.9, "person": 0.05, "unclear": 0.05},
                    }
                }
            },
        )
        assert not ok and any("largest probability" in p for p in problems), problems
        return {"uniform_ok": True, "argmax_mismatch_problems": problems}


SCENARIOS["check_answer_units"] = check_answer_units


def main():
    if len(sys.argv) > 1:
        name = sys.argv[1]
        print(json.dumps({name: SCENARIOS[name]()}, ensure_ascii=False))
        return 0
    failures = []
    for name in SCENARIOS:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), name], capture_output=True, text=True, cwd=HERE
        )
        tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or [""]
        status = "PASS" if proc.returncode == 0 else "FAIL"
        print(f"{name:<24} {status} {tail[0][:220]}")
        if proc.returncode != 0:
            failures.append(name)
    print(f"SCENARIOS {len(SCENARIOS)} failures {len(failures)} {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
