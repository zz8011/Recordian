import pytest
from recordian.preset_manager import PresetManager


def test_load_preset_rejects_path_traversal() -> None:
    mgr = PresetManager()
    with pytest.raises(ValueError, match="非法预设名称"):
        mgr.load_preset("../../etc/passwd")


def test_load_preset_rejects_dot_prefix() -> None:
    mgr = PresetManager()
    with pytest.raises(ValueError, match="非法预设名称"):
        mgr.load_preset(".hidden")


def test_load_preset_rejects_backslash() -> None:
    mgr = PresetManager()
    with pytest.raises(ValueError, match="非法预设名称"):
        mgr.load_preset("foo\\bar")
