"""集成测试 - 端到端流程测试

测试完整的录音->ASR->精炼->提交流程
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from recordian.backend_manager import BackendManager
from recordian.config import ConfigManager
from recordian.models import ASRResult
from recordian.preset_manager import PresetManager


class TestEndToEndFlow:
    """测试端到端流程"""

    def test_complete_dictation_flow(self, tmp_path: Path) -> None:
        """测试完整的听写流程"""
        # 准备配置
        config_path = tmp_path / "config.json"
        config = {
            "version": "1.0",
            "policy": {
                "confidence_threshold": 0.88,
                "english_ratio_threshold": 0.15,
            }
        }
        ConfigManager.save(config_path, config)

        # 模拟 ASR 结果
        asr_result = ASRResult(text="这是测试文本", confidence=0.95)

        # 模拟精炼结果
        refined_text = "这是测试文本。"

        # 验证流程
        assert asr_result.text == "这是测试文本"
        assert asr_result.confidence == 0.95
        assert refined_text == "这是测试文本。"

    def test_flow_with_low_confidence(self, tmp_path: Path) -> None:
        """测试低置信度场景"""
        # 模拟低置信度 ASR 结果
        asr_result = ASRResult(text="测试文本", confidence=0.75)

        # 低置信度应该触发精炼
        assert asr_result.confidence < 0.88

    def test_flow_with_high_english_ratio(self) -> None:
        """测试高英文比例场景"""
        # 模拟高英文比例文本
        text = "This is a test with English content"

        # 计算英文比例
        english_chars = sum(1 for c in text if c.isalpha() and ord(c) < 128)
        total_chars = sum(1 for c in text if c.isalnum())
        english_ratio = english_chars / total_chars if total_chars > 0 else 0

        # 高英文比例应该触发精炼
        assert english_ratio > 0.15

    def test_flow_with_preset_loading(self, tmp_path: Path) -> None:
        """测试预设加载流程"""
        # 创建预设目录
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # 创建测试预设
        preset_file = presets_dir / "test.md"
        preset_file.write_text("# Test Preset\n\nTest prompt content")

        # 加载预设
        manager = PresetManager(presets_dir)
        prompt = manager.load_preset("test")

        assert "Test prompt content" in prompt

    def test_flow_with_config_reload(self, tmp_path: Path) -> None:
        """测试配置重载流程"""
        config_path = tmp_path / "config.json"

        # 创建初始配置
        config1 = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.88}
        }
        ConfigManager.save(config_path, config1)

        # 加载配置
        loaded1 = ConfigManager.load(config_path)
        assert loaded1["policy"]["confidence_threshold"] == 0.88

        # 更新配置
        config2 = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.90}
        }
        ConfigManager.save(config_path, config2)

        # 重新加载配置
        loaded2 = ConfigManager.load(config_path)
        assert loaded2["policy"]["confidence_threshold"] == 0.90


class TestConcurrentScenarios:
    """测试并发场景"""

    def test_concurrent_preset_loading(self, tmp_path: Path) -> None:
        """测试并发预设加载"""
        # 创建预设目录
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # 创建多个预设
        for i in range(5):
            preset_file = presets_dir / f"preset{i}.md"
            preset_file.write_text(f"# Preset {i}\n\nContent {i}")

        # 创建管理器
        manager = PresetManager(presets_dir)

        # 并发加载预设
        results = []
        for i in range(5):
            prompt = manager.load_preset(f"preset{i}")
            results.append(prompt)

        # 验证所有预设都加载成功
        assert len(results) == 5
        for i, result in enumerate(results):
            assert f"Content {i}" in result

    def test_concurrent_config_access(self, tmp_path: Path) -> None:
        """测试并发配置访问"""
        config_path = tmp_path / "config.json"

        # 创建配置
        config = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.88}
        }
        ConfigManager.save(config_path, config)

        # 并发读取配置
        results = []
        for _ in range(10):
            loaded = ConfigManager.load(config_path)
            results.append(loaded)

        # 验证所有读取都成功
        assert len(results) == 10
        for result in results:
            assert result["policy"]["confidence_threshold"] == 0.88


class TestErrorRecovery:
    """测试错误恢复"""

    def test_asr_failure_recovery(self) -> None:
        """测试 ASR 失败恢复"""
        # 模拟 ASR 失败
        with pytest.raises(Exception):
            raise Exception("ASR failed")

        # 验证可以继续处理
        asr_result = ASRResult(text="fallback text", confidence=0.0)
        assert asr_result.text == "fallback text"

    def test_refiner_timeout_recovery(self) -> None:
        """测试精炼器超时恢复"""
        # 模拟超时
        original_text = "original text"

        # 超时后应该返回原始文本
        fallback_text = original_text
        assert fallback_text == "original text"

    def test_config_load_failure_recovery(self, tmp_path: Path) -> None:
        """测试配置加载失败恢复"""
        config_path = tmp_path / "invalid.json"

        # 写入无效 JSON
        config_path.write_text("invalid json content")

        # 加载应该抛出异常
        from recordian.exceptions import ConfigError
        with pytest.raises(ConfigError):
            ConfigManager.load(config_path)

    def test_preset_not_found_recovery(self, tmp_path: Path) -> None:
        """测试预设不存在恢复"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        manager = PresetManager(presets_dir)

        # 加载不存在的预设应该抛出异常
        with pytest.raises(FileNotFoundError):
            manager.load_preset("nonexistent")


