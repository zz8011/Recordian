"""Vocabulary choices preserve literal text and only select supported spans."""
import pytest

from recordian.contextual_choice import CandidateEdit, apply_choices
from recordian.contextual_vocabulary import vocabulary_choice


def test_context_is_readonly_and_choice_only_changes_one_supported_word():
    text = '请把食盐结果记录到表格中。'
    span = {'start': 2, 'end': 4, 'surface': '食盐', 'candidates': ['实验']}
    state, choice = vocabulary_choice(text, span, context='我们刚完成这次物理实验。')
    assert '物理实验' in state
    assert apply_choices(text, [choice], {'pick': 'c0'}) == '请把实验结果记录到表格中。'
    assert apply_choices(text, [choice], {'pick': 'keep'}) == text
    assert apply_choices(text, [choice], {'pick': '实验并总结报告'}) == text


def test_late_target_has_bounded_surrounding_evidence():
    prefix = '前面的资料已经归档。' * 1000
    text = prefix + '请把食盐结果记录下来。'
    start = len(prefix) + 2
    state, choice = vocabulary_choice(text, {'start': start, 'end': start + 2, 'surface': '食盐', 'candidates': ['实验']}, context='旧话题。' * 1000)
    assert len(state) <= 1000
    assert '食盐结果' in state
    assert all(len(value) < 1000 for value in choice.question['criteria'].values())
    assert apply_choices(text, [choice], {'pick': 'c0'}) == prefix + '请把实验结果记录下来。'


def test_missing_or_shifted_target_is_not_a_question():
    assert vocabulary_choice('请记录实验结果。', {'start': 0, 'end': 2, 'surface': '食盐', 'candidates': ['实验']}) is None


def test_candidate_list_is_bounded_and_keeps_original_option():
    result = vocabulary_choice('请记录实验结果。', {'start': 3, 'end': 5, 'surface': '实验', 'candidates': ['食盐'] * 12})
    assert result is not None
    _state, choice = result
    assert len(choice.question['criteria']) == 2
    assert choice.edits['keep'] == ()


@pytest.mark.parametrize('separator', list('，。！？；：、,.!?;:\n') + ['？！\n'])
@pytest.mark.parametrize('target', ['first', 'second'])
def test_repeated_surface_in_other_clause_has_target_only_payload(separator, target):
    first, second = '园内张贴公示', '学徒抄写公示计算面积'
    text = first + separator + second
    start = text.index('公示') if target == 'first' else text.rindex('公示')
    focus = first if target == 'first' else second
    changed = '园内张贴公式' if target == 'first' else '学徒抄写公式计算面积'
    state, choice = vocabulary_choice(
        text, {'start': start, 'end': start + 2, 'surface': '公示', 'candidates': ['公式']},
    )
    assert state == focus + '\n只判断这个跨度：「公示」'
    assert choice.question == {
        'type': 'choice',
        'instructions': '这是整句识别假设。只能选择一个候选条件。不要改条件以外的字，不要删字。',
        'criteria': {
            'keep': '原句中的公示在此语境正确，保持原文。' + focus,
            'c0': '此语境应该使用公式，应把公示替换为公式，其余不变。' + changed,
        },
    }
    assert choice.edits == {'keep': (), 'c0': (CandidateEdit(start, start + 2, '公示', '公式'),)}
    expected = changed + separator + second if target == 'first' else first + separator + changed
    assert apply_choices(text, [choice], {'pick': 'c0'}) == expected


@pytest.mark.parametrize('target', ['first', 'second'])
def test_repeated_surface_in_same_clause_keeps_legacy_window(target):
    text = '庭院灯光亮起，学徒对照公示抄写公示计算面积，窗外落叶飘散。'
    start = text.index('公示') if target == 'first' else text.rindex('公示')
    changed = (
        '庭院灯光亮起，学徒对照公式抄写公示计算面积，窗外落叶飘散。'
        if target == 'first' else
        '庭院灯光亮起，学徒对照公示抄写公式计算面积，窗外落叶飘散。'
    )
    state, choice = vocabulary_choice(
        text, {'start': start, 'end': start + 2, 'surface': '公示', 'candidates': ['公式']},
    )
    assert state == text + '\n只判断这个跨度：「公示」'
    assert choice.question['criteria'] == {
        'keep': '原句中的公示在此语境正确，保持原文。' + text,
        'c0': '此语境应该使用公式，应把公示替换为公式，其余不变。' + changed,
    }
    assert apply_choices(text, [choice], {'pick': 'c0'}) == changed


