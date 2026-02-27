"""性能基准测试套件

用于测试和监控 Recordian 的性能指标。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psutil


@dataclass
class PerformanceMetrics:
    """性能指标"""

    duration_ms: float
    memory_mb: float
    cpu_percent: float

    def __str__(self) -> str:
        return (
            f"Duration: {self.duration_ms:.2f}ms, "
            f"Memory: {self.memory_mb:.2f}MB, "
            f"CPU: {self.cpu_percent:.1f}%"
        )


class PerformanceBenchmark:
    """性能基准测试工具"""

    def __init__(self) -> None:
        self.process = psutil.Process()
        self.results: dict[str, list[PerformanceMetrics]] = {}

    def measure(
        self,
        name: str,
        func: Callable[[], Any],
        iterations: int = 1,
    ) -> PerformanceMetrics:
        """测量函数性能

        Args:
            name: 测试名称
            func: 要测试的函数
            iterations: 迭代次数

        Returns:
            平均性能指标
        """
        metrics_list = []

        for _ in range(iterations):
            # 记录初始状态
            mem_before = self.process.memory_info().rss / 1024 / 1024
            cpu_before = self.process.cpu_percent()

            # 执行函数
            start_time = time.perf_counter()
            func()
            end_time = time.perf_counter()

            # 记录结束状态
            mem_after = self.process.memory_info().rss / 1024 / 1024
            cpu_after = self.process.cpu_percent()

            # 计算指标
            duration_ms = (end_time - start_time) * 1000
            memory_mb = mem_after - mem_before
            cpu_percent = cpu_after - cpu_before

            metrics = PerformanceMetrics(
                duration_ms=duration_ms,
                memory_mb=memory_mb,
                cpu_percent=cpu_percent,
            )
            metrics_list.append(metrics)

        # 计算平均值
        avg_metrics = PerformanceMetrics(
            duration_ms=sum(m.duration_ms for m in metrics_list) / iterations,
            memory_mb=sum(m.memory_mb for m in metrics_list) / iterations,
            cpu_percent=sum(m.cpu_percent for m in metrics_list) / iterations,
        )

        # 保存结果
        if name not in self.results:
            self.results[name] = []
        self.results[name].append(avg_metrics)

        return avg_metrics

    def get_results(self, name: str) -> list[PerformanceMetrics]:
        """获取测试结果"""
        return self.results.get(name, [])

    def print_summary(self) -> None:
        """打印性能摘要"""
        print("\n" + "=" * 60)
        print("Performance Benchmark Summary")
        print("=" * 60)

        for name, metrics_list in self.results.items():
            print(f"\n{name}:")
            for i, metrics in enumerate(metrics_list, 1):
                print(f"  Run {i}: {metrics}")

            if len(metrics_list) > 1:
                avg_duration = sum(m.duration_ms for m in metrics_list) / len(metrics_list)
                avg_memory = sum(m.memory_mb for m in metrics_list) / len(metrics_list)
                avg_cpu = sum(m.cpu_percent for m in metrics_list) / len(metrics_list)
                print(f"  Average: Duration: {avg_duration:.2f}ms, "
                      f"Memory: {avg_memory:.2f}MB, CPU: {avg_cpu:.1f}%")

        print("=" * 60)


class ASRBenchmark:
    """ASR 性能基准测试"""

    def __init__(self, benchmark: PerformanceBenchmark) -> None:
        self.benchmark = benchmark

    def test_transcribe_short_audio(self, provider: Any, audio_path: Path) -> None:
        """测试短音频转录（<10秒）"""
        def transcribe() -> None:
            provider.transcribe_file(audio_path, hotwords=[])

        metrics = self.benchmark.measure(
            "ASR: Short Audio (<10s)",
            transcribe,
            iterations=3,
        )
        print(f"Short audio transcription: {metrics}")

    def test_transcribe_medium_audio(self, provider: Any, audio_path: Path) -> None:
        """测试中等音频转录（10-30秒）"""
        def transcribe() -> None:
            provider.transcribe_file(audio_path, hotwords=[])

        metrics = self.benchmark.measure(
            "ASR: Medium Audio (10-30s)",
            transcribe,
            iterations=3,
        )
        print(f"Medium audio transcription: {metrics}")

    def test_transcribe_long_audio(self, provider: Any, audio_path: Path) -> None:
        """测试长音频转录（>30秒）"""
        def transcribe() -> None:
            provider.transcribe_file(audio_path, hotwords=[])

        metrics = self.benchmark.measure(
            "ASR: Long Audio (>30s)",
            transcribe,
            iterations=3,
        )
        print(f"Long audio transcription: {metrics}")


class RefinerBenchmark:
    """精炼器性能基准测试"""

    def __init__(self, benchmark: PerformanceBenchmark) -> None:
        self.benchmark = benchmark

    def test_refine_short_text(self, refiner: Any, text: str) -> None:
        """测试短文本精炼（<50字）"""
        def refine() -> None:
            refiner.refine(text)

        metrics = self.benchmark.measure(
            "Refiner: Short Text (<50 chars)",
            refine,
            iterations=5,
        )
        print(f"Short text refinement: {metrics}")

    def test_refine_medium_text(self, refiner: Any, text: str) -> None:
        """测试中等文本精炼（50-200字）"""
        def refine() -> None:
            refiner.refine(text)

        metrics = self.benchmark.measure(
            "Refiner: Medium Text (50-200 chars)",
            refine,
            iterations=5,
        )
        print(f"Medium text refinement: {metrics}")

    def test_refine_long_text(self, refiner: Any, text: str) -> None:
        """测试长文本精炼（>200字）"""
        def refine() -> None:
            refiner.refine(text)

        metrics = self.benchmark.measure(
            "Refiner: Long Text (>200 chars)",
            refine,
            iterations=5,
        )
        print(f"Long text refinement: {metrics}")


class EndToEndBenchmark:
    """端到端性能基准测试"""

    def __init__(self, benchmark: PerformanceBenchmark) -> None:
        self.benchmark = benchmark

    def test_full_pipeline(
        self,
        record_func: Callable[[], Path],
        transcribe_func: Callable[[Path], str],
        refine_func: Callable[[str], str],
    ) -> None:
        """测试完整流程：录音 -> ASR -> 精炼"""

        # 测试录音
        def record() -> Path:
            return record_func()

        metrics_record = self.benchmark.measure(
            "E2E: Recording",
            record,
            iterations=3,
        )

        # 测试 ASR
        audio_path = record_func()

        def transcribe() -> str:
            return transcribe_func(audio_path)

        metrics_asr = self.benchmark.measure(
            "E2E: ASR",
            transcribe,
            iterations=3,
        )

        # 测试精炼
        text = transcribe_func(audio_path)

        def refine() -> str:
            return refine_func(text)

        metrics_refine = self.benchmark.measure(
            "E2E: Refinement",
            refine,
            iterations=3,
        )

        # 计算总延迟
        total_duration = (
            metrics_record.duration_ms +
            metrics_asr.duration_ms +
            metrics_refine.duration_ms
        )

        print(f"\nEnd-to-End Pipeline:")
        print(f"  Recording: {metrics_record.duration_ms:.2f}ms")
        print(f"  ASR: {metrics_asr.duration_ms:.2f}ms")
        print(f"  Refinement: {metrics_refine.duration_ms:.2f}ms")
        print(f"  Total: {total_duration:.2f}ms")

        # 识别瓶颈
        stages = [
            ("Recording", metrics_record.duration_ms),
            ("ASR", metrics_asr.duration_ms),
            ("Refinement", metrics_refine.duration_ms),
        ]
        bottleneck = max(stages, key=lambda x: x[1])
        print(f"  Bottleneck: {bottleneck[0]} ({bottleneck[1]:.2f}ms)")


def run_benchmarks() -> None:
    """运行所有基准测试"""
    print("Starting performance benchmarks...")

    benchmark = PerformanceBenchmark()

    # 这里可以添加实际的测试
    # asr_bench = ASRBenchmark(benchmark)
    # refiner_bench = RefinerBenchmark(benchmark)
    # e2e_bench = EndToEndBenchmark(benchmark)

    benchmark.print_summary()


if __name__ == "__main__":
    run_benchmarks()
