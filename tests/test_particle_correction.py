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
    reply = {"choice": "地", "probabilities": {"的": 0.20, "地": 0.72, "得": 0.08}}
    assert interpret_choice(options, reply) == "地"
    assert interpret_choice(options, reply, min_top=pc.MIN_TOP, min_margin=pc.MIN_MARGIN) is None
    calibrated = {"choice": "地", "probabilities": {"的": 0.12, "地": 0.8, "得": 0.08}}
    assert interpret_choice(options, calibrated, min_top=pc.MIN_TOP, min_margin=pc.MIN_MARGIN) == "地"


def test_final_only_equal_length_repeated_clause_edits(monkeypatch):
    corrector, calls = wrapper(monkeypatch)
    text = "认真仔细的检查，认真仔细的检查。"
    assert corrector.submit(text) == corrector.poll(text) == text
    assert calls == []
    result = corrector.finish(text)
    assert result == "认真仔细地检查，认真仔细的检查。"
    assert len(calls) == 1 and len(calls[0][1]) == 1
    assert calls[0][2]["min_top"] > 0.70
    assert corrector.finish(text) == corrector.poll(text) == result
    assert len(calls) == 1


def test_fluent_candidate_context_keeps_original_edit_offsets(monkeypatch):
    """The judge must see fluent words as well as the exact character it may edit."""
    def ask(timeout, state, questions, **options):
        answers = {}
        for name, question in questions.items():
            candidate = question["criteria"]["得"]
            assert candidate == "她把流程讲得很清楚"
            assert "她把流程讲【的】很清楚" in question["instructions"]
            answers[name] = "得"
        return answers

    monkeypatch.setattr(pc, "request_jev_choices", ask)
    corrector = pc.ParticleCorrector(Identity(), timeout_s=1.5)
    assert corrector.finish("她把流程讲的很清楚；她把流程讲的很清楚。") == (
        "她把流程讲得很清楚；她把流程讲的很清楚。"
    )


@pytest.mark.parametrize("text", ["稳妥的安排", "深入的研究。", "我跑的快！"])
def test_context_poor_fragments_are_preserved_without_judgment(monkeypatch, text):
    corrector, calls = wrapper(monkeypatch)
    assert corrector.finish(text) == text
    assert calls == []


def test_judge_receives_neighboring_clauses_for_nominal_context(monkeypatch):
    text = "先分配值班人员，细心的负责复核记录，其他人整理仓库。"
    def ask(timeout, state, questions, **options):
        context = next(iter(questions.values()))["instructions"]
        assert "先分配值班人员" in context
        assert "其他人整理仓库" in context
        return dict.fromkeys(questions, "的")
    monkeypatch.setattr(pc, "request_jev_choices", ask)
    corrector = pc.ParticleCorrector(Identity(), timeout_s=1.5)
    assert corrector.finish(text) == text


def test_long_context_is_bounded_and_keeps_late_candidate(monkeypatch):
    text = "资料已经归档。" * 200 + "她把流程讲的很清楚。" * 200
    def ask(timeout, state, questions, **options):
        assert len(state) < 700
        assert len(questions) == 1
        context = next(iter(questions.values()))["instructions"]
        assert len(context) < 700
        assert "她把流程讲【的】很清楚" in context
        return dict.fromkeys(questions, "得")
    monkeypatch.setattr(pc, "request_jev_choices", ask)
    corrector = pc.ParticleCorrector(Identity(), timeout_s=1.5)
    assert corrector.finish(text) == (
        "资料已经归档。" * 200 + "她把流程讲的很清楚。" * 2
        + "她把流程讲得很清楚。" * 196 + "她把流程讲的很清楚。" * 2
    )


def test_repeated_clause_does_not_apply_a_choice_to_other_contexts(monkeypatch):
    text = "请你，仔细的检查，然后提交报告。" + "材料已经归档。" * 80 + "这里缺少，仔细的检查，只有敷衍应付。"
    def ask(timeout, state, questions, **options):
        assert len(questions) == 1
        answers = {}
        for name, question in questions.items():
            context = question["instructions"]
            if "请你" in context:
                answers[name] = "地"
            else:
                assert "这里缺少" in context
                answers[name] = "的"
        return answers
    monkeypatch.setattr(pc, "request_jev_choices", ask)
    corrector = pc.ParticleCorrector(Identity(), timeout_s=1.5)
    assert corrector.finish(text) == text.replace("仔细的检查", "仔细地检查", 1)


def test_adjacent_repetitions_across_sentences_do_not_share_a_choice(monkeypatch):
    text = "这里缺少，仔细的检查。仔细的检查，才能发现问题。"
    def ask(timeout, state, questions, **options):
        assert len(questions) == 1
        return {
            name: "的" if "这里缺少，仔细【的】检查。" in question["instructions"] else "地"
            for name, question in questions.items()
        }
    monkeypatch.setattr(pc, "request_jev_choices", ask)
    corrector = pc.ParticleCorrector(Identity(), timeout_s=1.5)
    assert corrector.finish(text) == text


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
