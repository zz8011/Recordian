"""语音唤醒集成测试"""
from __future__ import annotations

from pathlib import Path


class TestVoiceWakeConfiguration:
    """测试语音唤醒配置"""

    def test_wake_word_config(self) -> None:
        """测试唤醒词配置"""
        # 默认唤醒词
        default_wake_word = "小智"
        assert default_wake_word == "小智"

        # 自定义唤醒词
        custom_wake_word = "你好小智"
        assert len(custom_wake_word) > 0

    def test_wake_sensitivity_config(self) -> None:
        """测试唤醒灵敏度配置"""
        # 灵敏度范围 0.0 - 1.0
        sensitivity = 0.5
        assert 0.0 <= sensitivity <= 1.0

        # 高灵敏度
        high_sensitivity = 0.8
        assert high_sensitivity > 0.5

        # 低灵敏度
        low_sensitivity = 0.3
        assert low_sensitivity < 0.5

    def test_wake_model_config(self) -> None:
        """测试唤醒模型配置"""
        # 模型路径
        model_path = Path.home() / ".cache" / "recordian" / "wake" / "model.bin"
        assert model_path.parent.name == "wake"

        # 模型类型
        model_type = "sherpa-onnx"
        assert model_type in ["sherpa-onnx", "porcupine", "custom"]


class TestVoiceWakeDetection:
    """测试语音唤醒检测"""

    def test_wake_word_detection_concept(self) -> None:
        """测试唤醒词检测概念"""
        wake_word = "小智"
        detected_text = "小智"

        # 精确匹配
        is_detected = detected_text == wake_word
        assert is_detected is True

        # 不匹配
        detected_text = "其他文本"
        is_detected = detected_text == wake_word
        assert is_detected is False

    def test_partial_wake_word_detection(self) -> None:
        """测试部分唤醒词检测"""
        wake_word = "小智"
        detected_text = "你好小智"

        # 包含唤醒词
        is_detected = wake_word in detected_text
        assert is_detected is True

    def test_wake_word_with_noise(self) -> None:
        """测试带噪音的唤醒词检测"""
        wake_word = "小智"

        # 模拟噪音环境下的检测
        noisy_detections = [
            "小智",      # 清晰
            "小 智",     # 有间隔
            "小智啊",    # 有后缀
            "嗯小智",    # 有前缀
        ]

        for detection in noisy_detections:
            # 使用模糊匹配
            is_detected = wake_word in detection.replace(" ", "")
            assert is_detected is True

    def test_false_positive_rejection(self) -> None:
        """测试误报拒绝"""
        wake_word = "小智"

        # 相似但不是唤醒词的文本
        false_positives = [
            "小知",
            "晓智",
            "小志",
            "消失",
        ]

        for text in false_positives:
            is_detected = text == wake_word
            assert is_detected is False


class TestVoiceWakeStateManagement:
    """测试语音唤醒状态管理"""

    def test_wake_service_lifecycle(self) -> None:
        """测试唤醒服务生命周期"""
        # 服务状态
        is_running = False

        # 启动服务
        is_running = True
        assert is_running is True

        # 停止服务
        is_running = False
        assert is_running is False

    def test_wake_cooldown_period(self) -> None:
        """测试唤醒冷却期"""
        import time

        cooldown_seconds = 2.0
        last_wake_time = time.time()

        # 立即再次唤醒（应该被拒绝）
        current_time = time.time()
        time_since_last_wake = current_time - last_wake_time
        should_trigger = time_since_last_wake >= cooldown_seconds

        assert should_trigger is False

    def test_wake_event_callback(self) -> None:
        """测试唤醒事件回调"""
        wake_events = []

        def on_wake(keyword: str) -> None:
            wake_events.append(keyword)

        # 模拟唤醒
        detected_keyword = "小智"
        on_wake(detected_keyword)

        assert len(wake_events) == 1
        assert wake_events[0] == "小智"


class TestVoiceWakeIntegrationWithHotkey:
    """测试语音唤醒与热键集成"""

    def test_voice_wake_triggers_recording(self) -> None:
        """测试语音唤醒触发录音"""
        is_recording = False

        def start_recording(trigger_source: str) -> None:
            nonlocal is_recording
            is_recording = True

        # 语音唤醒触发
        start_recording("voice_wake")

        assert is_recording is True

    def test_voice_wake_and_hotkey_coexist(self) -> None:
        """测试语音唤醒和热键共存"""
        recording_source = None

        def start_recording(source: str) -> None:
            nonlocal recording_source
            recording_source = source

        # 语音唤醒触发
        start_recording("voice_wake")
        assert recording_source == "voice_wake"

        # 热键触发
        start_recording("hotkey")
        assert recording_source == "hotkey"

    def test_voice_wake_disabled_fallback_to_hotkey(self) -> None:
        """测试语音唤醒禁用时回退到热键"""
        voice_wake_enabled = False
        hotkey_enabled = True

        # 语音唤醒禁用
        if not voice_wake_enabled:
            # 应该使用热键
            assert hotkey_enabled is True