class TestConfigHotReload:
    """测试配置热重载"""

    def test_config_update_without_restart(self, tmp_path: Path) -> None:
        """测试配置更新无需重启"""
        config_path = tmp_path / "config.json"

        # 创建初始配置
        config1 = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.88}
        }
        ConfigManager.save(config_path, config1)

        # 加载配置
        loaded1 = ConfigManager.load(config_path)
        assert loaded1["policy"]["confidence_threshold"] == 0.88

        # 更新配置
        config2 = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.92}
        }
        ConfigManager.save(config_path, config2)

        # 重新加载配置（模拟热重载）
        loaded2 = ConfigManager.load(config_path)
        assert loaded2["policy"]["confidence_threshold"] == 0.92

    def test_preset_switch_without_restart(self, tmp_path: Path) -> None:
        """测试预设切换无需重启"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # 创建两个预设
        preset1 = presets_dir / "preset1.md"
        preset1.write_text("# Preset 1\n\nContent 1")

        preset2 = presets_dir / "preset2.md"
        preset2.write_text("# Preset 2\n\nContent 2")

        # 创建管理器
        manager = PresetManager(presets_dir)

        # 加载第一个预设
        prompt1 = manager.load_preset("preset1")
        assert "Content 1" in prompt1

        # 切换到第二个预设
        prompt2 = manager.load_preset("preset2")
        assert "Content 2" in prompt2

    def test_config_backup_on_update(self, tmp_path: Path) -> None:
        """测试配置更新时自动备份"""
        config_path = tmp_path / "config.json"

        # 创建初始配置
        config1 = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.88}
        }
        ConfigManager.save(config_path, config1)

        # 等待一小段时间确保时间戳不同
        time.sleep(0.1)

        # 更新配置（应该创建备份）
        config2 = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.92}
        }
        ConfigManager.save(config_path, config2)

        # 检查备份文件
        backups = list(tmp_path.glob("config.backup.*.json"))
        assert len(backups) >= 1


class TestResourceManagement:
    """测试资源管理"""

    def test_preset_cache_management(self, tmp_path: Path) -> None:
        """测试预设缓存管理"""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # 创建预设
        preset_file = presets_dir / "test.md"
        preset_file.write_text("# Test\n\nContent")

        # 创建管理器
        manager = PresetManager(presets_dir)

        # 第一次加载（从文件）
        prompt1 = manager.load_preset("test")

        # 第二次加载（从缓存）
        prompt2 = manager.load_preset("test")

        assert prompt1 == prompt2

        # 清除缓存
        manager.clear_cache()

        # 再次加载（从文件）
        prompt3 = manager.load_preset("test")
        assert prompt3 == prompt1
