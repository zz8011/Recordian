"""Contracts for applying caller-authored edits to one immutable snapshot."""

import asyncio
import threading
from concurrent.futures import CancelledError
from dataclasses import FrozenInstanceError
from itertools import permutations

import pytest

from recordian.contextual_choice import (
    CandidateEdit,
    Choice,
    apply_choices,
    judge_choices,
    question_payload,
)


def choice(qid, *edits):
    return Choice(qid, {"criteria": {"keep": "unchanged", "fix": "candidate"}}, {"keep": (), "fix": edits})


def test_candidate_edit_cannot_be_changed_after_validation():
    edit = CandidateEdit(0, 1, "甲", "乙")
    with pytest.raises(FrozenInstanceError):
        edit.replacement = "unapproved text"


def test_payload_contains_questions_without_edit_objects_and_is_detached():
    item = choice("q", CandidateEdit(0, 1, "甲", "乙"))
    payload = question_payload([item])
    assert payload == {"q": {"criteria": {"keep": "unchanged", "fix": "candidate"}}}
    payload["q"]["criteria"]["injected"] = "not declared"
    assert "injected" not in item.question["criteria"]


def test_duplicate_question_ids_rejected_before_request_or_application():
    choices = [choice("q", CandidateEdit(0, 1, "甲", "乙")), choice("q")]
    with pytest.raises(ValueError, match="duplicate"):
        question_payload(choices)
    with pytest.raises(ValueError, match="duplicate"):
        apply_choices("甲", choices, {"q": "fix"})
    with pytest.raises(ValueError, match="duplicate"):
        judge_choices("甲", "state", choices, request=unexpected_request, timeout_s=1)


@pytest.mark.parametrize(
    "answers",
    [
        None,
        [],
        "fix",
        {},
        {"unknown": "fix"},
        {"q": "unknown"},
        {"q": None},
        {"q": []},
        {"q": {"choice": "fix"}},
        {"q": True},
    ],
)
def test_missing_unknown_or_malformed_answers_preserve_snapshot(answers):
    assert apply_choices("甲", [choice("q", CandidateEdit(0, 1, "甲", "乙"))], answers) == "甲"


@pytest.mark.parametrize(
    "criteria,edits",
    [
        ({"keep": "unchanged"}, {"fix": (CandidateEdit(0, 1, "甲", "乙"),)}),
        ({"fix": "candidate"}, {"keep": ()}),
        (None, {"fix": (CandidateEdit(0, 1, "甲", "乙"),)}),
        (["fix"], {"fix": (CandidateEdit(0, 1, "甲", "乙"),)}),
    ],
)
def test_answer_requires_membership_in_both_criteria_and_edits(criteria, edits):
    item = Choice("q", {"criteria": criteria}, edits)
    assert apply_choices("甲", [item], {"q": "fix"}) == "甲"


def test_declared_keep_is_empty_and_does_not_block_an_independent_choice():
    choices = [choice("keep", CandidateEdit(0, 1, "甲", "乙")), choice("fix", CandidateEdit(1, 2, "丙", "丁"))]
    assert apply_choices("甲丙", choices, {"keep": "keep", "fix": "fix"}) == "甲丁"


@pytest.mark.parametrize(
    "bad",
    [
        CandidateEdit(-1, 1, "甲", "乙"),
        CandidateEdit(0, 10, "甲", "乙"),
        CandidateEdit(1, 0, "", "乙"),
        CandidateEdit(0, 0, "", "乙"),
        CandidateEdit(True, 2, "甲", "乙"),
        CandidateEdit(0, False, "", "乙"),
        CandidateEdit(0.0, 1, "甲", "乙"),
        CandidateEdit(0, "1", "甲", "乙"),
        CandidateEdit(0, 1, "stale", "乙"),
        CandidateEdit(0, 1, None, "乙"),
        CandidateEdit(0, 1, "甲", None),
        {"start": 0, "end": 1, "original": "甲", "replacement": "乙"},
    ],
)
def test_invalid_edit_rejects_whole_option_but_not_independent_choice(bad):
    choices = [choice("bad", CandidateEdit(1, 2, "丙", "丁"), bad), choice("good", CandidateEdit(2, 3, "戊", "己"))]
    assert apply_choices("甲丙戊", choices, {"bad": "fix", "good": "fix"}) == "甲丙己"


def test_invalid_unselected_option_does_not_poison_selected_option():
    item = choice("q", CandidateEdit(0, 1, "甲", "乙"))
    item.edits["other"] = (CandidateEdit(-1, 100, "stale", "bad"),)
    assert apply_choices("甲", [item], {"q": "fix"}) == "乙"


@pytest.mark.parametrize("edits", [None, "rewrite", [CandidateEdit(0, 1, "甲", "乙")]])
def test_malformed_selected_edit_bundle_is_ignored(edits):
    item = Choice("q", {"criteria": {"fix": "candidate"}}, {"fix": edits})
    assert apply_choices("甲", [item], {"q": "fix"}) == "甲"


@pytest.mark.parametrize(
    "literal", ["`甲`", "```\n甲\n```", "https://example.test/甲", "a@example.test", "fn(甲)", "123", "一百", "不要甲"]
)
def test_real_protected_spans_reject_entire_option(literal):
    snapshot = "前" + literal + "后"
    # Covering a protected span is rejected even when the edit starts outside it.
    edits = (
        CandidateEdit(0, len(snapshot) - 1, snapshot[:-1], "替换"),
        CandidateEdit(len(snapshot) - 1, len(snapshot), "后", "末"),
    )
    assert apply_choices(snapshot, [choice("q", *edits)], {"q": "fix"}) == snapshot


