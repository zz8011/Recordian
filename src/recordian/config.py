from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .exceptions import ConfigError


# 配置版本号
CONFIG_VERSION = "1.0"


@dataclass(slots=True)
class Pass2PolicyConfig:
    confidence_threshold: float = 0.88
    english_ratio_threshold: float = 0.15
    pass2_timeout_ms_local: int = 900
    pass2_timeout_ms_cloud: int = 1500


@dataclass(slots=True)
class AppConfig:
    policy: Pass2PolicyConfig = field(default_factory=Pass2PolicyConfig)


class ConfigValidator:
    """配置验证器"""

    @staticmethod
    def validate(config: dict[str, Any]) -> list[str]:
        """验证配置，返回错误列表

        Args:
            config: 配置字典

        Returns:
            错误信息列表，空列表表示验证通过
        """
        errors = []

        # 验证版本号
        if "version" in config:
            version = config["version"]
            if not isinstance(version, str):
                errors.append("配置版本号必须是字符串")

        # 验证 policy 配置
        if "policy" in config:
            policy = config["policy"]
            if not isinstance(policy, dict):
                errors.append("policy 必须是对象")
            else:
                errors.extend(ConfigValidator._validate_policy(policy))

        return errors

    @staticmethod
    def _validate_policy(policy: dict[str, Any]) -> list[str]:
        """验证 policy 配置"""
        errors = []

        # 验证 confidence_threshold
        if "confidence_threshold" in policy:
            threshold = policy["confidence_threshold"]
            if not isinstance(threshold, (int, float)):
                errors.append("policy.confidence_threshold 必须是数字")
            elif not 0.0 <= threshold <= 1.0:
                errors.append("policy.confidence_threshold 必须在 0.0 到 1.0 之间")

        # 验证 english_ratio_threshold
        if "english_ratio_threshold" in policy:
            threshold = policy["english_ratio_threshold"]
            if not isinstance(threshold, (int, float)):
                errors.append("policy.english_ratio_threshold 必须是数字")
            elif not 0.0 <= threshold <= 1.0:
                errors.append("policy.english_ratio_threshold 必须在 0.0 到 1.0 之间")

        # 验证超时配置
        for key in ["pass2_timeout_ms_local", "pass2_timeout_ms_cloud"]:
            if key in policy:
                timeout = policy[key]
                if not isinstance(timeout, int):
                    errors.append(f"policy.{key} 必须是整数")
                elif timeout < 0:
                    errors.append(f"policy.{key} 必须大于等于 0")

        return errors


class ConfigMigrator:
    """配置迁移器"""

    @staticmethod
    def migrate(config: dict[str, Any]) -> dict[str, Any]:
        """迁移配置到最新版本

        Args:
            config: 旧版本配置

        Returns:
            迁移后的配置
        """
        # 获取当前版本
        current_version = config.get("version", "0.0")

        # 如果已经是最新版本，直接返回
        if current_version == CONFIG_VERSION:
            return config

        # 执行迁移
        migrated = config.copy()

        # 从 0.0 迁移到 1.0
        if current_version == "0.0":
            migrated = ConfigMigrator._migrate_0_0_to_1_0(migrated)

        # 设置版本号
        migrated["version"] = CONFIG_VERSION

        return migrated

    @staticmethod
    def _migrate_0_0_to_1_0(config: dict[str, Any]) -> dict[str, Any]:
        """从 0.0 迁移到 1.0"""
        # 0.0 版本没有版本号字段，直接添加
        migrated = config.copy()

        # 确保 policy 配置存在
        if "policy" not in migrated:
            migrated["policy"] = {}

        # 添加默认值（如果缺失）
        policy = migrated["policy"]
        if "confidence_threshold" not in policy:
            policy["confidence_threshold"] = 0.88
        if "english_ratio_threshold" not in policy:
            policy["english_ratio_threshold"] = 0.15
        if "pass2_timeout_ms_local" not in policy:
            policy["pass2_timeout_ms_local"] = 900
        if "pass2_timeout_ms_cloud" not in policy:
            policy["pass2_timeout_ms_cloud"] = 1500

        return migrated


class ConfigManager:
    """统一的配置文件管理器"""

    @staticmethod
    def load(path: Path | str) -> dict[str, Any]:
        """加载配置文件

        Args:
            path: 配置文件路径

        Returns:
            配置字典

        Raises:
            ConfigError: 配置文件格式错误或验证失败
        """
        p = Path(path).expanduser()
        if not p.exists():
            return {}

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ConfigError(f"配置文件格式错误: {path}")

            # 迁移配置
            migrated = ConfigMigrator.migrate(data)

            # 验证配置
            errors = ConfigValidator.validate(migrated)
            if errors:
                error_msg = "\n".join(f"  - {err}" for err in errors)
                raise ConfigError(f"配置验证失败:\n{error_msg}")

            return migrated

        except json.JSONDecodeError as e:
            raise ConfigError(f"配置文件 JSON 格式错误: {e}")
        except OSError as e:
            raise ConfigError(f"读取配置文件失败: {e}")

    @staticmethod
    def save(path: Path | str, config: dict[str, Any]) -> None:
        """保存配置文件

        Args:
            path: 配置文件路径
            config: 配置字典

        Raises:
            ConfigError: 保存失败
        """
        p = Path(path).expanduser()

        # 验证配置
        errors = ConfigValidator.validate(config)
        if errors:
            error_msg = "\n".join(f"  - {err}" for err in errors)
            raise ConfigError(f"配置验证失败:\n{error_msg}")

        try:
            # 创建备份
            if p.exists():
                ConfigManager.backup(p)

            # 保存配置
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except OSError as e:
            raise ConfigError(f"保存配置文件失败: {e}")

    @staticmethod
    def backup(path: Path | str, max_backups: int = 5) -> Path | None:
        """备份配置文件

        Args:
            path: 配置文件路径
            max_backups: 最大备份数量

        Returns:
            备份文件路径，如果文件不存在则返回 None
        """
        p = Path(path).expanduser()
        if not p.exists():
            return None

        # 生成备份文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = p.parent / f"{p.stem}.backup.{timestamp}{p.suffix}"

        # 复制文件
        shutil.copy2(p, backup_path)

        # 清理旧备份
        ConfigManager._cleanup_old_backups(p, max_backups)

        return backup_path

    @staticmethod
    def _cleanup_old_backups(path: Path, max_backups: int) -> None:
        """清理旧备份文件"""
        p = Path(path).expanduser()
        backup_pattern = f"{p.stem}.backup.*{p.suffix}"

        # 查找所有备份文件
        backups = sorted(
            p.parent.glob(backup_pattern),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )

        # 删除超出数量的备份
        for backup in backups[max_backups:]:
            try:
                backup.unlink()
            except OSError:
                pass  # 忽略删除失败
