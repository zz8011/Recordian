"""Recordian 异常层次结构

定义了项目中使用的所有自定义异常类型，提供更精确的错误处理和上下文信息。
"""
from __future__ import annotations


class RecordianError(Exception):
    """Recordian 基础异常类

    所有 Recordian 自定义异常的基类。
    """
    pass


class ASRError(RecordianError):
    """ASR（语音识别）相关错误

    包括：
    - 语音识别失败
    - 音频格式错误
    - ASR 服务不可用
    """
    pass


class RefinerError(RecordianError):
    """文本精炼器相关错误

    包括：
    - LLM 推理失败
    - API 调用超时
    - 精炼结果格式错误
    """
    pass


class CommitError(RecordianError):
    """文本提交相关错误

    包括：
    - 剪贴板操作失败
    - 键盘模拟失败
    - X11/Wayland 操作失败
    """
    pass


class ConfigError(RecordianError):
    """配置相关错误

    包括：
    - 配置文件格式错误
    - 必需配置项缺失
    - 配置值无效
    """
    pass


class BackendError(RecordianError):
    """后端进程相关错误

    包括：
    - 后端进程启动失败
    - 后端进程异常退出
    - 后端通信失败
    """
    pass


class AudioError(RecordianError):
    """音频处理相关错误

    包括：
    - 音频设备不可用
    - 音频格式转换失败
    - 音频文件读写失败
    """
    pass


class TimeoutError(RecordianError):
    """超时错误

    包括：
    - ASR 超时
    - 精炼器超时
    - 后端响应超时
    """
    pass


class ResourceError(RecordianError):
    """资源相关错误

    包括：
    - 临时文件创建失败
    - 磁盘空间不足
    - 内存不足
    """
    pass
