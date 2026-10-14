"""Bounded final-only grammar edits cannot rewrite the dictation."""

import threading
import time
from types import SimpleNamespace

import pytest

from recordian import particle_correction as pc
from recordian.semif_judge import interpret_choice
from recordian.streaming_correction import corrector_from_args


class Identity:
    def submit(self, text):
        return text

    poll = submit
    finish = submit

    def cancel(self):
        pass

    close = cancel


def wrapper(monkeypatch, choice="地", **kwargs):
    calls = []

    def ask(timeout, state, questions, **options):
        calls.append((timeout, questions, options))
        return dict.fromkeys(questions, choice)

    monkeypatch.setattr(pc, "request_jev_choices", ask)
    return pc.ParticleCorrector(Identity(), timeout_s=1.5, **kwargs), calls


def test_strict_particle_gate_keeps_existing_alias_gate():
    options = {"的": "a", "地": "b", "得": "c"}
    reply = {"choice": "地", "probabilities": {"的": 0.12, "地": 0.8, "得": 0.08}}
    assert interpret_choice(options, reply) == "地"
    assert interpret_choice(options, reply, min_top=0.85, min_margin=0.35) is None


def test_final_only_equal_length_repeated_clause_edits(monkeypatch):
    corrector, calls = wrapper(monkeypatch)
    text = "认真仔细的检查，认真仔细的检查。"
    assert corrector.submit(text) == corrector.poll(text) == text
    assert calls == []
    result = corrector.finish(text)
    assert result == "认真仔细地检查，认真仔细地检查。"
    assert len(calls) == 1 and len(calls[0][1]) == 1
    assert calls[0][2]["min_top"] == 0.85
    assert corrector.finish(text) == corrector.poll(text) == result
    assert len(calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "English words stay intact.",
        "地理资料得到了奖励。",
        "代码 `认真仔细的检查` 保持原样。",
        "原话是“认真仔细的检查”。",
        "请把的改成地。",
        "path/认真的文件.txt",
        "这段代码的变量是 foo_bar。",
    ],
)
def test_protected_or_ineligible_text_does_not_call_model(monkeypatch, text):
    corrector, calls = wrapper(monkeypatch)
    assert corrector.finish(text) == text
    assert not calls


def test_unknown_answers_cannot_insert_or_delete(monkeypatch):
    corrector, _ = wrapper(monkeypatch, choice="地并且重复整句话")
    text = "认真仔细的检查。"
    assert corrector.finish(text) == text


def test_batch_and_clause_bounds(monkeypatch):
    corrector, calls = wrapper(monkeypatch)
    text = "。".join("甲" * i + "组仔细的检查" for i in range(30))
    result = corrector.finish(text)
    assert len(calls) == 1 and len(calls[0][1]) <= 8
    assert len(result) == len(text)
    assert all(a == b or (a == "的" and b in "地得") for a, b in zip(text, result, strict=True))
    corrector.finish("很" * 100 + "的检查")
    assert len(calls) == 1


def test_shared_budget_does_not_add_a_second_wait(monkeypatch):
    corrector, calls = wrapper(monkeypatch)
    corrector.timeout_s = 0.03
    corrector.base.finish = lambda text: (time.sleep(0.04), text)[1]
    assert corrector.finish("认真仔细的检查。") == "认真仔细的检查。"
    assert calls == []


def test_cancel_rejects_late_result(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def ask(timeout, state, questions, **options):
        started.set()
        release.wait(1)
        return dict.fromkeys(questions, "地")

    monkeypatch.setattr(pc, "request_jev_choices", ask)
    corrector = pc.ParticleCorrector(Identity(), timeout_s=1.5)
    result = []
    thread = threading.Thread(target=lambda: result.append(corrector.finish("认真仔细的检查。")))
    thread.start()
    assert started.wait(1)
    corrector.cancel()
    release.set()
    thread.join(1)
    assert result == ["认真仔细的检查。"]
    corrector.close()
    assert corrector.finish("认真仔细的检查。") == "认真仔细的检查。"


def test_explicit_opt_in_and_provider_gate():
    for enabled, provider in [(False, "jev"), (True, "semif")]:
        c = corrector_from_args(SimpleNamespace(enable_particle_correction=enabled, correction_provider=provider), [])
        assert not isinstance(c, pc.ParticleCorrector)
        c.close()
    c = corrector_from_args(SimpleNamespace(enable_particle_correction=True, correction_provider="jev"), [])
    assert isinstance(c, pc.ParticleCorrector)
    c.close()


def test_config_opt_in_round_trip(tmp_path, monkeypatch):
    import json

    from recordian.arg_parser import _parse_args_with_config, build_parser
    from recordian.runtime_config import normalize_runtime_config

    assert normalize_runtime_config({})["enable_particle_correction"] is False
    cfg = tmp_path / "hotkey.json"
    cfg.write_text(json.dumps({"enable_particle_correction": True}))
    monkeypatch.setattr("sys.argv", ["recordian", "--config-path", str(cfg)])
    assert _parse_args_with_config(build_parser()).enable_particle_correction is True
    monkeypatch.setattr("sys.argv", ["recordian", "--config-path", str(cfg), "--no-enable-particle-correction"])
    assert _parse_args_with_config(build_parser()).enable_particle_correction is False


def test_quoted_multiple_clauses_never_judged(monkeypatch):
    corrector, calls = wrapper(monkeypatch)
    text = "他说“认真仔细的检查，认真仔细的检查，认真仔细的检查。”"
    assert corrector.finish(text) == text
    assert not calls


def test_particle_only_oneshot_final_entry(monkeypatch):
    from recordian.postprocess_pipeline import _apply_final_semif

    calls = []
    def ask(timeout, state, questions, **options):
        calls.append(questions)
        return dict.fromkeys(questions, '地')
    monkeypatch.setattr(pc, 'request_jev_choices', ask)
    args = SimpleNamespace(enable_particle_correction=True, enable_semif_correction=False, correction_provider='jev')
    assert _apply_final_semif('认真仔细的检查。', args=args, hotwords=[], on_state=lambda _: None) == '认真仔细地检查。'
    assert len(calls) == 1
