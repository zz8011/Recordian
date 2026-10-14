"""Independent occurrence evidence with baseline and request lifecycle protection."""

import json
from copy import deepcopy
from itertools import product
from pathlib import Path
from threading import Event

import pytest

from recordian import contextual_vocabulary as cv
from recordian import streaming_correction as sc
from recordian.contextual_choice import CandidateEdit, apply_choices, question_payload

ALIASES = [{"heard": "jeff", "word": "jev", "meaning": "软件工具"}]
INSTRUCTIONS = "依据完整上下文，只判断第{ordinal}处词语：{before}〔{surface}〕{after}。其他位置的用词不属于本题。"


def span_for(text, surface="食盐", candidates=None, start=None):
    start = text.index(surface) if start is None else start
    return {"start": start, "end": start + len(surface), "surface": surface,
            "candidates": ["实验"] if candidates is None else candidates}


def build(text, span=None, *, context="", protected=(), aliases=()):
    assert callable(getattr(cv, "vocabulary_occurrence_choices", None)), "independent occurrence builder missing"
    return cv.vocabulary_occurrence_choices(
        text, span_for(text) if span is None else span, context=context,
        is_eligible=lambda start, end: (
            not sc._meta_mention(text, start, end)
            and not sc._overlaps_span(start, end, protected)
        ),
        aliases=aliases,
    )


def assert_baseline(text, span=None, **kwargs):
    span = span_for(text) if span is None else span
    expected = cv.vocabulary_choice(text, span, context=kwargs.get("context", ""))
    actual = build(text, span, **kwargs)
    assert actual == (None if expected is None else (expected[0], [expected[1]]))
    if expected is not None:
        assert question_payload(actual[1]) == question_payload([expected[1]])


def test_two_positions_preserve_baseline_and_have_exact_independent_edits():
    text = "前言。食盐结果，食盐用量。"
    span = span_for(text)
    original_span = deepcopy(span)
    context = "前段" * 200
    state, choices = build(text, span, context=context)
    baseline_state, baseline_choice = cv.vocabulary_choice(text, span, context=context)
    assert state == baseline_state == "上一段：" + "前段" * 128 + "\n" + text + "\n只判断这个跨度：「食盐」"
    assert choices[0] == baseline_choice
    assert choices[0].question_id == "pick"
    assert choices[0].edits == {"keep": (), "c0": (CandidateEdit(3, 5, "食盐", "实验"),)}
    assert choices[1].question_id == "p1"
    assert choices[1].question == {
        "type": "choice",
        "instructions": "依据完整上下文，只判断第2处词语：〔食盐〕用量。其他位置的用词不属于本题。",
        "criteria": {"keep": "此处应使用食盐：食盐用量", "c0": "此处应使用实验：实验用量"},
    }
    assert choices[1].edits == {"keep": (), "c0": (CandidateEdit(8, 10, "食盐", "实验"),)}
    for answers, expected in (
        ({"pick": "keep", "p1": "keep"}, text),
        ({"pick": "keep", "p1": "c0"}, "前言。食盐结果，实验用量。"),
        ({"pick": "c0", "p1": "keep"}, "前言。实验结果，食盐用量。"),
        ({"pick": "c0", "p1": "c0"}, "前言。实验结果，实验用量。"),
    ):
        assert apply_choices(text, choices, answers) == expected
    assert span == original_span