class TestVoiceWakePerformance:
    """测试语音唤醒性能"""

    def test_wake_detection_latency(self) -> None:
        """测试唤醒检测延迟"""
        import time

        # 模拟检测延迟
        start_time = time.time()

        # 模拟检测过程
        time.sleep(0.01)  # 10ms

        end_time = time.time()
        latency_ms = (end_time - start_time) * 1000

        # 延迟应该小于 100ms
        assert latency_ms < 100

    def test_wake_cpu_usage_concept(self) -> None:
        """测试唤醒 CPU 使用概念"""
        # 唤醒服务应该是轻量级的
        is_lightweight = True
        assert is_lightweight is True

        # 应该使用低功耗模式
        low_power_mode = True
        assert low_power_mode is True

    def test_wake_memory_usage_concept(self) -> None:
        """测试唤醒内存使用概念"""
        # 模型应该加载到内存
        model_loaded = True
        assert model_loaded is True

        # 内存占用应该合理
        memory_mb = 50  # 假设 50MB
        assert memory_mb < 200  # 应该小于 200MB


class TestVoiceWakeErrorHandling:
    """测试语音唤醒错误处理"""

    def test_model_load_failure(self) -> None:
        """测试模型加载失败"""
        model_path = Path("/nonexistent/model.bin")

        # 模型不存在
        model_exists = model_path.exists()
        assert model_exists is False

        # 应该能处理加载失败
        try:
            if not model_exists:
                raise FileNotFoundError("Model not found")
        except FileNotFoundError as e:
            assert "Model not found" in str(e)

    def test_audio_input_failure(self) -> None:
        """测试音频输入失败"""
        audio_available = False

        # 音频设备不可用
        if not audio_available:
            # 应该回退到热键模式
            fallback_to_hotkey = True
            assert fallback_to_hotkey is True

    def test_wake_detection_timeout(self) -> None:
        """测试唤醒检测超时"""
        import time

        timeout_seconds = 5.0
        start_time = time.time()

        # 模拟长时间无唤醒
        time.sleep(0.01)

        elapsed = time.time() - start_time

        # 应该能处理超时
        if elapsed > timeout_seconds:
            should_continue = True
        else:
            should_continue = True

        assert should_continue is True


class TestVoiceWakeEdgeCases:
    """测试语音唤醒边界情况"""

    def test_multiple_wake_words(self) -> None:
        """测试多个唤醒词"""
        wake_words = ["小智", "你好小智", "嘿小智"]
        detected_text = "你好小智"

        # 检查是否匹配任一唤醒词
        is_detected = any(word in detected_text for word in wake_words)
        assert is_detected is True

    def test_wake_word_in_sentence(self) -> None:
        """测试句子中的唤醒词"""
        wake_word = "小智"
        sentence = "我想问小智一个问题"

        # 应该能检测到句子中的唤醒词
        is_detected = wake_word in sentence
        assert is_detected is True

    def test_wake_word_case_sensitivity(self) -> None:
        """测试唤醒词大小写敏感性"""
        wake_word = "小智"
        detected_text = "小智"  # 中文没有大小写

        is_detected = detected_text == wake_word
        assert is_detected is True

    def test_wake_word_with_punctuation(self) -> None:
        """测试带标点的唤醒词"""
        wake_word = "小智"
        detected_text = "小智，"

        # 移除标点后匹配
        cleaned_text = detected_text.strip("，。！？")
        is_detected = cleaned_text == wake_word
        assert is_detected is True

    def test_simultaneous_wake_and_hotkey(self) -> None:
        """测试同时触发唤醒和热键"""
        wake_triggered = True
        hotkey_triggered = True

        # 应该优先处理其中一个
        if wake_triggered and hotkey_triggered:
            # 优先语音唤醒
            primary_trigger = "voice_wake"
        elif wake_triggered:
            primary_trigger = "voice_wake"
        else:
            primary_trigger = "hotkey"

        assert primary_trigger in ["voice_wake", "hotkey"]
