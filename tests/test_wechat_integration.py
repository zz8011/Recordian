#!/usr/bin/env python3
"""
微信集成测试脚本

测试 Electron 应用检测和输入功能。
使用方法：
1. 打开微信聊天窗口
2. 运行此脚本
3. 脚本会在 5 秒后自动输入测试文本到微信
"""

import subprocess
import sys
import time

sys.path.insert(0, 'src')

from recordian.linux_commit import _is_electron_window, _is_terminal_window, resolve_committer


def get_wechat_window_id():
    """获取微信窗口 ID"""
    result = subprocess.run(
        ["xdotool", "search", "--class", "WeChatAppEx"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return int(result.stdout.strip().split('\n')[0])
    return None


def main():
    print("=" * 60)
    print("微信集成测试")
    print("=" * 60)

    # 1. 检测微信窗口
    wechat_wid = get_wechat_window_id()
    if not wechat_wid:
        print("❌ 未找到微信窗口，请先启动微信")
        return 1

    print(f"✅ 找到微信窗口 ID: {wechat_wid}")

    # 2. 测试 Electron 检测
    is_electron = _is_electron_window(wechat_wid)
    print(f"✅ Electron 检测: {is_electron}")
    if not is_electron:
        print("⚠️  警告：微信未被识别为 Electron 应用")

    is_terminal = _is_terminal_window(wechat_wid)
    print(f"✅ 终端检测: {is_terminal}")

    # 3. 测试 auto 模式路由
    print("\n测试 auto 模式:")
    committer = resolve_committer("auto", target_window_id=wechat_wid)
    print(f"✅ Backend: {committer.backend_name}")
    print(f"✅ Target window ID: {committer.target_window_id}")

    # 4. 测试 auto-fallback 模式
    print("\n测试 auto-fallback 模式:")
    committer_fallback = resolve_committer("auto-fallback", target_window_id=wechat_wid)
    print(f"✅ Backend: {committer_fallback.backend_name}")
    if hasattr(committer_fallback, 'committers'):
        print(f"✅ 降级链长度: {len(committer_fallback.committers)}")

    # 5. 实际输入测试
    print("\n" + "=" * 60)
    print("准备进行实际输入测试")
    print("请在 5 秒内切换到微信聊天窗口...")
    print("=" * 60)

    for i in range(5, 0, -1):
        print(f"{i}...", flush=True)
        time.sleep(1)

    print("\n开始输入测试文本...")

    test_text = "✅ Recordian 微信集成测试成功！"

    try:
        result = committer.commit(test_text)
        print("\n✅ 输入成功！")
        print(f"   Backend: {result.backend}")
        print(f"   Committed: {result.committed}")
        print(f"   Detail: {result.detail}")

        print("\n" + "=" * 60)
        print("测试完成！请检查微信聊天窗口是否收到测试文本。")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n❌ 输入失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