def test_edit_adjacent_to_literal_preserves_literal_bytes():
    assert (
        apply_choices(
            "甲`代码`乙",
            [choice("q", CandidateEdit(0, 1, "甲", "开始"), CandidateEdit(5, 6, "乙", "结束"))],
            {"q": "fix"},
        )
        == "开始`代码`结束"
    )


def test_variable_lengths_use_original_coordinates_and_preserve_surroundings():
    choices = [choice("a", CandidateEdit(1, 3, "甲乙", "长替换")), choice("b", CandidateEdit(4, 6, "丙丁", ""))]
    assert apply_choices("「甲乙」丙丁！", choices, {"a": "fix", "b": "fix"}) == "「长替换」！"


def test_repeated_surface_only_changes_selected_occurrence():
    assert apply_choices("甲乙，甲乙。", [choice("q", CandidateEdit(3, 5, "甲乙", "丙"))], {"q": "fix"}) == "甲乙，丙。"


@pytest.mark.parametrize("replacement", ["地", "", "新词"])
def test_thousands_of_edits_have_linear_slice_volume(replacement):
    class SliceTrackedText(str):
        """Keep the meter through concatenation to expose repeated rebuilding."""

        def __new__(cls, text, meter):
            instance = super().__new__(cls, text)
            instance.meter = meter
            return instance

        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                self.meter[0] += len(result)
            return type(self)(result, self.meter)

        def __add__(self, other):
            return type(self)(super().__add__(other), self.meter)

        def __radd__(self, other):
            return type(self)(str.__add__(other, self), self.meter)

    count = 2048
    meter = [0]
    snapshot = SliceTrackedText("甲乙" * count, meter)
    edits = tuple(CandidateEdit(2 * i, 2 * i + 1, "甲", replacement) for i in range(count))
    result = apply_choices(snapshot, [choice("q", *edits)], {"q": "fix"})

    assert result == (replacement + "乙") * count
    # Includes validation slices as well as assembly; repeated whole-result
    # reconstruction exceeds this bound by orders of magnitude.
    assert meter[0] <= 2 * len(snapshot)


def test_exact_duplicates_apply_once_even_when_replacement_changes_length():
    edit = CandidateEdit(0, 1, "甲", "长词")
    choices = [choice("a", edit, edit), choice("b", edit)]
    assert apply_choices("甲乙", choices, {"a": "fix", "b": "fix"}) == "长词乙"


def test_conflicting_chain_preserves_all_conflicts_independent_of_order():
    choices = [
        choice("a", CandidateEdit(0, 2, "甲乙", "A")),
        choice("b", CandidateEdit(1, 3, "乙丙", "B")),
        choice("c", CandidateEdit(2, 4, "丙丁", "C")),
        choice("d", CandidateEdit(4, 5, "戊", "好")),
    ]
    for ordering in permutations(choices):
        assert apply_choices("甲乙丙丁戊", ordering, dict.fromkeys("abcd", "fix")) == "甲乙丙丁好"


def test_conflicting_same_span_rejects_both_selected_bundles_atomically():
    choices = [
        choice("a", CandidateEdit(0, 1, "甲", "长词"), CandidateEdit(2, 3, "丙", "丁")),
        choice("b", CandidateEdit(0, 1, "甲", "另一个词")),
    ]
    assert apply_choices("甲乙丙", choices, {"a": "fix", "b": "fix"}) == "甲乙丙"


def test_conflict_within_one_option_rejects_whole_option():
    item = choice(
        "q", CandidateEdit(0, 2, "甲乙", "A"), CandidateEdit(1, 3, "乙丙", "B"), CandidateEdit(3, 4, "丁", "C")
    )
    assert apply_choices("甲乙丙丁", [item], {"q": "fix"}) == "甲乙丙丁"


def unexpected_request(*args, **kwargs):
    pytest.fail("request must not be called")


@pytest.mark.parametrize("budget", [0, -1])
def test_no_request_without_positive_budget(budget):
    assert judge_choices("甲", "state", [choice("q")], request=unexpected_request, timeout_s=budget) == "甲"


def test_no_request_for_empty_choices():
    assert judge_choices("甲", "state", iter(()), request=unexpected_request, timeout_s=1) == "甲"


def test_judge_forwards_caller_policy_and_cannot_use_freeform_model_text():
    item = choice("q", CandidateEdit(0, 1, "甲", "乙"))
    owner_thread = threading.get_ident()
    token = object()

    def request(timeout, state, questions, **options):
        assert threading.get_ident() == owner_thread
        assert timeout == 0.25 and state == "caller-bounded context"
        assert questions == {"q": {"criteria": {"keep": "unchanged", "fix": "candidate"}}}
        assert options == {"min_top": 0.91, "cancel_token": token}
        return {"q": "fix", "text": "rewrite the entire snapshot"}

    assert (
        judge_choices(
            "甲，原文。",
            "caller-bounded context",
            iter([item]),
            request=request,
            timeout_s=0.25,
            min_top=0.91,
            cancel_token=token,
        )
        == "乙，原文。"
    )


@pytest.mark.parametrize("error", [TimeoutError("caller deadline"), CancelledError(), asyncio.CancelledError()])
def test_caller_timeout_and_cancellation_propagate_without_retry(error):
    calls = []

    def request(*args, **kwargs):
        calls.append(True)
        raise error

    with pytest.raises(type(error)) as caught:
        judge_choices("甲", "state", [choice("q")], request=request, timeout_s=0.1)
    assert caught.value is error and calls == [True]


def test_caller_can_report_timeout_or_cancellation_as_empty_answers():
    def request(*args, **kwargs):
        return {}

    assert (
        judge_choices("甲", "state", [choice("q", CandidateEdit(0, 1, "甲", "乙"))], request=request, timeout_s=0.1)
        == "甲"
    )
