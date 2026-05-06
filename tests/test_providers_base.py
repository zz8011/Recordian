"""测试 providers/base.py 基础类和工具函数"""
from __future__ import annotations

from pathlib import Path

from recordian.models import ASRResult, ASRSegment, coerce_asr_segments, coerce_asr_timestamps
from recordian.providers.base import (
    ASRProvider,
    ASRProviderCapabilities,
    _estimate_english_ratio,
    provider_supports_file_streaming,
    provider_supports_realtime,
)


class MockASRProvider(ASRProvider):
    """用于测试的 Mock ASR Provider"""

    @property
    def provider_name(self) -> str:
        return "mock"

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        return ASRResult(text="test", confidence=0.95)


class TestASRProvider:
    """测试 ASRProvider 抽象基类"""

    def test_provider_name_abstract(self) -> None:
        """测试 provider_name 是抽象属性"""
        provider = MockASRProvider()
        assert provider.provider_name == "mock"

    def test_is_cloud_default_false(self) -> None:
        """测试 is_cloud 默认为 False"""
        provider = MockASRProvider()
        assert provider.is_cloud is False

    def test_transcribe_file_abstract(self) -> None:
        """测试 transcribe_file 是抽象方法"""
        provider = MockASRProvider()
        result = provider.transcribe_file(Path("/tmp/test.wav"), hotwords=[])
        assert isinstance(result, ASRResult)
        assert result.text == "test"

    def test_provider_support_helpers_use_capabilities(self) -> None:
        class _CapabilityProvider(MockASRProvider):
            @property
            def capabilities(self) -> ASRProviderCapabilities:
                return ASRProviderCapabilities(supports_file_streaming=True, supports_realtime=True)

        provider = _CapabilityProvider()
        assert provider_supports_file_streaming(provider) is True
        assert provider_supports_realtime(provider) is True

    def test_provider_support_helpers_fall_back_to_legacy_methods(self) -> None:
        class _LegacyProvider:
            def supports_realtime_transcription(self) -> bool:
                return True

            def transcribe_file_stream(self, wav_path: Path, hotwords: list[str]):  # noqa: ANN001
                yield "chunk"

        provider = _LegacyProvider()
        assert provider_supports_file_streaming(provider) is True
        assert provider_supports_realtime(provider) is True

    def test_asr_result_defaults_keep_text_as_primary_output(self) -> None:
        result = ASRResult(text="测试文本")

        assert result.raw_text == "测试文本"
        assert result.normalized_text == "测试文本"
        assert result.detected_language is None
        assert result.timestamps == []
        assert result.segments == []

    def test_asr_result_derives_timestamps_from_segments(self) -> None:
        result = ASRResult(
            text="hello",
            segments=[{"text": "hello", "start": 0, "end": 520, "speaker": "spk-1"}],
        )

        assert result.timestamps == [(0, 520)]
        assert result.segments == [
            ASRSegment(text="hello", start_ms=0, end_ms=520, speaker="spk-1", metadata={}),
        ]

    def test_coerce_asr_timestamps_accepts_dicts_and_pairs(self) -> None:
        timestamps = coerce_asr_timestamps(
            [{"start": 0, "end_ms": 800}, (900, 1200), ["1300", "1500"], "bad"]
        )

        assert timestamps == [(0, 800), (900, 1200), (1300, 1500)]

    def test_coerce_asr_segments_ignores_invalid_items(self) -> None:
        segments = coerce_asr_segments(
            [{"text": "one", "start_ms": 0, "end_ms": 500}, "bad", {"speaker": "spk-2"}]
        )

        assert segments == [
            ASRSegment(text="one", start_ms=0, end_ms=500, speaker=None, metadata={}),
            ASRSegment(text="", start_ms=None, end_ms=None, speaker="spk-2", metadata={}),
        ]


class TestEstimateEnglishRatio:
    """测试 _estimate_english_ratio 函数"""

    def test_empty_string(self) -> None:
        """测试空字符串返回 0.0"""
        assert _estimate_english_ratio("") == 0.0

    def test_pure_english(self) -> None:
        """测试纯英文返回 1.0"""
        assert _estimate_english_ratio("hello world") == 1.0
        assert _estimate_english_ratio("HELLO WORLD") == 1.0
        assert _estimate_english_ratio("Hello World") == 1.0

    def test_pure_chinese(self) -> None:
        """测试纯中文返回 0.0"""
        assert _estimate_english_ratio("你好世界") == 0.0
        assert _estimate_english_ratio("测试文本") == 0.0

    def test_mixed_text(self) -> None:
        """测试中英混合文本"""
        # "hello你好" = 5个拉丁字母 / 7个字母(5英文+2中文) ≈ 0.714
        result = _estimate_english_ratio("hello你好")
        assert 0.71 < result < 0.72

        # "你好hello世界" = 5个拉丁字母 / 9个字母 ≈ 0.556
        result = _estimate_english_ratio("你好hello世界")
        assert 0.55 < result < 0.56

    def test_no_alpha_characters(self) -> None:
        """测试没有字母字符返回 0.0"""
        assert _estimate_english_ratio("123456") == 0.0
        assert _estimate_english_ratio("!@#$%^") == 0.0
        assert _estimate_english_ratio("   ") == 0.0

    def test_mixed_with_numbers(self) -> None:
        """测试包含数字的文本"""
        # "hello123" = 5个英文字母 / 5个字母 = 1.0
        result = _estimate_english_ratio("hello123")
        assert result == 1.0

        # "你好123" = 0个英文字母 / 0个字母 = 0.0
        result = _estimate_english_ratio("你好123")
        assert result == 0.0

    def test_case_insensitive(self) -> None:
        """测试大小写不敏感"""
        assert _estimate_english_ratio("ABC") == 1.0
        assert _estimate_english_ratio("abc") == 1.0
        assert _estimate_english_ratio("AbC") == 1.0

    def test_special_characters_ignored(self) -> None:
        """测试特殊字符被忽略"""
        # "hello, world!" = 10个英文字母 / 10个字母 = 1.0
        result = _estimate_english_ratio("hello, world!")
        assert result == 1.0

        # "你好，世界！" = 0个英文字母 / 0个字母 = 0.0
        result = _estimate_english_ratio("你好，世界！")
        assert result == 0.0

    def test_partial_english(self) -> None:
        """测试部分英文的情况"""
        # "a你" = 1个拉丁字母 / 2个字母 = 0.5
        result = _estimate_english_ratio("a你")
        assert result == 0.5

        # "abc你好" = 3个拉丁字母 / 5个字母 = 0.6
        result = _estimate_english_ratio("abc你好")
        assert result == 0.6
