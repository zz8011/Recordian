"""测试 PresetManager 预设管理功能"""
from __future__ import annotations

from pathlib import Path

import pytest

from recordian.preset_manager import PresetManager


class TestPresetManagerInit:
    """测试 PresetManager 初始化"""

    def test_init_with_default_dir(self) -> None:
        """测试默认预设目录"""
        manager = PresetManager()
        assert manager.presets_dir.name == "presets"
        assert manager.presets_dir.is_absolute()

    def test_init_with_custom_dir(self, tmp_path: Path) -> None:
        """测试自定义预设目录"""
        custom_dir = tmp_path / "custom_presets"
        custom_dir.mkdir()
        manager = PresetManager(custom_dir)
        assert manager.presets_dir == custom_dir

    def test_init_with_relative_path(self) -> None:
        """测试相对路径转换为绝对路径"""
        manager = PresetManager("presets")
        assert manager.presets_dir.is_absolute()


class TestPresetManagerListPresets:
    """测试列出预设"""

    def test_list_presets_empty_dir(self, tmp_path: Path) -> None:
        """测试空目录返回空列表"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        manager = PresetManager(presets_dir)
        assert manager.list_presets() == []

    def test_list_presets_nonexistent_dir(self, tmp_path: Path) -> None:
        """测试不存在的目录返回空列表"""
        presets_dir = tmp_path / "nonexistent"
        manager = PresetManager(presets_dir)
        assert manager.list_presets() == []

    def test_list_presets_with_files(self, tmp_path: Path) -> None:
        """测试列出预设文件"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # 创建预设文件
        (presets_dir / "default.md").write_text("Default preset")
        (presets_dir / "custom.md").write_text("Custom preset")
        (presets_dir / "test.md").write_text("Test preset")

        manager = PresetManager(presets_dir)
        presets = manager.list_presets()

        assert len(presets) == 3
        assert presets == ["custom", "default", "test"]  # 排序后的结果

    def test_list_presets_ignores_non_md_files(self, tmp_path: Path) -> None:
        """测试忽略非 .md 文件"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        (presets_dir / "preset1.md").write_text("Preset 1")
        (presets_dir / "preset2.txt").write_text("Not a preset")
        (presets_dir / "preset3.json").write_text("{}")

        manager = PresetManager(presets_dir)
        presets = manager.list_presets()

        assert presets == ["preset1"]


class TestPresetManagerLoadPreset:
    """测试加载预设"""

    def test_load_preset_success(self, tmp_path: Path) -> None:
        """测试成功加载预设"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        content = "This is a test preset"
        (presets_dir / "test.md").write_text(content)

        manager = PresetManager(presets_dir)
        result = manager.load_preset("test")

        assert result == content

    def test_load_preset_removes_title(self, tmp_path: Path) -> None:
        """测试移除第一行标题"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        content = "# Title\nThis is the content"
        (presets_dir / "test.md").write_text(content)

        manager = PresetManager(presets_dir)
        result = manager.load_preset("test")

        assert result == "This is the content"
        assert "# Title" not in result

    def test_load_preset_caches_result(self, tmp_path: Path) -> None:
        """测试缓存预设内容"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        (presets_dir / "test.md").write_text("Original content")

        manager = PresetManager(presets_dir)
        result1 = manager.load_preset("test")

        # 修改文件内容
        (presets_dir / "test.md").write_text("Modified content")

        # 应该返回缓存的内容
        result2 = manager.load_preset("test")
        assert result1 == result2 == "Original content"

    def test_load_preset_not_found(self, tmp_path: Path) -> None:
        """测试加载不存在的预设"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        manager = PresetManager(presets_dir)

        with pytest.raises(FileNotFoundError, match="预设 'nonexistent' 不存在"):
            manager.load_preset("nonexistent")

    def test_load_preset_rejects_path_traversal(self) -> None:
        """测试拒绝路径遍历"""
        mgr = PresetManager()
        with pytest.raises(ValueError, match="非法预设名称"):
            mgr.load_preset("../../etc/passwd")

    def test_load_preset_rejects_dot_prefix(self) -> None:
        """测试拒绝点开头的名称"""
        mgr = PresetManager()
        with pytest.raises(ValueError, match="非法预设名称"):
            mgr.load_preset(".hidden")

    def test_load_preset_rejects_backslash(self) -> None:
        """测试拒绝反斜杠"""
        mgr = PresetManager()
        with pytest.raises(ValueError, match="非法预设名称"):
            mgr.load_preset("foo\\bar")

    def test_load_preset_with_multiline_content(self, tmp_path: Path) -> None:
        """测试加载多行内容"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        content = """# Title
Line 1
Line 2
Line 3"""
        (presets_dir / "test.md").write_text(content)

        manager = PresetManager(presets_dir)
        result = manager.load_preset("test")

        assert result == "Line 1\nLine 2\nLine 3"


class TestPresetManagerGetPresetPath:
    """测试获取预设路径"""

    def test_get_preset_path(self, tmp_path: Path) -> None:
        """测试获取预设文件路径"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        manager = PresetManager(presets_dir)
        path = manager.get_preset_path("test")

        assert path == presets_dir / "test.md"


class TestPresetManagerPresetExists:
    """测试检查预设是否存在"""

    def test_preset_exists_true(self, tmp_path: Path) -> None:
        """测试预设存在"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "test.md").write_text("Content")

        manager = PresetManager(presets_dir)
        assert manager.preset_exists("test") is True

    def test_preset_exists_false(self, tmp_path: Path) -> None:
        """测试预设不存在"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        manager = PresetManager(presets_dir)
        assert manager.preset_exists("nonexistent") is False


class TestPresetManagerClearCache:
    """测试清除缓存"""

    def test_clear_cache(self, tmp_path: Path) -> None:
        """测试清除缓存后重新加载"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        (presets_dir / "test.md").write_text("Original content")

        manager = PresetManager(presets_dir)
        result1 = manager.load_preset("test")

        # 修改文件内容
        (presets_dir / "test.md").write_text("Modified content")

        # 清除缓存
        manager.clear_cache()

        # 应该返回新内容
        result2 = manager.load_preset("test")
        assert result1 == "Original content"
        assert result2 == "Modified content"

    def test_clear_cache_empty(self, tmp_path: Path) -> None:
        """测试清除空缓存"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        manager = PresetManager(presets_dir)
        # 不应该抛出异常
        manager.clear_cache()
        assert manager._cache == {}