def test_three_binary_positions_include_all_eight_independent_answers():
    text = "食盐，食盐，食盐。"
    state, choices = build(text)
    assert (state, choices[0]) == cv.vocabulary_choice(text, span_for(text))
    assert [c.question_id for c in choices] == ["pick", "p1", "p2"]
    assert [apply_choices(text, choices, dict(zip(["pick", "p1", "p2"], answers, strict=True)))
            for answers in product(["keep", "c0"], repeat=3)] == [
        "食盐，食盐，食盐。", "食盐，食盐，实验。", "食盐，实验，食盐。", "食盐，实验，实验。",
        "实验，食盐，食盐。", "实验，食盐，实验。", "实验，实验，食盐。", "实验，实验，实验。",
    ]


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("layout", ["compact", "padded", "clipped"])
@pytest.mark.parametrize("surface,replacement", [("食盐", "实验"), ("公示", "公式")], ids=["salt", "notice"])
@pytest.mark.parametrize("context", ["", "旧段\n" * 100 + "尾行\n末行\n"], ids=["no-context", "multiline"])
def test_natural_criteria_equal_applied_bundles_in_original_window(
    count, layout, surface, replacement, context,
):
    """Catch lost characters/newlines, wrong window offsets and partial assignments."""
    core = "\n，\t".join([surface] * count)
    if layout == "compact":
        text, start, begin = "\n " + core + " \n", 2, 0
        stop = len(text)
    elif layout == "padded":
        text = "前" * 200 + "\n " + core + " \n" + "后" * 200
        start, begin, stop = 202, 42, 364
    else:
        # A partial surface at each edge must survive, but never become an edit.
        text = surface + "，" * 159 + core + "，" * (161 - len(core)) + surface + "尾"
        start, begin, stop = 161, 1, 323
    span = span_for(text, surface=surface, candidates=[replacement], start=start)
    state, choices = build(text, span, context=context)
    assert (state, choices[0]) == cv.vocabulary_choice(text, span, context=context)
    assert len(choices) == count
    positions = [start + i * (len(surface) + 3) for i in range(count)]
    for i, choice in enumerate(choices):
        assert choice.question_id == ("pick" if i == 0 else f"p{i}")
        assert list(choice.edits) == ["keep", "c0"]
        assert choice.edits == {"keep": (), "c0": (CandidateEdit(positions[i], positions[i] + 2, surface, replacement),)}
        if i:
            # Nearest punctuation removes newline/comma; the tab survives.
            after = "" if i + 1 < count or layout == "clipped" else " "
            assert choice.question["instructions"] == INSTRUCTIONS.format(
                ordinal=i + 1, before="\t", surface=surface, after=after,
            )
            assert choice.question["criteria"] == {
                "keep": f"此处应使用{surface}：\t{surface}{after}",
                "c0": f"此处应使用{replacement}：\t{replacement}{after}",
            }
    outputs = set()
    for selected in product(["keep", "c0"], repeat=count):
        answers = {c.question_id: key for c, key in zip(choices, selected, strict=True)}
        result = apply_choices(text, choices, answers)
        expected = text
        for left, key in reversed(list(zip(positions, selected, strict=True))):
            if key == "c0":
                expected = expected[:left] + replacement + expected[left + 2:]
        assert result == expected
        assert result[:begin] == text[:begin]
        assert result[stop:] == text[stop:]
        outputs.add(result)
    assert len(outputs) == 2 ** count


@pytest.mark.parametrize("text,candidates", [
    ("食盐，食盐，食盐，食盐。", ["实验"]),
    ("食盐，食盐。", ["实验", "誓言"]),
    ("食盐。", ["实验"]),
    ("食盐，食盐。", ["实验室"]),
    ("食盐，食盐。", ["实验", "实验室"]),
])
def test_unbounded_single_or_variable_length_choices_fall_back_exactly(text, candidates):
    assert_baseline(text, span_for(text, candidates=candidates), context="前段")


def test_options_are_sanitized_by_baseline_before_counting_product():
    text = "食盐，食盐。"
    _, choices = build(text, span_for(text, candidates=[None, "", "食盐", "实验", "实验", "字" * 33]))
    assert len(choices) == 2
    assert all(list(c.edits) == ["keep", "c0"] for c in choices)
    assert apply_choices(text, choices, {"pick": "c0", "p1": "c0"}) == "实验，实验。"


@pytest.mark.parametrize("text", ["食盐，不要食盐。", "食盐，https://host/食盐", "食盐，f(食盐)", "食盐，写成食盐。"])
def test_protected_or_meta_second_position_falls_back(text):
    assert_baseline(text)


