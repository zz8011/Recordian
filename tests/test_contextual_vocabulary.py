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


def test_instructions_require_clear_evidence_and_preserve_reasonable_original():
    text = '窗边传来琴声。'
    _, choice = vocabulary_choice(
        text, {'start': 4, 'end': 6, 'surface': '琴声', 'candidates': ['禽声']},
    )
    assert choice.question['instructions'] == (
        '这是整句识别假设。只有证据明确表明原词有误且候选正确时才替换；'
        '原词有合理解释或证据不足时必须保持。只能选择一个候选条件，不要改条件以外的字，不要删字。'
    )


def test_keep_explicitly_represents_insufficient_evidence_and_is_noop():
    text = '窗边传来琴声。'
    _, choice = vocabulary_choice(
        text, {'start': 4, 'end': 6, 'surface': '琴声', 'candidates': ['禽声']},
    )
    assert choice.question['criteria']['keep'] == (
        '原句中的琴声有合理解释，或者没有足够证据排除原词，应保持原文。窗边传来琴声。'
    )
    assert choice.edits['keep'] == ()
    assert apply_choices(text, [choice], {'pick': 'keep'}) == text


@pytest.mark.parametrize('target', ['first', 'second'])
def test_repeated_surface_retains_full_cross_clause_window_and_absolute_edit(target):
    text = '长廊尽头传来琴声，学徒记录琴声的节拍。'
    start = text.index('琴声') if target == 'first' else text.rindex('琴声')
    changed = (
        '长廊尽头传来禽声，学徒记录琴声的节拍。' if target == 'first'
        else '长廊尽头传来琴声，学徒记录禽声的节拍。'
    )
    state, choice = vocabulary_choice(
        text, {'start': start, 'end': start + 2, 'surface': '琴声', 'candidates': ['禽声']},
    )
    assert state == text + '\n只判断这个跨度：「琴声」'
    assert choice.question['criteria'] == {
        'keep': '原句中的琴声有合理解释，或者没有足够证据排除原词，应保持原文。' + text,
        'c0': '此语境应该使用禽声，应把琴声替换为禽声，其余不变。' + changed,
    }
    assert choice.edits['c0'] == (CandidateEdit(start, start + 2, '琴声', '禽声'),)
    assert apply_choices(text, [choice], {'pick': 'c0'}) == changed


def test_focus_flanks_and_previous_tail_remain_bounded_without_localization():
    text = '林' * 200 + '琴声' + '云' * 200
    state, choice = vocabulary_choice(
        text, {'start': 200, 'end': 202, 'surface': '琴声', 'candidates': ['禽声']},
        context='旧' * 300 + '钟声渐歇。',
    )
    focus = '林' * 160 + '琴声' + '云' * 160
    assert state == '上一段：' + '旧' * 251 + '钟声渐歇。\n' + focus + '\n只判断这个跨度：「琴声」'
    assert choice.question['criteria']['c0'] == (
        '此语境应该使用禽声，应把琴声替换为禽声，其余不变。' + '林' * 160 + '禽声' + '云' * 160
    )
    assert apply_choices(text, [choice], {'pick': 'c0'}) == '林' * 200 + '禽声' + '云' * 200


def test_keep_retains_option_slot_and_candidate_length_deduplication_bounds():
    text = '窗边传来琴声。'
    _, choice = vocabulary_choice(
        text,
        {'start': 4, 'end': 6, 'surface': '琴声',
         'candidates': ['', None, '琴声', '羽' * 33, '禽声', '禽声', '羽' * 32,
                        '甲声', '乙声', '丙声', '丁声', '戊声', '己声']},
    )
    assert list(choice.question['criteria']) == ['keep', 'c0', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6']
    assert choice.edits['keep'] == ()
    assert [choice.edits[key][0].replacement for key in list(choice.edits)[1:]] == [
        '禽声', '羽' * 32, '甲声', '乙声', '丙声', '丁声', '戊声',
    ]
    assert apply_choices(text, [choice], {'pick': 'c1'}) == '窗边传来' + '羽' * 32 + '。'
    assert apply_choices(text, [choice], {'pick': 'c7'}) == text
