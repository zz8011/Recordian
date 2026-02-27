"""测试性能基准测试框架"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from recordian.performance_benchmark import (
    ASRBenchmark,
    EndToEndBenchmark,
    PerformanceBenchmark,
    PerformanceMetrics,
    RefinerBenchmark,
)


class TestPerformanceMetrics:
    """测试性能指标"""

    def test_metrics_creation(self) -> None:
        """测试创建性能指标"""
        metrics = PerformanceMetrics(
            duration_ms=100.5,
            memory_mb=50.2,
            cpu_percent=25.0,
        )
        assert metrics.duration_ms == 100.5
        assert metrics.memory_mb == 50.2
        assert metrics.cpu_percent == 25.0

    def test_metrics_str(self) -> None:
        """测试性能指标字符串表示"""
        metrics = PerformanceMetrics(
            duration_ms=100.5,
            memory_mb=50.2,
            cpu_percent=25.0,
        )
        result = str(metrics)
        assert "100.50ms" in result
        assert "50.20MB" in result
        assert "25.0%" in result


class TestPerformanceBenchmark:
    """测试性能基准测试工具"""

    def test_measure_single_iteration(self) -> None:
        """测试单次迭代测量"""
        benchmark = PerformanceBenchmark()

        def test_func() -> None:
            time.sleep(0.01)  # 10ms

        metrics = benchmark.measure("test", test_func, iterations=1)

        assert metrics.duration_ms >= 10.0
        assert "test" in benchmark.results

    def test_measure_multiple_iterations(self) -> None:
        """测试多次迭代测量"""
        benchmark = PerformanceBenchmark()

        def test_func() -> None:
            time.sleep(0.01)

        metrics = benchmark.measure("test", test_func, iterations=3)

        assert metrics.duration_ms >= 10.0
        assert len(benchmark.results["test"]) == 1

    def test_get_results(self) -> None:
        """测试获取结果"""
        benchmark = PerformanceBenchmark()

        def test_func() -> None:
            pass

        benchmark.measure("test1", test_func)
        benchmark.measure("test2", test_func)

        results1 = benchmark.get_results("test1")
        results2 = benchmark.get_results("test2")
        results3 = benchmark.get_results("nonexistent")

        assert len(results1) == 1
        assert len(results2) == 1
        assert len(results3) == 0

    def test_multiple_measurements_same_name(self) -> None:
        """测试同名多次测量"""
        benchmark = PerformanceBenchmark()

        def test_func() -> None:
            pass

        benchmark.measure("test", test_func)
        benchmark.measure("test", test_func)

        results = benchmark.get_results("test")
        assert len(results) == 2

    def test_print_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试打印摘要"""
        benchmark = PerformanceBenchmark()

        def test_func() -> None:
            time.sleep(0.001)

        benchmark.measure("test", test_func)
        benchmark.print_summary()

        captured = capsys.readouterr()
        assert "Performance Benchmark Summary" in captured.out
        assert "test:" in captured.out


class TestASRBenchmark:
    """测试 ASR 性能基准"""

    def test_short_audio_benchmark(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试短音频基准"""
        benchmark = PerformanceBenchmark()
        asr_bench = ASRBenchmark(benchmark)

        mock_provider = Mock()
        mock_provider.transcribe_file.return_value = Mock(text="test")

        audio_path = Path("/tmp/test.wav")
        asr_bench.test_transcribe_short_audio(mock_provider, audio_path)

        captured = capsys.readouterr()
        assert "Short audio transcription" in captured.out
        assert mock_provider.transcribe_file.call_count == 3

    def test_medium_audio_benchmark(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试中等音频基准"""
        benchmark = PerformanceBenchmark()
        asr_bench = ASRBenchmark(benchmark)

        mock_provider = Mock()
        mock_provider.transcribe_file.return_value = Mock(text="test")

        audio_path = Path("/tmp/test.wav")
        asr_bench.test_transcribe_medium_audio(mock_provider, audio_path)

        captured = capsys.readouterr()
        assert "Medium audio transcription" in captured.out

    def test_long_audio_benchmark(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试长音频基准"""
        benchmark = PerformanceBenchmark()
        asr_bench = ASRBenchmark(benchmark)

        mock_provider = Mock()
        mock_provider.transcribe_file.return_value = Mock(text="test")

        audio_path = Path("/tmp/test.wav")
        asr_bench.test_transcribe_long_audio(mock_provider, audio_path)

        captured = capsys.readouterr()
        assert "Long audio transcription" in captured.out


class TestRefinerBenchmark:
    """测试精炼器性能基准"""

    def test_short_text_benchmark(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试短文本基准"""
        benchmark = PerformanceBenchmark()
        refiner_bench = RefinerBenchmark(benchmark)

        mock_refiner = Mock()
        mock_refiner.refine.return_value = "refined text"

        refiner_bench.test_refine_short_text(mock_refiner, "test")

        captured = capsys.readouterr()
        assert "Short text refinement" in captured.out
        assert mock_refiner.refine.call_count == 5

    def test_medium_text_benchmark(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试中等文本基准"""
        benchmark = PerformanceBenchmark()
        refiner_bench = RefinerBenchmark(benchmark)

        mock_refiner = Mock()
        mock_refiner.refine.return_value = "refined text"

        refiner_bench.test_refine_medium_text(mock_refiner, "test" * 20)

        captured = capsys.readouterr()
        assert "Medium text refinement" in captured.out

    def test_long_text_benchmark(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试长文本基准"""
        benchmark = PerformanceBenchmark()
        refiner_bench = RefinerBenchmark(benchmark)

        mock_refiner = Mock()
        mock_refiner.refine.return_value = "refined text"

        refiner_bench.test_refine_long_text(mock_refiner, "test" * 100)

        captured = capsys.readouterr()
        assert "Long text refinement" in captured.out


class TestEndToEndBenchmark:
    """测试端到端性能基准"""

    def test_full_pipeline(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试完整流程基准"""
        benchmark = PerformanceBenchmark()
        e2e_bench = EndToEndBenchmark(benchmark)

        audio_path = Path("/tmp/test.wav")

        def mock_record() -> Path:
            time.sleep(0.001)
            return audio_path

        def mock_transcribe(path: Path) -> str:
            time.sleep(0.001)
            return "transcribed text"

        def mock_refine(text: str) -> str:
            time.sleep(0.001)
            return "refined text"

        e2e_bench.test_full_pipeline(mock_record, mock_transcribe, mock_refine)

        captured = capsys.readouterr()
        assert "End-to-End Pipeline" in captured.out
        assert "Recording:" in captured.out
        assert "ASR:" in captured.out
        assert "Refinement:" in captured.out
        assert "Total:" in captured.out
        assert "Bottleneck:" in captured.out

    def test_identifies_bottleneck(self, capsys: pytest.CaptureFixture[str]) -> None:
        """测试识别瓶颈"""
        benchmark = PerformanceBenchmark()
        e2e_bench = EndToEndBenchmark(benchmark)

        audio_path = Path("/tmp/test.wav")

        def mock_record() -> Path:
            time.sleep(0.001)
            return audio_path

        def mock_transcribe(path: Path) -> str:
            time.sleep(0.01)  # 最慢的阶段
            return "transcribed text"

        def mock_refine(text: str) -> str:
            time.sleep(0.001)
            return "refined text"

        e2e_bench.test_full_pipeline(mock_record, mock_transcribe, mock_refine)

        captured = capsys.readouterr()
        # ASR 应该是瓶颈
        assert "Bottleneck: ASR" in captured.out