@pytest.mark.parametrize('side', ['before', 'after'])
def test_repetition_outside_visible_window_preserves_ordinary_payload(side):
    text = '庭院安静。' + '林' * 180 + '学徒抄写公示计算面积。' + '云' * 180 + '仓门关闭。'
    start = text.index('公示')
    span = {'start': start, 'end': start + 2, 'surface': '公示', 'candidates': ['公式']}
    repeated = text.replace('庭院' if side == 'before' else '仓门', '公示')
    baseline_state, baseline_choice = vocabulary_choice(text, span, context='讲义留待明日复核。')
    state, choice = vocabulary_choice(repeated, span, context='讲义留待明日复核。')
    assert state == baseline_state
    assert choice == baseline_choice


def test_single_occurrence_retains_exact_legacy_payload_and_context_tail():
    text = '雨声渐歇，学徒抄写公示计算面积，窗边放着墨盒。'
    start = text.index('公示')
    state, choice = vocabulary_choice(
        text, {'start': start, 'end': start + 2, 'surface': '公示', 'candidates': ['公式']},
        context='旧' * 300 + '讲义已装订。',
    )
    assert state == '上一段：' + '旧' * 250 + '讲义已装订。\n' + text + '\n只判断这个跨度：「公示」'
    assert choice.question == {
        'type': 'choice',
        'instructions': '这是整句识别假设。只能选择一个候选条件。不要改条件以外的字，不要删字。',
        'criteria': {
            'keep': '原句中的公示在此语境正确，保持原文。雨声渐歇，学徒抄写公示计算面积，窗边放着墨盒。',
            'c0': '此语境应该使用公式，应把公示替换为公式，其余不变。雨声渐歇，学徒抄写公式计算面积，窗边放着墨盒。',
        },
    }


@pytest.mark.parametrize('side', ['left', 'right'])
def test_narrowed_clause_stays_inside_original_focus(side):
    if side == 'left':
        text = '林' * 200 + '公示记录了面积，学徒校对公示。'
        start = 200
        focus = '林' * 160 + '公示记录了面积'
        changed = '林' * 160 + '公式记录了面积'
    else:
        text = '园内张贴公示，学徒抄写公示' + '云' * 200
        start = text.rindex('公示')
        focus = '学徒抄写公示' + '云' * 160
        changed = '学徒抄写公式' + '云' * 160
    state, choice = vocabulary_choice(
        text, {'start': start, 'end': start + 2, 'surface': '公示', 'candidates': ['公式']},
        context='旧' * 300 + '公示来自前段。',
    )
    assert state == '上一段：' + '旧' * 249 + '公示来自前段。\n' + focus + '\n只判断这个跨度：「公示」'
    assert choice.question['criteria'] == {
        'keep': '原句中的公示在此语境正确，保持原文。' + focus,
        'c0': '此语境应该使用公式，应把公示替换为公式，其余不变。' + changed,
    }


def test_short_unique_target_clause_is_narrowed_without_length_heuristic():
    text = '公示，学徒抄写公示计算面积。'
    state, choice = vocabulary_choice(
        text, {'start': 0, 'end': 2, 'surface': '公示', 'candidates': ['公式']},
    )
    assert state == '公示\n只判断这个跨度：「公示」'
    assert choice.question['criteria']['c0'] == '此语境应该使用公式，应把公示替换为公式，其余不变。公式'


@pytest.mark.parametrize('suffix', ['，大家都认识多多。', '，随后休息。'])
@pytest.mark.parametrize('start,changed_prefix', [(0, '朵朵多练习'), (1, '多朵朵练习')])
def test_overlapping_occurrences_preserve_window_and_absolute_target(suffix, start, changed_prefix):
    text = '多多多练习' + suffix
    changed = changed_prefix + suffix
    state, choice = vocabulary_choice(
        text, {'start': start, 'end': start + 2, 'surface': '多多', 'candidates': ['朵朵']},
    )
    assert state == text + '\n只判断这个跨度：「多多」'
    assert choice.question['criteria'] == {
        'keep': '原句中的多多在此语境正确，保持原文。' + text,
        'c0': '此语境应该使用朵朵，应把多多替换为朵朵，其余不变。' + changed,
    }
    assert choice.edits['c0'] == (CandidateEdit(start, start + 2, '多多', '朵朵'),)
    assert apply_choices(text, [choice], {'pick': 'c0'}) == changed
