"""Shared contextual choices keep per-type protections and lifecycle budgets."""
from recordian import streaming_correction as sc


def test_active_word_choice_uses_context_and_preserves_existing_policy(monkeypatch):
    calls = []
    def ask(timeout, state, questions, **options):
        calls.append((state, questions, options))
        return {'pick': 'c0'}
    monkeypatch.setattr(sc, 'request_jev_choices', ask)
    corrector = sc.StreamingHotwordCorrector(['实验', '食盐'], enabled=True, provider='jev', timeout_s=1.5, context='正在汇总物理实验。', contextual_aliases=[{'heard':'jeff','word':'jev','meaning':'软件工具'}])
    try:
        assert corrector.finish('请把食盐结果记入报告。') == '请把实验结果记入报告。'
        assert len(calls) == 1
        state, _, policy = calls[0]
        assert '物理实验' in state
        assert policy['min_top'] == sc.WORD_MIN_TOP == 0.70
        assert policy['min_margin'] == sc.WORD_MIN_MARGIN == 0.20
        assert corrector.finish('请把食盐结果记入报告。') == '请把实验结果记入报告。'
        assert len(calls) == 1
    finally:
        corrector.close()


def test_explicit_replacement_output_cannot_be_reversed_by_homophone_choice(monkeypatch):
    calls = []
    def ask(*args, **options):
        calls.append(args)
        return {'pick': 'c0'}
    monkeypatch.setattr(sc, 'request_jev_choices', ask)
    corrector = sc.StreamingHotwordCorrector(['食盐→实验', '食盐'], enabled=True, provider='jev', timeout_s=1.5, contextual_aliases=[{'heard':'jeff','word':'jev','meaning':'软件工具'}])
    try:
        assert corrector.finish('请把食盐结果记入报告。') == '请把实验结果记入报告。'
        assert calls == []
    finally:
        corrector.close()


def test_quoted_vocabulary_is_literal_even_when_alias_enables_network(monkeypatch):
    calls = []
    def ask(*args, **options):
        calls.append(args)
        return {'pick': 'c0'}
    monkeypatch.setattr(sc, 'request_jev_choices', ask)
    corrector = sc.StreamingHotwordCorrector(['实验', '食盐'], enabled=True, provider='jev', timeout_s=1.5, contextual_aliases=[{'heard':'jeff','word':'jev','meaning':'软件工具'}])
    try:
        text = '原话是“把食盐结果记录下来”。'
        assert corrector.finish(text) == text
        assert calls == []
    finally:
        corrector.close()


def test_explicit_replacement_target_is_also_protected_from_software_alias(monkeypatch):
    calls = []
    def ask(*args, **kwargs):
        calls.append(args)
        return {'role': 'tool'}
    monkeypatch.setattr(sc, 'request_jev_choices', ask)
    corrector = sc.StreamingHotwordCorrector(['克劳动→克劳德'], enabled=True, timeout_s=1.5, contextual_aliases=[{'heard':'克劳德','word':'Claude','meaning':'软件工具'}])
    try:
        assert corrector.finish('请打开克劳动工具。') == '请打开克劳德工具。'
        assert calls == []
    finally:
        corrector.close()


def test_protected_first_candidate_does_not_hide_later_eligible_word(monkeypatch):
    calls = []
    def ask(timeout, state, questions, **kwargs):
        calls.append(questions)
        return {'pick':'c0'}
    monkeypatch.setattr(sc, 'request_jev_choices', ask)
    corrector = sc.StreamingHotwordCorrector(['食盐→实验', '食盐', '湿气'], enabled=True, timeout_s=1.5, contextual_aliases=[{'heard':'jeff','word':'jev','meaning':'软件工具'}])
    try:
        assert corrector.finish('请记录食盐结果，地下室的时期很重。') == '请记录实验结果，地下室的湿气很重。'
        assert len(calls) == 1
    finally:
        corrector.close()


def test_protected_aliases_do_not_consume_question_budget(monkeypatch):
    calls = []
    def ask(*args, **kwargs):
        calls.append(args)
        return {'role':'tool'}
    monkeypatch.setattr(sc, 'request_jev_choices', ask)
    corrector = sc.StreamingHotwordCorrector(['克劳动→克劳德'], enabled=True, timeout_s=1.5, contextual_aliases=[{'heard':'克劳德','word':'Claude','meaning':'软件工具'}, {'heard':'jeff','word':'jev','meaning':'软件工具'}])
    try:
        assert corrector.finish('打开克劳动工具，升级克劳动工具，重启克劳动工具，然后启动jeff工具。') == '打开克劳德工具，升级克劳德工具，重启克劳德工具，然后启动jev工具。'
        assert len(calls) == 1
    finally:
        corrector.close()
