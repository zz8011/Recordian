"""测试配置管理功能"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from recordian.config import (
    CONFIG_VERSION,
    ConfigManager,
    ConfigMigrator,
    ConfigValidator,
)
from recordian.exceptions import ConfigError


class TestConfigValidator:
    """测试配置验证器"""

    def test_validate_empty_config(self) -> None:
        """测试空配置验证通过"""
        errors = ConfigValidator.validate({})
        assert errors == []

    def test_validate_valid_config(self) -> None:
        """测试有效配置验证通过"""
        config = {
            "version": "1.0",
            "policy": {
                "confidence_threshold": 0.88,
                "english_ratio_threshold": 0.15,
                "pass2_timeout_ms_local": 900,
                "pass2_timeout_ms_cloud": 1500,
            }
        }
        errors = ConfigValidator.validate(config)
        assert errors == []

    def test_validate_invalid_version_type(self) -> None:
        """测试版本号类型错误"""
        config = {"version": 1.0}
        errors = ConfigValidator.validate(config)
        assert len(errors) == 1
        assert "版本号必须是字符串" in errors[0]

    def test_validate_invalid_policy_type(self) -> None:
        """测试 policy 类型错误"""
        config = {"policy": "invalid"}
        errors = ConfigValidator.validate(config)
        assert len(errors) == 1
        assert "policy 必须是对象" in errors[0]

    def test_validate_confidence_threshold_out_of_range(self) -> None:
        """测试 confidence_threshold 超出范围"""
        config = {"policy": {"confidence_threshold": 1.5}}
        errors = ConfigValidator.validate(config)
        assert len(errors) == 1
        assert "0.0 到 1.0 之间" in errors[0]

    def test_validate_confidence_threshold_invalid_type(self) -> None:
        """测试 confidence_threshold 类型错误"""
        config = {"policy": {"confidence_threshold": "invalid"}}
        errors = ConfigValidator.validate(config)
        assert len(errors) == 1
        assert "必须是数字" in errors[0]

    def test_validate_english_ratio_threshold_out_of_range(self) -> None:
        """测试 english_ratio_threshold 超出范围"""
        config = {"policy": {"english_ratio_threshold": -0.1}}
        errors = ConfigValidator.validate(config)
        assert len(errors) == 1
        assert "0.0 到 1.0 之间" in errors[0]

    def test_validate_timeout_negative(self) -> None:
        """测试超时值为负数"""
        config = {"policy": {"pass2_timeout_ms_local": -100}}
        errors = ConfigValidator.validate(config)
        assert len(errors) == 1
        assert "必须大于等于 0" in errors[0]

    def test_validate_timeout_invalid_type(self) -> None:
        """测试超时值类型错误"""
        config = {"policy": {"pass2_timeout_ms_cloud": "invalid"}}
        errors = ConfigValidator.validate(config)
        assert len(errors) == 1
        assert "必须是整数" in errors[0]

    def test_validate_multiple_errors(self) -> None:
        """测试多个错误"""
        config = {
            "version": 123,
            "policy": {
                "confidence_threshold": 2.0,
                "pass2_timeout_ms_local": -100,
            }
        }
        errors = ConfigValidator.validate(config)
        assert len(errors) == 3


class TestConfigMigrator:
    """测试配置迁移器"""

    def test_migrate_already_latest_version(self) -> None:
        """测试已经是最新版本"""
        config = {"version": CONFIG_VERSION}
        migrated = ConfigMigrator.migrate(config)
        assert migrated["version"] == CONFIG_VERSION

    def test_migrate_from_0_0_to_1_0(self) -> None:
        """测试从 0.0 迁移到 1.0"""
        config = {"policy": {"confidence_threshold": 0.9}}
        migrated = ConfigMigrator.migrate(config)

        assert migrated["version"] == "1.0"
        assert migrated["policy"]["confidence_threshold"] == 0.9
        assert migrated["policy"]["english_ratio_threshold"] == 0.15
        assert migrated["policy"]["pass2_timeout_ms_local"] == 900
        assert migrated["policy"]["pass2_timeout_ms_cloud"] == 1500

    def test_migrate_preserves_user_settings(self) -> None:
        """测试迁移保留用户设置"""
        config = {
            "policy": {
                "confidence_threshold": 0.95,
                "english_ratio_threshold": 0.2,
            }
        }
        migrated = ConfigMigrator.migrate(config)

        assert migrated["policy"]["confidence_threshold"] == 0.95
        assert migrated["policy"]["english_ratio_threshold"] == 0.2

    def test_migrate_adds_missing_fields(self) -> None:
        """测试迁移添加缺失字段"""
        config = {}
        migrated = ConfigMigrator.migrate(config)

        assert "version" in migrated
        assert "policy" in migrated
        assert "confidence_threshold" in migrated["policy"]


class TestConfigManagerLoad:
    """测试配置加载"""

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        """测试加载不存在的文件"""
        config_path = tmp_path / "nonexistent.json"
        config = ConfigManager.load(config_path)
        assert config == {}

    def test_load_valid_config(self, tmp_path: Path) -> None:
        """测试加载有效配置"""
        config_path = tmp_path / "config.json"
        config_data = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.9}
        }
        config_path.write_text(json.dumps(config_data))

        config = ConfigManager.load(config_path)
        assert config["version"] == "1.0"
        assert config["policy"]["confidence_threshold"] == 0.9

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        """测试加载无效 JSON"""
        config_path = tmp_path / "config.json"
        config_path.write_text("invalid json")

        with pytest.raises(ConfigError, match="JSON 格式错误"):
            ConfigManager.load(config_path)

    def test_load_non_dict_json(self, tmp_path: Path) -> None:
        """测试加载非字典 JSON"""
        config_path = tmp_path / "config.json"
        config_path.write_text('["array"]')

        with pytest.raises(ConfigError, match="配置文件格式错误"):
            ConfigManager.load(config_path)

    def test_load_auto_migrates(self, tmp_path: Path) -> None:
        """测试加载时自动迁移"""
        config_path = tmp_path / "config.json"
        old_config = {"policy": {"confidence_threshold": 0.9}}
        config_path.write_text(json.dumps(old_config))

        config = ConfigManager.load(config_path)
        assert config["version"] == "1.0"

    def test_load_validates_config(self, tmp_path: Path) -> None:
        """测试加载时验证配置"""
        config_path = tmp_path / "config.json"
        invalid_config = {
            "version": "1.0",
            "policy": {"confidence_threshold": 2.0}
        }
        config_path.write_text(json.dumps(invalid_config))

        with pytest.raises(ConfigError, match="配置验证失败"):
            ConfigManager.load(config_path)


class TestConfigManagerSave:
    """测试配置保存"""

    def test_save_valid_config(self, tmp_path: Path) -> None:
        """测试保存有效配置"""
        config_path = tmp_path / "config.json"
        config = {
            "version": "1.0",
            "policy": {"confidence_threshold": 0.9}
        }

        ConfigManager.save(config_path, config)

        assert config_path.exists()
        saved_data = json.loads(config_path.read_text())
        assert saved_data["version"] == "1.0"

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        """测试保存时创建父目录"""
        config_path = tmp_path / "subdir" / "config.json"
        config = {"version": "1.0"}

        ConfigManager.save(config_path, config)

        assert config_path.exists()

    def test_save_validates_config(self, tmp_path: Path) -> None:
        """测试保存时验证配置"""
        config_path = tmp_path / "config.json"
        invalid_config = {"policy": {"confidence_threshold": 2.0}}

        with pytest.raises(ConfigError, match="配置验证失败"):
            ConfigManager.save(config_path, invalid_config)

    def test_save_creates_backup(self, tmp_path: Path) -> None:
        """测试保存时创建备份"""
        config_path = tmp_path / "config.json"

        # 创建初始配置
        config_path.write_text('{"version": "1.0"}')
        time.sleep(0.01)  # 确保时间戳不同

        # 保存新配置
        new_config = {"version": "1.0", "policy": {}}
        ConfigManager.save(config_path, new_config)

        # 检查备份文件
        backups = list(tmp_path.glob("config.backup.*.json"))
        assert len(backups) == 1


class TestConfigManagerBackup:
    """测试配置备份"""

    def test_backup_nonexistent_file(self, tmp_path: Path) -> None:
        """测试备份不存在的文件"""
        config_path = tmp_path / "nonexistent.json"
        backup_path = ConfigManager.backup(config_path)
        assert backup_path is None

    def test_backup_creates_backup_file(self, tmp_path: Path) -> None:
        """测试创建备份文件"""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"version": "1.0"}')

        backup_path = ConfigManager.backup(config_path)

        assert backup_path is not None
        assert backup_path.exists()
        assert "backup" in backup_path.name

    def test_backup_preserves_content(self, tmp_path: Path) -> None:
        """测试备份保留内容"""
        config_path = tmp_path / "config.json"
        original_content = '{"version": "1.0"}'
        config_path.write_text(original_content)

        backup_path = ConfigManager.backup(config_path)

        assert backup_path is not None
        assert backup_path.read_text() == original_content

    def test_backup_cleanup_old_backups(self, tmp_path: Path) -> None:
        """测试清理旧备份"""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"version": "1.0"}')

        # 手动创建多个备份文件（模拟历史备份）
        for i in range(7):
            timestamp = f"2024010{i}_120000"
            backup_file = tmp_path / f"config.backup.{timestamp}.json"
            backup_file.write_text('{"version": "1.0"}')

        # 创建一个新备份，应该触发清理
        ConfigManager.backup(config_path, max_backups=5)

        # 应该只保留 5 个最新的备份
        backups = list(tmp_path.glob("config.backup.*.json"))
        assert len(backups) == 5

    def test_backup_custom_max_backups(self, tmp_path: Path) -> None:
        """测试自定义最大备份数"""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"version": "1.0"}')

        # 手动创建多个备份文件
        for i in range(5):
            timestamp = f"2024010{i}_120000"
            backup_file = tmp_path / f"config.backup.{timestamp}.json"
            backup_file.write_text('{"version": "1.0"}')

        # 创建一个新备份，最多保留 3 个
        ConfigManager.backup(config_path, max_backups=3)

        backups = list(tmp_path.glob("config.backup.*.json"))
        assert len(backups) == 3
