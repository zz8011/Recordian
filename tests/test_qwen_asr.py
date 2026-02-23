import inspect


def test_qwen_asr_no_debug_print() -> None:
    """qwen_asr.py 不应包含 [DEBUG] print 语句"""
    from recordian.providers import qwen_asr
    source = inspect.getsource(qwen_asr)
    assert "[DEBUG]" not in source, "qwen_asr.py 仍包含 [DEBUG] print 语句"