def test_protected_position_is_excluded_without_hiding_other_eligible_positions():
    text = "食盐，不要食盐。然后记录食盐。"
    state, choices = build(text)
    assert (state, choices[0]) == cv.vocabulary_choice(text, span_for(text))
    assert [c.question_id for c in choices] == ["pick", "p1"]
    assert apply_choices(text, choices, {"pick": "c0", "p1": "c0"}) == "实验，不要食盐。然后记录实验。"


def test_explicit_target_overlap_falls_back_even_when_only_part_of_surface():
    assert_baseline("食盐，食盐。", protected=[(4, 5)])


@pytest.mark.parametrize("context", ["", "食盐是上一段的词。"])
def test_supplementary_ordinal_counts_protected_occurrences_in_source(context):
    text = "食盐，食盐，食盐。"
    state, choices = build(text, context=context, protected=[(3, 5)])
    assert (state, choices[0]) == cv.vocabulary_choice(text, span_for(text), context=context)
    assert choices[1].edits["c0"] == (CandidateEdit(6, 8, "食盐", "实验"),)
    assert "第3处词语" in choices[1].question["instructions"]
    assert apply_choices(text, choices, {"p1": "c0"}) == "食盐，食盐，实验。"


@pytest.mark.parametrize("alias", [{"start": 0, "end": 2}, {"start": 4, "end": 5}])
def test_alias_overlap_including_a_kept_position_vetoes_entire_joint_choice(alias):
    assert_baseline("食盐，食盐。", aliases=[alias])


def test_nonoverlapping_alias_does_not_veto_independent_choices():
    _, choices = build("食盐，食盐。jeff", aliases=[{"start": 6, "end": 10}])
    assert len(choices) == 2


def test_overlapping_starts_veto_joint_choice():
    text = "哈哈哈，哈哈。"
    assert_baseline(text, span_for(text, surface="哈哈", candidates=["蛤蛤"]))


@pytest.mark.parametrize("marker", ["〔", "〕"])
@pytest.mark.parametrize("location", ["window", "context"])
def test_reserved_markers_veto_joint_choice(marker, location):
    text = "食盐，食盐。" + (marker if location == "window" else "")
    assert_baseline(text, context=marker + "旧" * 300 if location == "context" else "")


def test_window_is_anchored_to_original_span_with_absolute_offsets():
    text = "前" * 200 + "食盐，食盐。" + "后" * 200
    state, choices = build(text)
    assert state == "前" * 160 + "食盐，食盐。" + "后" * 156 + "\n只判断这个跨度：「食盐」"
    assert choices[0].edits["c0"] == (CandidateEdit(200, 202, "食盐", "实验"),)
    assert choices[1].edits["c0"] == (CandidateEdit(203, 205, "食盐", "实验"),)
    assert apply_choices(text, choices, {"p1": "c0"}) == "前" * 200 + "食盐，实验。" + "后" * 200


@pytest.mark.parametrize("gap", [159, 160, 161])
def test_right_clipped_or_outside_occurrence_leaves_single_baseline(gap):
    assert_baseline("食盐" + "，" * gap + "食盐")


def test_occurrence_exactly_inside_right_window_is_included():
    text = "食盐" + "，" * 158 + "食盐"
    _, choices = build(text)
    assert choices[1].edits["c0"] == (CandidateEdit(160, 162, "食盐", "实验"),)


def test_left_window_scans_before_original_span_but_excludes_clipped_occurrence():
    text = "食盐" + "，" * 159 + "食盐，食盐"
    state, choices = build(text, span_for(text, start=161))
    assert state == "盐" + "，" * 159 + "食盐，食盐" + "\n只判断这个跨度：「食盐」"
    assert choices[0].edits["c0"] == (CandidateEdit(161, 163, "食盐", "实验"),)
    assert choices[1].edits["c0"] == (CandidateEdit(164, 166, "食盐", "实验"),)
    text = "食盐，食盐"
    state, choices = build(text, span_for(text, start=3))
    assert (state, choices[0]) == cv.vocabulary_choice(text, span_for(text, start=3))
    assert choices[1].question_id == "p0"
    assert choices[1].edits["c0"] == (CandidateEdit(0, 2, "食盐", "实验"),)


