#!/usr/bin/env python3
"""语音唤醒诊断工具"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def print_check(name: str, passed: bool, details: str = "") -> None:
    """打印检查结果"""
    symbol = "✓" if passed else "✗"
    color = "\033[92m" if passed else "\033[91m"
    reset = "\033[0m"
    print(f"{color}{symbol}{reset} {name}")
    if details:
        print(f"  {details}")


def check_config() -> tuple[bool, dict[str, Any]]:
    """检查配置文件"""
    config_path = Path.home() / ".config" / "recordian" / "hotkey.json"

    if not config_path.exists():
        print_check("配置文件", False, f"文件不存在: {config_path}")
        return False, {}

    try:
        with open(config_path) as f:
            config = json.load(f)

        enable_voice_wake = config.get("enable_voice_wake", False)
        print_check("配置文件", True, f"路径: {config_path}")
        print_check("语音唤醒开关", enable_voice_wake,
                   f"enable_voice_wake = {enable_voice_wake}")

        return True, config
    except Exception as e:
        print_check("配置文件", False, f"读取失败: {e}")
        return False, {}


def check_model_files(config: dict[str, Any]) -> bool:
    """检查模型文件"""
    required_keys = ["wake_encoder", "wake_decoder", "wake_joiner", "wake_tokens"]
    all_exist = True

    for key in required_keys:
        path_str = config.get(key, "")
        if not path_str:
            print_check(f"模型配置 {key}", False, "配置中未设置")
            all_exist = False
            continue

        path = Path(path_str)
        exists = path.exists()
        print_check(f"模型文件 {key}", exists,
                   f"{'存在' if exists else '不存在'}: {path}")
        if not exists:
            all_exist = False

    return all_exist


def check_keywords_file(config: dict[str, Any]) -> bool:
    """检查关键词文件"""
    cache_dir = Path.home() / ".cache" / "recordian" / "wake"
    keywords_file = cache_dir / "keywords.txt"

    if not keywords_file.exists():
        print_check("关键词文件", False, f"文件不存在: {keywords_file}")
        return False

    try:
        with open(keywords_file) as f:
            lines = [line.strip() for line in f if line.strip()]

        print_check("关键词文件", True,
                   f"路径: {keywords_file}\n  包含 {len(lines)} 个关键词变体")

        # 显示前5个关键词
        if lines:
            print("  前5个关键词:")
            for line in lines[:5]:
                # 提取显示名称（@后面的部分）
                if "@" in line:
                    display = line.split("@")[1].split(":")[0].strip()
                    print(f"    - {display}")

        return True
    except Exception as e:
        print_check("关键词文件", False, f"读取失败: {e}")
        return False


def check_dependencies() -> bool:
    """检查依赖库"""
    all_ok = True

    # 检查 sherpa_onnx
    try:
        import sherpa_onnx
        version = getattr(sherpa_onnx, "__version__", "未知")
        print_check("sherpa_onnx", True, f"版本: {version}")
    except ImportError as e:
        print_check("sherpa_onnx", False, f"导入失败: {e}")
        all_ok = False

    # 检查 sounddevice
    try:
        import sounddevice as sd
        version = getattr(sd, "__version__", "未知")
        print_check("sounddevice", True, f"版本: {version}")

        # 显示默认输入设备
        try:
            default_device = sd.query_devices(kind="input")
            print(f"  默认输入设备: {default_device['name']}")
            print(f"  采样率: {default_device['default_samplerate']} Hz")
            print(f"  输入通道数: {default_device['max_input_channels']}")
        except Exception as e:
            print(f"  警告: 无法查询默认设备: {e}")
    except ImportError as e:
        print_check("sounddevice", False, f"导入失败: {e}")
        all_ok = False

    # 检查 numpy
    try:
        import numpy as np
        print_check("numpy", True, f"版本: {np.__version__}")
    except ImportError as e:
        print_check("numpy", False, f"导入失败: {e}")
        all_ok = False

    return all_ok


def check_wake_config(config: dict[str, Any]) -> None:
    """显示语音唤醒配置"""
    print("\n📋 语音唤醒配置:")

    wake_prefix = config.get("wake_prefix", [])
    wake_name = config.get("wake_name", [])
    print(f"  唤醒前缀: {', '.join(wake_prefix) if wake_prefix else '(无)'}")
    print(f"  唤醒名称: {', '.join(wake_name) if wake_name else '(无)'}")

    wake_owner_verify = config.get("wake_owner_verify", False)
    print(f"  声纹验证: {'启用' if wake_owner_verify else '禁用'}")

    if wake_owner_verify:
        threshold = config.get("wake_owner_threshold", 0.72)
        profile_path = config.get("wake_owner_profile", "")
        print(f"    阈值: {threshold}")
        print(f"    配置文件: {profile_path}")

        if profile_path:
            profile = Path(profile_path).expanduser()
            if profile.exists():
                print("    ✓ 声纹配置文件存在")
            else:
                print("    ✗ 声纹配置文件不存在")

    keyword_score = config.get("wake_keyword_score", 1.5)
    keyword_threshold = config.get("wake_keyword_threshold", 0.12)
    print(f"  关键词得分: {keyword_score}")
    print(f"  关键词阈值: {keyword_threshold}")


def main() -> int:
    """主函数"""
    print("🔍 Recordian 语音唤醒诊断工具\n")
    print("=" * 60)

    # 1. 检查配置
    print("\n1️⃣ 检查配置文件")
    print("-" * 60)
    config_ok, config = check_config()

    if not config_ok:
        print("\n❌ 配置文件检查失败，无法继续")
        return 1

    if not config.get("enable_voice_wake", False):
        print("\n⚠️  语音唤醒未启用")
        print("   请在配置中设置 enable_voice_wake = true")
        return 1

    # 2. 检查模型文件
    print("\n2️⃣ 检查模型文件")
    print("-" * 60)
    models_ok = check_model_files(config)

    # 3. 检查关键词文件
    print("\n3️⃣ 检查关键词文件")
    print("-" * 60)
    keywords_ok = check_keywords_file(config)

    # 4. 检查依赖库
    print("\n4️⃣ 检查依赖库")
    print("-" * 60)
    deps_ok = check_dependencies()

    # 5. 显示配置
    check_wake_config(config)

    # 总结
    print("\n" + "=" * 60)
    all_ok = config_ok and models_ok and keywords_ok and deps_ok

    if all_ok:
        print("✅ 所有检查通过！")
        print("\n💡 如果语音唤醒仍然不工作，可能的原因：")
        print("   1. 声纹验证阈值太高（当前: {})".format(
            config.get("wake_owner_threshold", 0.72)))
        print("   2. 关键词检测阈值太高（当前: {})".format(
            config.get("wake_keyword_threshold", 0.12)))
        print("   3. 音频流没有正确传递给 sherpa_onnx")
        print("\n建议：")
        print("   - 尝试临时禁用声纹验证: wake_owner_verify = false")
        print("   - 或降低阈值: wake_owner_threshold = 0.65")
        print("   - 或降低关键词阈值: wake_keyword_threshold = 0.08")
        return 0
    else:
        print("❌ 部分检查失败，请修复上述问题")
        return 1


if __name__ == "__main__":
    sys.exit(main())
