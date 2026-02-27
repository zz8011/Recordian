"""Recordian 统一日志配置

提供统一的日志配置和管理，支持：
- 文件日志轮转
- 控制台输出
- 可配置的日志级别
- 统一的日志格式
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(
    level: int = logging.INFO,
    log_file: str | Path | None = None,
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    force_reconfigure: bool = False,
) -> logging.Logger:
    """配置 Recordian 日志系统

    Args:
        level: 日志级别（默认 INFO）
        log_file: 日志文件路径（默认 ~/.local/share/recordian/recordian.log）
        console: 是否输出到控制台（默认 True）
        max_bytes: 单个日志文件最大字节数（默认 10MB）
        backup_count: 保留的备份文件数量（默认 5）
        force_reconfigure: 强制重新配置（默认 False）

    Returns:
        配置好的 logger 实例
    """
    # 获取或创建 logger
    logger = logging.getLogger("recordian")

    # 如果已经配置过且不强制重新配置，直接返回
    if logger.handlers and not force_reconfigure:
        return logger

    # 清除现有 handlers（如果强制重新配置）
    if force_reconfigure:
        logger.handlers.clear()

    logger.setLevel(level)
    logger.propagate = False  # 不传播到根 logger

    # 统一的日志格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件 handler（带轮转）
    if log_file is None:
        log_file = Path.home() / ".local" / "share" / "recordian" / "recordian.log"
    else:
        log_file = Path(log_file)

    # 确保日志目录存在
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 控制台 handler
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """获取 logger 实例

    Args:
        name: logger 名称（默认使用 "recordian"）

    Returns:
        logger 实例
    """
    if name is None:
        return logging.getLogger("recordian")
    return logging.getLogger(f"recordian.{name}")


def set_level(level: int | str) -> None:
    """设置日志级别

    Args:
        level: 日志级别（可以是 int 或 str，如 "DEBUG", "INFO" 等）
    """
    logger = logging.getLogger("recordian")

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)


def configure_from_env() -> logging.Logger:
    """从环境变量配置日志系统

    支持的环境变量：
    - RECORDIAN_LOG_LEVEL: 日志级别（DEBUG, INFO, WARNING, ERROR）
    - RECORDIAN_LOG_FILE: 日志文件路径
    - RECORDIAN_LOG_CONSOLE: 是否输出到控制台（1/0）

    Returns:
        配置好的 logger 实例
    """
    level_str = os.environ.get("RECORDIAN_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    log_file = os.environ.get("RECORDIAN_LOG_FILE")
    console = os.environ.get("RECORDIAN_LOG_CONSOLE", "1") == "1"

    return setup_logging(level=level, log_file=log_file, console=console)