def test_invalid_or_empty_base_choice_stays_none():
    assert build("食盐，食盐", span_for("食盐，食盐", candidates=[])) is None
    assert build("食盐，食盐", {"start": 1, "end": 3, "surface": "食盐"}) is None


def corrector(**kwargs):
    return sc.StreamingHotwordCorrector(
        kwargs.pop("hotwords", ["实验"]), enabled=kwargs.pop("enabled", True),
        provider=kwargs.pop("provider", "jev"), timeout_s=1.5,
        contextual_aliases=kwargs.pop("contextual_aliases", ALIASES), **kwargs,
    )


@pytest.mark.parametrize("answers,expected", [
    ({"pick": "keep", "p1": "keep"}, "食盐结果，食盐用量。"),
    ({"pick": "keep", "p1": "c0"}, "食盐结果，实验用量。"),
    ({"pick": "c0", "p1": "keep"}, "实验结果，食盐用量。"),
    ({"pick": "c0", "p1": "c0"}, "实验结果，实验用量。"),
    ({"pick": None, "p1": "c0"}, "食盐结果，实验用量。"),
    ({"pick": "c0", "p1": None}, "实验结果，食盐用量。"),
    ({"pick": "unknown", "p1": "c0"}, "食盐结果，实验用量。"),
    ({"pick": "c0", "p1": "unknown"}, "实验结果，食盐用量。"),
    ({"p1": "c0"}, "食盐结果，实验用量。"),
    ({"pick": "c0"}, "实验结果，食盐用量。"),
    ({}, "食盐结果，食盐用量。"),
    (None, "食盐结果，食盐用量。"),
])
def test_jev_one_request_independent_abstentions_and_no_weak_retry(monkeypatch, answers, expected):
    calls, published = [], []
    def ask(timeout, state, questions, **policy):
        calls.append((timeout, state, questions, policy))
        return answers
    monkeypatch.setattr(sc, "request_jev_choices", ask)
    c = corrector(context="前段" * 200)
    original_publish = c._publish_partial
    def publish(job, text):
        published.append(text)
        original_publish(job, text)
    monkeypatch.setattr(c, "_publish_partial", publish)
    text = "食盐结果，食盐用量。"
    try:
        assert c.finish(text) == expected
        assert c.finish(text) == expected
        assert c.poll(text) == expected
        assert len(calls) == 1
        timeout, state, questions, policy = calls[0]
        assert 0 < timeout <= 1.5
        baseline_state, baseline_choice = cv.vocabulary_choice(text, span_for(text), context="前段" * 200)
        assert state == baseline_state
        assert set(questions) == {"pick", "p1"}
        assert questions["pick"] == baseline_choice.question
        assert questions["p1"] == {
            "type": "choice",
            "instructions": "依据完整上下文，只判断第2处词语：〔食盐〕用量。其他位置的用词不属于本题。",
            "criteria": {"keep": "此处应使用食盐：食盐用量", "c0": "此处应使用实验：实验用量"},
        }
        assert policy["min_top"] == 0.70
        assert policy["min_margin"] == 0.20
        assert published == ([] if expected == text else [expected])
    finally:
        c.close()


@pytest.mark.parametrize("text,hotwords", [
    ("食盐结果，写成食盐。", ["实验"]),
    ("食盐结果，不要食盐。", ["实验"]),
    ("食盐结果，食盐味道。", ["实验", "咸味→盐味"]),
])
def test_request_uses_original_guards_for_each_position(monkeypatch, text, hotwords):
    calls = []
    def ask(timeout, state, questions, **policy):
        calls.append((state, questions))
        return {"pick": "c0"}
    monkeypatch.setattr(sc, "request_jev_choices", ask)
    c = corrector(hotwords=hotwords)
    try:
        assert c.finish(text) == "实验" + text[2:]
        expected = cv.vocabulary_choice(text, span_for(text))
        assert calls == [(expected[0], question_payload([expected[1]]))]
    finally:
        c.close()


