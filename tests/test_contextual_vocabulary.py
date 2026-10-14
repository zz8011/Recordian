"""Vocabulary choices preserve literal text and only select supported spans."""
from recordian.contextual_choice import apply_choices
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
