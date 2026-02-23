import inspect


def test_model_uses_eval_not_training_flag() -> None:
    """应使用 model.eval() 而非直接设置 training = False"""
    from recordian.providers import qwen_text_refiner
    source = inspect.getsource(qwen_text_refiner)
    assert "training = False" not in source, "应使用 self._model.eval()"
    assert ".eval()" in source, "缺少 self._model.eval() 调用"