@pytest.mark.parametrize("quote", list("`\"'“”‘’「」『』【】"))
def test_global_quote_prepare_guard_still_blocks_all_requests(monkeypatch, quote):
    def forbidden(*args, **kwargs):
        pytest.fail("quoted text must not be judged")
    monkeypatch.setattr(sc, "request_jev_choices", forbidden)
    c = corrector()
    try:
        text = "食盐，食盐。" + "远" * 200 + quote
        assert c.finish(text) == text
    finally:
        c.close()


@pytest.mark.parametrize("kwargs", [{"enabled": False}, {"contextual_aliases": []}])
def test_inactive_jev_stays_disabled(monkeypatch, kwargs):
    def forbidden(*args, **options):
        pytest.fail("inactive provider must not request")
    monkeypatch.setattr(sc, "request_jev_choices", forbidden)
    c = corrector(**kwargs)
    try:
        assert c.finish("食盐，食盐") == "食盐，食盐"
        assert c._thread is None
    finally:
        c.close()


def test_semif_still_uses_one_original_span_and_full_original_state(monkeypatch):
    calls = []
    def ask(session, endpoint, timeout, state, questions):
        calls.append((state, questions))
        return {"pick": "c0"}
    monkeypatch.setattr(sc, "request_choices", ask)
    c = corrector(provider="semif", endpoint="http://127.0.0.1:9", context="前段")
    try:
        assert c.finish("食盐结果，食盐用量。") == "实验结果，食盐用量。"
        assert len(calls) == 1
        state, questions = calls[0]
        assert state == "上一段：前段\n食盐结果，食盐用量。\n只判断这个跨度：「食盐」"
        assert questions["pick"]["criteria"] == {
            "keep": "原句中的食盐在此语境正确，保持原文。食盐结果，食盐用量。",
            "c0": "此语境应该使用实验，应把食盐替换为实验，其余不变。实验结果，食盐用量。",
        }
    finally:
        c.close()


@pytest.mark.parametrize("after_timeout", ["keep_cached", "cancel", "close"])
def test_joint_timeout_cache_survives_late_alias_until_invalidated(monkeypatch, after_timeout):
    """Timeout retains both decided edits; a late alias cannot overwrite them."""
    entered, release = Event(), Event()
    calls = []

    def ask(timeout, state, questions, **policy):
        calls.append(deepcopy(questions))
        if "pick" in questions:
            return {"pick": "c0", "p1": "c0"}
        entered.set()
        assert release.wait(3), "test did not release the old alias reply"
        return {"role": "tool"}

    monkeypatch.setattr(sc, "request_jev_choices", ask)
    c = sc.StreamingHotwordCorrector(
        ["实验"], enabled=True, provider="jev", timeout_s=0.4, contextual_aliases=ALIASES,
    )
    text = "食盐结果，食盐用量。打开jeff工具。"
    bundle = "实验结果，实验用量。打开jeff工具。"
    try:
        assert c.submit(text) == text
        assert entered.wait(1), "alias request must follow the complete word bundle"
        with c._cv:
            assert c._inflight.partial == bundle
        # The pending alias is deliberately held beyond finish's budget.
        assert c.finish(text) == bundle
        assert c.finish(text) == bundle
        assert c.poll(text) == bundle
        assert [set(q) for q in calls] == [{"pick", "p1"}, {"role"}]
        assert set(calls[0]) == {"pick", "p1"}

        if after_timeout == "cancel":
            c.cancel()
        elif after_timeout == "close":
            c.close()
        expected = bundle if after_timeout == "keep_cached" else text
        assert c.poll(text) == expected
        release.set()
        # Wait for the real worker's post-judge publish/cache path, not merely
        # for the fake request to return: that is where stale replies can leak.
        with c._cv:
            assert c._cv.wait_for(lambda: c._inflight is None, timeout=1)
        assert c.poll(text) == expected
        assert [set(q) for q in calls] == [{"pick", "p1"}, {"role"}]
        if after_timeout == "keep_cached":
            assert c.finish(text) == bundle
            c.cancel()
            assert c.poll(text) == text
        elif after_timeout == "close":
            assert c.finish(text) == text
    finally:
        release.set()
        c.close()
        if c._thread is not None:
            c._thread.join(timeout=1)
            assert not c._thread.is_alive()


