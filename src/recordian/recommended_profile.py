"""Shared local fast-PTT profile for the tray menu and settings form.

The menu and the settings window both read these values so the recommended
Confucius profile, Chinese labels, and endpoint rules stay in one place.
This module does not read the user config directory, start services, or
embed credentials. The machine token stays in the user's own config.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

CONFUCIUS_LOCAL_WS_ENDPOINT = "ws://127.0.0.1:8321/asr_stream_api_v1"
CONFUCIUS_WS_PATH = "/asr_stream_api_v1"

# Only these realtime values are rewritten when Confucius is selected.
# Empty is the argparse default. The http://127.0.0.1:8000 forms are the
# old local HTTP default that was left behind next to confucius-asr.
_STOCK_STALE_REALTIME_KEYS = frozenset(
    {
        "",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8000/v1/realtime",
        "http://127.0.0.1:8000/v1/audio/transcriptions",
    }
)

# Recommend never overwrites these. A later edit to the profile dict must
# not start copying secrets into the form.
CREDENTIAL_KEYS = frozenset(
    {
        "asr_api_key",
        "refine_api_key",
        "remote_paste_key",
        "remote_code",
    }
)

_RECOMMENDED_LOCAL_FAST_PTT: dict[str, Any] = {
    "asr_provider": "confucius-asr",
    "asr_realtime_endpoint": CONFUCIUS_LOCAL_WS_ENDPOINT,
    "qwen_language": "auto",
    "enable_streaming_commit": True,
    "auto_hard_enter": False,
    "enable_text_refine": False,
    "enable_voice_wake": False,
    "enable_remote_paste": False,
    # Contextual correction is intentionally absent: applying the recommended
    # profile must not change a user's provider, SemIf endpoint, Jev budget,
    # enabled flag, or aliases. Fresh installs stay off via the generic default.
    "trigger_mode": "ptt",
    "hotkey": "<ctrl_r>",
    "toggle_hotkey": "<alt_r>",
    "stop_hotkey": "<ctrl_r>",
    "sample_rate": 16000,
    "channels": 1,
    "record_format": "wav",
    "record_backend": "auto",
    "commit_backend": "fcitx",
    "notify_backend": "auto",
    "enable_streaming_refine": False,
}

TRIGGER_MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("ptt", "按住说话"),
    ("toggle", "点一下开关"),
    ("oneshot", "点一下录一段"),
)

LANGUAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "自动（中英）"),
    ("Chinese", "中文"),
    ("English", "英文"),
)

PROVIDER_CHOICES: tuple[tuple[str, str], ...] = (
    ("confucius-asr", "流式 Confucius"),
    ("qwen-asr", "本机 Qwen"),
    ("http-cloud", "网络识别服务"),
)

CORRECTION_PROVIDER_CHOICES: tuple[tuple[str, str], ...] = (
    ("semif", "本地 SemIf（默认）"),
    ("jev", "官方Jev（沿用本机登录）"),
)

RECORD_BACKEND_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "自动选择"),
    ("ffmpeg-pulse", "PulseAudio（ffmpeg）"),
    ("arecord", "ALSA（arecord）"),
)

RECORD_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("wav", "WAV"),
    ("ogg", "Ogg"),
)

RECOMMENDED_PROFILE_NOTICE = (
    "已填入推荐配置，还没保存。需要流式识别服务已在运行；口令和常用词保持不变。"
)

CONFUCIUS_STOCK_MIGRATION_NOTICE = (
    "旧的默认识别地址已换成流式 Confucius 的本机地址。点「保存并生效」后才会写入。"
)

SAVE_EFFECT_INTRO = (
    "点「保存并生效」写入并让设置生效。点「取消」放弃还没保存的修改。"
)

BUSY_SAVE_MESSAGE = "请先结束当前听写，再保存设置"

# recording/processing are live dictation. busy is the next trigger arriving
# while the previous utterance is still being committed.
DICTATION_BUSY_STATUSES = frozenset({"recording", "processing", "busy"})

_HOTKEY_PARTS = re.compile(r"<([^>]+)>")
_KEY_NAMES = {
    "ctrl_r": "右 Ctrl",
    "ctrl_l": "左 Ctrl",
    "ctrl": "Ctrl",
    "alt_r": "右 Alt",
    "alt_l": "左 Alt",
    "alt": "Alt",
    "alt_gr": "AltGr",
    "shift_r": "右 Shift",
    "shift_l": "左 Shift",
    "shift": "Shift",
    "cmd_r": "右 Super",
    "cmd_l": "左 Super",
    "cmd": "Super",
    "enter": "回车",
    "space": "空格",
    "esc": "Esc",
    "tab": "Tab",
}


def recommended_profile_values() -> dict[str, Any]:
    """Return a copy of the local fast-PTT profile. No credential keys."""
    values = dict(_RECOMMENDED_LOCAL_FAST_PTT)
    overlap = CREDENTIAL_KEYS.intersection(values)
    if overlap:
        raise RuntimeError(f"recommended profile must not carry credentials: {sorted(overlap)}")
    return values


def merge_recommended_profile(current: Mapping[str, Any]) -> dict[str, Any]:
    """Stage the profile over a config mapping.

    Credential keys and every key outside the profile stay as they are,
    including unknown fields and hotword lists.
    """
    merged = dict(current)
    for key, value in recommended_profile_values().items():
        if key in CREDENTIAL_KEYS:
            continue
        merged[key] = value
    return merged


def choice_label(choices: tuple[tuple[str, str], ...], value: object) -> str:
    token = str(value)
    for item_id, label in choices:
        if item_id == token:
            return label
    return token


def choice_id(choices: tuple[tuple[str, str], ...], label: object) -> str:
    token = str(label)
    for item_id, item_label in choices:
        if item_label == token or item_id == token:
            return item_id
    return token


def humanize_hotkey(spec: str) -> str:
    raw = str(spec or "").strip()
    if not raw:
        return ""
    parts = []
    for match in _HOTKEY_PARTS.findall(raw):
        parts.append(_KEY_NAMES.get(match, match.upper() if len(match) == 1 else match))
    if not parts:
        return raw
    return " + ".join(parts)


def hotkey_action_hint(config: Mapping[str, Any] | None) -> str:
    if not config:
        return ""
    mode = str(config.get("trigger_mode") or "ptt")
    spoken = humanize_hotkey(str(config.get("hotkey") or ""))
    if not spoken:
        return ""
    if mode == "toggle":
        return f"按 {spoken} 开始或停止"
    if mode == "oneshot":
        return f"按 {spoken} 录一段"
    return f"按住 {spoken} 说话"


def status_headline(state: Any, config: Mapping[str, Any] | None = None) -> str:
    """Short tray headline: readiness and hotkey, without transcript or paths."""
    status = str(getattr(state, "status", "idle") or "idle")
    running = bool(getattr(state, "backend_running", False))
    detail = str(getattr(state, "detail", "") or "")
    if status == "recording":
        base = "正在听写"
    elif status in {"processing", "busy"}:
        base = "正在识别"
    elif status == "error":
        base = "出错"
    elif status == "stopped" or (not running and detail == "Stopped"):
        base = "已暂停"
    elif status in {"starting", "warming"} or not running:
        base = "正在准备"
    else:
        base = "就绪"
    if base in {"就绪", "已暂停", "正在准备"}:
        hint = hotkey_action_hint(config)
        if hint:
            return f"{base} · {hint}"
    return base


def _stock_key(endpoint: str) -> str:
    return str(endpoint or "").strip().rstrip("/").lower()


def is_stock_stale_realtime_endpoint(endpoint: str) -> bool:
    return _stock_key(endpoint) in _STOCK_STALE_REALTIME_KEYS


def migrate_confucius_realtime_endpoint(endpoint: str) -> str | None:
    """Return the local WS default for a known stale value, else None.

    None means "do not change this string".
    """
    if is_stock_stale_realtime_endpoint(endpoint):
        return CONFUCIUS_LOCAL_WS_ENDPOINT
    return None


def _host_port_problem(parsed: Any) -> str | None:
    """Return a short error for a missing host or a bad port. Never raises."""
    try:
        host = parsed.hostname
    except ValueError:
        return "这个地址的主机名无法识别。请检查 IPv6 是否放在方括号里。"
    if not host:
        return "这个地址缺少主机名。请写成「协议://主机:端口/路径」。"
    try:
        port = parsed.port
    except ValueError:
        return "这个地址的端口无效。端口要是 1 到 65535 的数字。"
    if port is not None and not 1 <= int(port) <= 65535:
        return "这个地址的端口无效。端口要是 1 到 65535 的数字。"
    return None


def confucius_endpoint_problem(endpoint: str) -> str | None:
    """Error for a Confucius realtime address that cannot be used as typed.

    Valid ws/wss addresses keep whatever path the user wrote, including a
    reverse-proxy path. Only a missing host or a bad port is rejected.
    Known stock HTTP defaults are not valid until the caller migrates them.
    """
    raw = str(endpoint or "").strip()
    if not raw or is_stock_stale_realtime_endpoint(raw):
        return (
            "流式 Confucius 需要 ws:// 或 wss:// 地址。"
            "旧的默认 http://127.0.0.1:8000 会换成推荐的本机地址；其它地址请自己改。"
        )
    if "://" not in raw:
        return "流式 Confucius 的地址要以 ws:// 或 wss:// 开头，并写上主机和端口。"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "这个地址的主机名无法识别。请检查 IPv6 是否放在方括号里。"
    scheme = parsed.scheme.lower()
    if scheme not in {"ws", "wss"}:
        return (
            f"流式 Confucius 只接受 ws:// 或 wss://，当前是 {scheme or '未知'}://。"
            "这个自定义地址不会被自动改掉。"
        )
    return _host_port_problem(parsed)


def http_cloud_endpoint_problem(realtime_endpoint: str, http_endpoint: str = "") -> str | None:
    """Reject WebSocket addresses and addresses without a usable host. Do not rewrite them."""
    checks = (("实时地址", realtime_endpoint), ("接口地址", http_endpoint))
    for label, value in checks:
        raw = str(value or "").strip()
        if not raw:
            continue
        if "://" not in raw:
            return f"网络识别服务的{label}需要以 http:// 或 https:// 开头，并写上主机。这个地址不会被自动改掉。"
        try:
            parsed = urlparse(raw)
        except ValueError:
            return f"网络识别服务的{label}：这个地址的主机名无法识别。请检查 IPv6 是否放在方括号里。"
        scheme = parsed.scheme.lower()
        if scheme in {"ws", "wss"}:
            return (
                f"网络识别服务不能使用 WebSocket 作为{label}。"
                "请改成 http:// 或 https://，或改用「流式 Confucius」。这个地址不会被自动改掉。"
            )
        if scheme not in {"http", "https"}:
            return f"网络识别服务的{label}需要以 http:// 或 https:// 开头。这个地址不会被自动改掉。"
        problem = _host_port_problem(parsed)
        if problem:
            return f"网络识别服务的{label}：{problem}"
    return None


def provider_endpoint_problem(
    provider: str,
    realtime_endpoint: str,
    http_endpoint: str = "",
) -> str | None:
    token = str(provider or "").strip()
    if token == "confucius-asr":
        migrated = migrate_confucius_realtime_endpoint(realtime_endpoint)
        candidate = realtime_endpoint if migrated is None else migrated
        return confucius_endpoint_problem(candidate)
    if token == "http-cloud":
        return http_cloud_endpoint_problem(realtime_endpoint, http_endpoint)
    return None


def endpoint_hints_for_provider(provider: str) -> dict[str, str]:
    token = str(provider or "").strip()
    if token == "confucius-asr":
        return {
            "asr_realtime_endpoint": (
                "流式 Confucius 使用 ws:// 或 wss://。推荐本机地址写在高级设置里，自定义路径会原样保留。"
            ),
            "asr_api_key": "识别服务的访问口令。这里不会生成口令；清空后保存会写成空。",
            "qwen_model": "流式 Confucius 不使用这个模型栏。",
        }
    if token == "http-cloud":
        return {
            "asr_realtime_endpoint": "网络识别的实时地址，以 http:// 或 https:// 开头。这里不会把它改成 WebSocket。",
            "asr_api_key": "网络识别服务的访问口令。留空表示不发送口令。",
            "qwen_model": "网络服务里的模型名，例如 Qwen/Qwen3-ASR-0.6B。",
        }
    return {
        "asr_realtime_endpoint": "本机 Qwen 不使用实时地址。已填写的值会保留。",
        "asr_api_key": "本机 Qwen 不使用这个口令。已填写的值会保留。",
        "qwen_model": "本机 Qwen 模型的文件夹或模型名。",
    }


__all__ = [
    "BUSY_SAVE_MESSAGE",
    "CONFUCIUS_LOCAL_WS_ENDPOINT",
    "CONFUCIUS_STOCK_MIGRATION_NOTICE",
    "CONFUCIUS_WS_PATH",
    "CREDENTIAL_KEYS",
    "LANGUAGE_CHOICES",
    "PROVIDER_CHOICES",
    "RECORD_BACKEND_CHOICES",
    "RECORD_FORMAT_CHOICES",
    "RECOMMENDED_PROFILE_NOTICE",
    "SAVE_EFFECT_INTRO",
    "TRIGGER_MODE_CHOICES",
    "choice_id",
    "choice_label",
    "confucius_endpoint_problem",
    "endpoint_hints_for_provider",
    "hotkey_action_hint",
    "http_cloud_endpoint_problem",
    "humanize_hotkey",
    "is_stock_stale_realtime_endpoint",
    "merge_recommended_profile",
    "migrate_confucius_realtime_endpoint",
    "provider_endpoint_problem",
    "recommended_profile_values",
    "status_headline",
]
