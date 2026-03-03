"""测试安全修复：deque maxlen、文件权限、线程安全"""
import os
import tempfile
from collections import deque
from pathlib import Path


def test_voice_wake_owner_audio_chunks_has_maxlen():
    """测试 voice_wake.py 中的 owner_audio_chunks 有 maxlen 限制"""
    import inspect

    from recordian import voice_wake

    source = inspect.getsource(voice_wake.VoiceWakeService._run)
    # 检查 deque(maxlen=100) 存在
    assert "deque(maxlen=100)" in source, "voice_wake.py 中的 owner_audio_chunks 应该有 maxlen=100"


def test_hotkey_dictate_owner_audio_chunks_has_maxlen():
    """测试 hotkey_dictate.py 中的 owner_audio_chunks 有 maxlen 限制"""
    import inspect

    from recordian import hotkey_dictate

    source = inspect.getsource(hotkey_dictate.build_ptt_hotkey_handlers)
    # 检查 deque(maxlen=100) 存在
    assert "deque(maxlen=100)" in source, "hotkey_dictate.py 中的 owner_audio_chunks 应该有 maxlen=100"


def test_deque_maxlen_prevents_memory_leak():
    """验证 deque maxlen 机制能防止内存泄漏"""
    # 模拟音频块累积
    audio_chunks = deque(maxlen=100)

    # 添加超过 maxlen 的元素
    for i in range(200):
        audio_chunks.append(f"chunk_{i}")

    # 验证只保留最后 100 个
    assert len(audio_chunks) == 100
    assert audio_chunks[0] == "chunk_100"
    assert audio_chunks[-1] == "chunk_199"


def test_speaker_profile_file_permissions():
    """测试声纹文件保存时设置正确的权限 0o600"""
    import numpy as np

    from recordian.speaker_verify import SpeakerProfile, save_speaker_profile

    with tempfile.TemporaryDirectory() as tmpdir:
        profile_path = Path(tmpdir) / "test_profile.json"

        # 创建测试声纹
        profile = SpeakerProfile(
            embedding=np.random.rand(49).tolist(),
            sample_rate=16000,
            created_at=1234567890.0,
            source="test",
            feature_version=1,
        )

        # 保存声纹
        save_speaker_profile(profile_path, profile)

        # 验证文件存在
        assert profile_path.exists()

        # 验证文件权限为 0o600 (仅所有者可读写)
        stat_info = os.stat(profile_path)
        permissions = stat_info.st_mode & 0o777
        assert permissions == 0o600, f"声纹文件权限应为 0o600，实际为 {oct(permissions)}"


def test_speaker_profile_chmod_in_source():
    """验证 save_speaker_profile 源码中包含 chmod(0o600)"""
    import inspect

    from recordian import speaker_verify

    source = inspect.getsource(speaker_verify.save_speaker_profile)
    assert "chmod(0o600)" in source or "chmod(384)" in source, "save_speaker_profile 应该调用 chmod(0o600)"


def test_state_lock_exists():
    """验证 hotkey_dictate 中存在状态锁"""
    import inspect

    from recordian import hotkey_dictate

    source = inspect.getsource(hotkey_dictate.build_ptt_hotkey_handlers)
    # 检查 state_lock 的创建
    assert "state_lock" in source or "threading.Lock()" in source


def test_get_set_state_use_lock():
    """验证 _get_state 和 _set_state 使用锁保护"""
    import inspect

    from recordian import hotkey_dictate

    source = inspect.getsource(hotkey_dictate.build_ptt_hotkey_handlers)

    # 检查 _get_state 和 _set_state 函数定义存在
    assert "def _get_state" in source
    assert "def _set_state" in source

    # 检查使用了 with lock 模式
    assert "with state_lock:" in source or "with lock:" in source


def test_wake_owner_silence_extend_s_parameter_defined():
    """验证 wake_owner_silence_extend_s 参数已在 argparse 中定义"""
    import inspect

    from recordian import hotkey_dictate

    source = inspect.getsource(hotkey_dictate.build_parser)
    assert "wake-owner-silence-extend-s" in source, "wake_owner_silence_extend_s 参数应该在 argparse 中定义"


def test_wake_owner_silence_extend_s_default_value():
    """测试 wake_owner_silence_extend_s 参数的默认值"""
    from recordian.hotkey_dictate import build_parser

    parser = build_parser()
    args = parser.parse_args([])

    # 验证参数存在且默认值为 0.5
    assert hasattr(args, "wake_owner_silence_extend_s")
    assert args.wake_owner_silence_extend_s == 0.5