@pytest.mark.parametrize("invalidate", ["new_snapshot", "cancel", "close"])
def test_late_old_joint_bundle_cannot_publish_after_invalidation(monkeypatch, invalidate):
    """A delayed two-edit answer cannot seed a new snapshot/generation cache."""
    entered, release = Event(), Event()
    calls = []

    def ask(timeout, state, questions, **policy):
        calls.append((state, deepcopy(questions)))
        if len(calls) == 1:
            entered.set()
            assert release.wait(3), "test did not release the old word reply"
            return {"pick": "c0", "p1": "c0"}
        return {"pick": "keep", "p1": "c0"}

    monkeypatch.setattr(sc, "request_jev_choices", ask)
    c = corrector()
    old = "食盐旧记录，食盐旧用量。"
    new = "食盐新记录，食盐新用量。"
    try:
        assert c.submit(old) == old
        assert entered.wait(1)
        assert c.poll(old) == old
        if invalidate == "new_snapshot":
            target, expected = new, "食盐新记录，实验新用量。"
            assert c.submit(new) == new
        elif invalidate == "cancel":
            c.cancel()
            # Same text, new generation: the old two-position answer must not win over the new answer.
            target, expected = old, "食盐旧记录，实验旧用量。"
            assert c.submit(old) == old
        else:
            c.close()
            target, expected = old, old
        assert c.poll(old) == old
        assert c.poll(target) == target
        release.set()
        with c._cv:
            assert c._cv.wait_for(lambda: c._inflight is None and c._pending is None, timeout=1)
        assert c.finish(target) == expected
        assert c.poll(target) == expected
        assert c.finish(target) == expected
        assert len(calls) == (1 if invalidate == "close" else 2)
        assert set(calls[0][1]) == {"pick", "p1"}
        if invalidate == "new_snapshot":
            assert c.poll(old) == old
            assert "新记录" in calls[1][0]
        if invalidate != "close":
            c.cancel()
            assert c.poll(target) == target
    finally:
        release.set()
        c.close()
        if c._thread is not None:
            c._thread.join(timeout=1)
            assert not c._thread.is_alive()


def test_real_generated_alias_on_kept_occurrence_forces_baseline_request(monkeypatch):
    """Even a first-only assignment must not cover an alias-owned second slot."""
    calls = []

    def ask(timeout, state, questions, **policy):
        calls.append((state, deepcopy(questions)))
        return {"pick": "c0"} if "pick" in questions else {"role": "person"}

    monkeypatch.setattr(sc, "request_jev_choices", ask)
    c = corrector(contextual_aliases=[{"heard": "食盐工具", "word": "SaltApp", "meaning": "软件工具"}])
    text = "食盐结果，打开食盐工具。"
    try:
        deterministic, span, aliases = c._prepare(text)
        assert deterministic == text
        assert (span["start"], span["end"], span["surface"]) == (0, 2, "食盐")
        # Exercise the actual alias generator; do not inject hand-built spans.
        assert [(a["start"], a["end"], a["surface"]) for a in aliases] == [(7, 11, "食盐工具")]
        # The first-only output keeps the covered second occurrence. Its alias
        # still vetoes the joint payload before any option is selected.
        assert c.finish(text) == "实验结果，打开食盐工具。"
        baseline_state, baseline_choice = cv.vocabulary_choice(text, span)
        assert calls[0] == (baseline_state, question_payload([baseline_choice]))
        assert len(calls) == 2
        assert set(calls[1][1]) == {"role"}
        assert "食盐工具" in calls[1][0]
        assert c.finish(text) == "实验结果，打开食盐工具。"
        assert len(calls) == 2
    finally:
        c.close()


