#!/usr/bin/env python3
"""验证文档更新"""

import sys
from pathlib import Path

def check_file(filepath, keywords):
    """检查文件是否包含关键词"""
    try:
        content = Path(filepath).read_text()
        results = []
        for keyword in keywords:
            if keyword in content:
                results.append(f"  ✅ 包含: {keyword}")
            else:
                results.append(f"  ❌ 缺失: {keyword}")
        return results
    except Exception as e:
        return [f"  ❌ 错误: {e}"]

def main():
    print("=" * 60)
    print("文档验证")
    print("=" * 60)

    checks = {
        "docs/TROUBLESHOOTING.md": [
            "自动检测机制",
            "xprop",
            "Electron 应用",
            "常见问题 (FAQ)",
            "为什么微信输入有延迟",
            "如何禁用自动检测",
            "降级机制",
        ],
        "docs/USER_GUIDE.md": [
            "智能输入方式",
            "自动检测 Electron 应用",
            "支持的 Electron 应用",
            "降级机制",
            "auto-fallback",
        ],
        "QUICK_REFERENCE.md": [
            "文本上屏方式",
            "auto-fallback",
            "自动检测支持的应用",
            "降级链",
        ],
        "README.md": [
            "智能输入方式",
            "自动检测 Electron 应用",
        ],
    }

    all_passed = True

    for filepath, keywords in checks.items():
        print(f"\n检查: {filepath}")
        results = check_file(filepath, keywords)
        for result in results:
            print(result)
            if "❌" in result:
                all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("✅ 所有文档验证通过")
        return 0
    else:
        print("❌ 部分文档验证失败")
        return 1

if __name__ == "__main__":
    sys.exit(main())