@pytest.mark.parametrize("separator", list("，。！？；,:;!?\n"))
def test_supplementary_anchor_stops_at_nearest_probe_separator(separator):
    text = "食盐前段" + separator + "左食盐右" + separator + "尾部"
    _, choices = build(text)
    assert choices[1].question["instructions"] == INSTRUCTIONS.format(
        ordinal=2, before="左", surface="食盐", after="右",
    )
    assert choices[1].question["criteria"]["c0"] == "此处应使用实验：左实验右"


def test_midpoint_anchors_are_disjoint_without_additional_clipping():
    # Selected baseline is the middle position; p0/p2 retain scan ordinals.
    text = "食盐" + "甲" * 61 + "食盐" + "乙" * 61 + "食盐" + "丙" * 60
    state, choices = build(text, span_for(text, start=63))
    assert (state, choices[0]) == cv.vocabulary_choice(text, span_for(text, start=63))
    assert [c.question_id for c in choices] == ["pick", "p0", "p2"]
    assert choices[1].question["instructions"] == INSTRUCTIONS.format(
        ordinal=1, before="", surface="食盐", after="甲" * 30,
    )
    assert choices[2].question["instructions"] == INSTRUCTIONS.format(
        ordinal=3, before="乙" * 31, surface="食盐", after="丙" * 60,
    )
    assert choices[1].question["criteria"]["c0"] == "此处应使用实验：实验" + "甲" * 30
    assert choices[2].question["criteria"]["c0"] == "此处应使用实验：" + "乙" * 31 + "实验" + "丙" * 60
    assert choices[1].edits["c0"] == (CandidateEdit(0, 2, "食盐", "实验"),)
    assert choices[2].edits["c0"] == (CandidateEdit(126, 128, "食盐", "实验"),)
    # Supplemental evidence ranges [0,32) and [95,188) cannot mix.
    assert 32 <= 95


REGRESSIONS = json.loads(
    (Path(__file__).parent / "fixtures" / "contextual_occurrence_regressions.json").read_text()
)


@pytest.mark.parametrize("case", REGRESSIONS, ids=lambda case: case["id"])
def test_durable_regressions_whitelist_can_express_gold_without_model_claim(case):
    """Structural reachability only: gold supplies answers; no model is invoked."""
    surface, replacement, count = {
        "OE_STRUCT:SO008": ("事迹", "试剂", 2),
        "OE_SEALED:H036": ("工事", "公事", 3),
        "OE_SEALED:N006": ("启明", "启铭", 2),
    }[case["id"]]
    text, gold = case["input"], case["expected"]
    span = span_for(text, surface=surface, candidates=[replacement])
    state, choices = build(text, span, context=case["context"])
    assert (state, choices[0]) == cv.vocabulary_choice(text, span, context=case["context"])
    positions = [i for i in range(len(text)) if text.startswith(surface, i)]
    assert len(choices) == len(positions) == count
    answers = {}
    for ordinal, (choice, left) in enumerate(zip(choices, positions, strict=True)):
        assert choice.question_id == ("pick" if ordinal == 0 else f"p{ordinal}")
        assert choice.edits == {
            "keep": (), "c0": (CandidateEdit(left, left + len(surface), surface, replacement),),
        }
        gold_word = gold[left:left + len(surface)]
        assert gold_word in (surface, replacement)
        answers[choice.question_id] = "keep" if gold_word == surface else "c0"
    assert apply_choices(text, choices, answers) == gold
