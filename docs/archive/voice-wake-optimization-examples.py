#!/usr/bin/env python3
"""
Voice Wake CPU Optimization - Code Examples
============================================

This file contains concrete code examples for optimizing the voice wake
CPU usage from 400-800% down to <100%.

Author: Research by Claude Sonnet 4.6
Date: 2026-03-03
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, TypeVar
import numpy as np

# ============================================================================
# 1. LOOP LIMITING PATTERNS
# ============================================================================

def example_infinite_loop_fix():
    """Example: Fix the infinite loop in voice_wake.py"""

    # ❌ BEFORE: Potential infinite loop
    def process_audio_unsafe(spotter, stream, samples):
        stream.accept_waveform(16000, samples)
        while spotter.is_ready(stream):  # Can loop forever!
            spotter.decode_stream(stream)

    # ✅ AFTER: Safe with iteration limit
    def process_audio_safe_v1(spotter, stream, samples):
        MAX_DECODE_ITERATIONS = 10

        stream.accept_waveform(16000, samples)

        decode_count = 0
        while spotter.is_ready(stream) and decode_count < MAX_DECODE_ITERATIONS:
            spotter.decode_stream(stream)
            decode_count += 1

        if decode_count >= MAX_DECODE_ITERATIONS:
            print(f"WARNING: decode limit reached: {decode_count}")

    # ✅ AFTER: Safe with time limit
    def process_audio_safe_v2(spotter, stream, samples):
        MAX_DECODE_TIME_MS = 50

        stream.accept_waveform(16000, samples)

        decode_start = time.perf_counter()
        while spotter.is_ready(stream):
            elapsed_ms = (time.perf_counter() - decode_start) * 1000
            if elapsed_ms > MAX_DECODE_TIME_MS:
                print(f"WARNING: decode timeout: {elapsed_ms:.1f}ms")
                break
            spotter.decode_stream(stream)

    # ✅ BEST: Hybrid approach (iteration + time limit)
    def process_audio_safe_hybrid(spotter, stream, samples):
        MAX_DECODE_ITERATIONS = 10
        MAX_DECODE_TIME_MS = 50

        stream.accept_waveform(16000, samples)

        decode_start = time.perf_counter()
        decode_count = 0

        while spotter.is_ready(stream):
            # Check iteration limit
            if decode_count >= MAX_DECODE_ITERATIONS:
                print(f"WARNING: decode limit: iterations={decode_count}")
                break

            # Check time limit
            elapsed_ms = (time.perf_counter() - decode_start) * 1000
            if elapsed_ms > MAX_DECODE_TIME_MS:
                print(f"WARNING: decode limit: time={elapsed_ms:.1f}ms")
                break

            spotter.decode_stream(stream)
            decode_count += 1


# ============================================================================
# 2. TTL CACHE IMPLEMENTATION
# ============================================================================

class TTLCache:
    """Time-To-Live cache with automatic expiration"""

    def __init__(self, ttl_seconds: float = 300.0):
        """
        Args:
            ttl_seconds: Time-to-live in seconds (default 5 minutes)
        """
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        """Get value from cache, returns None if expired or not found"""
        with self._lock:
            if key not in self._cache:
                return None

            value, timestamp = self._cache[key]
            if time.monotonic() - timestamp > self.ttl:
                del self._cache[key]
                return None

            return value

    def set(self, key: str, value: Any) -> None:
        """Set value in cache with current timestamp"""
        with self._lock:
            self._cache[key] = (value, time.monotonic())

    def clear(self) -> None:
        """Clear all cached values"""
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        """Return number of cached items"""
        with self._lock:
            return len(self._cache)

    def cleanup_expired(self) -> int:
        """Remove expired entries, return number removed"""
        with self._lock:
            now = time.monotonic()
            expired_keys = [
                key for key, (_, timestamp) in self._cache.items()
                if now - timestamp > self.ttl
            ]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)


def example_ttl_cache_usage():
    """Example: Using TTL cache for keyword file generation"""

    # Global cache instance
    _keyword_cache = TTLCache(ttl_seconds=3600)  # 1 hour

    def ensure_keywords_file_cached(
        phrases: list[str],
        tokens_type: str,
        score: float,
        threshold: float,
        cache_dir: Path,
    ) -> Path:
        """Generate keywords file with caching"""

        # Generate cache key from parameters
        cache_key = f"{','.join(sorted(phrases))}:{tokens_type}:{score}:{threshold}"

        # Check cache first
        cached_path = _keyword_cache.get(cache_key)
        if cached_path and Path(cached_path).exists():
            print(f"Cache HIT: {cache_key[:50]}...")
            return Path(cached_path)

        print(f"Cache MISS: {cache_key[:50]}...")

        # Generate keywords file (expensive operation)
        out_path = cache_dir / "keywords.txt"
        # ... actual generation logic ...

        # Store in cache
        _keyword_cache.set(cache_key, str(out_path))
        return out_path


# ============================================================================
# 3. LRU CACHE FOR STATIC DATA
# ============================================================================

@lru_cache(maxsize=8)
def load_tone_variant_groups_cached(tokens_path_str: str) -> dict[str, list[str]]:
    """
    Load tone variant groups with LRU caching.

    This is perfect for static data that doesn't change during runtime.
    The @lru_cache decorator automatically handles caching.

    Args:
        tokens_path_str: Path to tokens file (must be hashable, so use str)

    Returns:
        Dictionary mapping normalized tokens to their tone variants
    """
    tokens_path = Path(tokens_path_str)

    groups: dict[str, list[str]] = {}
    try:
        content = tokens_path.read_text(encoding="utf-8")
    except Exception:
        return groups

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("<"):
            continue

        token = line.split()[0].strip()
        # Normalize tone (remove tone marks)
        key = normalize_tone(token)

        if key not in groups:
            groups[key] = []
        groups[key].append(token)

    return {k: v for k, v in groups.items() if len(v) > 1}


def normalize_tone(token: str) -> str:
    """Normalize pinyin tone marks (simplified example)"""
    # In real implementation, use pypinyin.contrib.tone_convert
    return token.lower()


# ============================================================================
# 4. CPU USAGE MONITORING
# ============================================================================

class CPUMonitor:
    """Monitor CPU usage of current process"""

    def __init__(self, interval: float = 1.0):
        """
        Args:
            interval: Sampling interval in seconds
        """
        self.interval = interval
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        """Start monitoring in background thread"""
        self._stop.clear()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, float]:
        """Stop monitoring and return statistics"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

        if not self.samples:
            return {"avg": 0, "max": 0, "min": 0, "p50": 0, "p95": 0}

        sorted_samples = sorted(self.samples)
        return {
            "avg": sum(self.samples) / len(self.samples),
            "max": max(self.samples),
            "min": min(self.samples),
            "p50": sorted_samples[len(sorted_samples) // 2],
            "p95": sorted_samples[int(len(sorted_samples) * 0.95)],
            "count": len(self.samples),
        }

    def _monitor(self):
        """Background monitoring loop"""
        try:
            import psutil
            process = psutil.Process()
        except ImportError:
            print("WARNING: psutil not installed, CPU monitoring disabled")
            return

        while not self._stop.is_set():
            try:
                cpu_percent = process.cpu_percent(interval=self.interval)
                self.samples.append(cpu_percent)
            except Exception as e:
                print(f"CPU monitoring error: {e}")
                break


def example_cpu_monitoring():
    """Example: Monitor CPU usage during voice wake"""

    monitor = CPUMonitor(interval=0.5)
    monitor.start()

    # Run voice wake for 60 seconds
    print("Monitoring CPU for 60 seconds...")
    time.sleep(60)

    stats = monitor.stop()
    print(f"\nCPU Usage Statistics:")
    print(f"  Average: {stats['avg']:.1f}%")
    print(f"  Maximum: {stats['max']:.1f}%")
    print(f"  P50 (median): {stats['p50']:.1f}%")
    print(f"  P95: {stats['p95']:.1f}%")
    print(f"  Samples: {stats['count']}")


# ============================================================================
# 5. LATENCY PROFILING
# ============================================================================

class LatencyProfiler:
    """Profile latency of code blocks"""

    def __init__(self):
        self.timings: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def measure(self, name: str):
        """Context manager for measuring code blocks"""
        class Timer:
            def __init__(self, profiler: LatencyProfiler, name: str):
                self.profiler = profiler
                self.name = name
                self.start = 0.0

            def __enter__(self):
                self.start = time.perf_counter()
                return self

            def __exit__(self, *args):
                elapsed_ms = (time.perf_counter() - self.start) * 1000
                with self.profiler._lock:
                    self.profiler.timings[self.name].append(elapsed_ms)

        return Timer(self, name)

    def report(self) -> str:
        """Generate latency report"""
        with self._lock:
            lines = ["Latency Profile:"]
            lines.append("-" * 70)

            for name in sorted(self.timings.keys()):
                times = self.timings[name]
                if not times:
                    continue

                sorted_times = sorted(times)
                avg = sum(times) / len(times)
                p50 = sorted_times[len(sorted_times) // 2]
                p95 = sorted_times[int(len(sorted_times) * 0.95)]
                max_time = max(times)

                lines.append(
                    f"  {name:30s}: "
                    f"avg={avg:6.2f}ms  "
                    f"p50={p50:6.2f}ms  "
                    f"p95={p95:6.2f}ms  "
                    f"max={max_time:6.2f}ms  "
                    f"n={len(times)}"
                )

            return "\n".join(lines)

    def clear(self):
        """Clear all timing data"""
        with self._lock:
            self.timings.clear()


def example_latency_profiling():
    """Example: Profile voice wake latency"""

    profiler = LatencyProfiler()

    # Simulate voice wake loop
    for i in range(100):
        with profiler.measure("audio_read"):
            time.sleep(0.010)  # Simulate 10ms audio read

        with profiler.measure("preprocessing"):
            time.sleep(0.001)  # Simulate 1ms preprocessing

        with profiler.measure("inference"):
            time.sleep(0.030)  # Simulate 30ms inference

        with profiler.measure("postprocessing"):
            time.sleep(0.002)  # Simulate 2ms postprocessing

    print(profiler.report())


# ============================================================================
# 6. ONNX QUANTIZATION EXAMPLES
# ============================================================================

def quantize_model_dynamic(
    model_fp32_path: str,
    model_int8_path: str,
) -> None:
    """
    Quantize ONNX model using dynamic quantization.

    This is the easiest approach and works well for CPU inference.

    Args:
        model_fp32_path: Path to FP32 ONNX model
        model_int8_path: Path to save INT8 model
    """
    try:
        from onnxruntime.quantization import quantize_dynamic, QuantType
    except ImportError:
        print("ERROR: onnxruntime not installed")
        print("Install with: pip install onnxruntime")
        return

    print(f"Quantizing {model_fp32_path}...")
    print(f"Output: {model_int8_path}")

    quantize_dynamic(
        model_input=model_fp32_path,
        model_output=model_int8_path,
        weight_type=QuantType.QUInt8,  # or QInt8
        optimize_model=True,
        per_channel=True,  # Better accuracy
    )

    # Compare file sizes
    fp32_size = Path(model_fp32_path).stat().st_size / (1024 * 1024)
    int8_size = Path(model_int8_path).stat().st_size / (1024 * 1024)

    print(f"FP32 size: {fp32_size:.2f} MB")
    print(f"INT8 size: {int8_size:.2f} MB")
    print(f"Reduction: {(1 - int8_size/fp32_size)*100:.1f}%")


def quantize_sherpa_onnx_models(model_dir: Path) -> None:
    """
    Quantize all sherpa-onnx models in a directory.

    Args:
        model_dir: Directory containing encoder.onnx, decoder.onnx, joiner.onnx
    """
    models = ["encoder", "decoder", "joiner"]

    for model_name in models:
        fp32_path = model_dir / f"{model_name}.onnx"
        int8_path = model_dir / f"{model_name}_int8.onnx"

        if not fp32_path.exists():
            print(f"WARNING: {fp32_path} not found, skipping")
            continue

        if int8_path.exists():
            print(f"INFO: {int8_path} already exists, skipping")
            continue

        quantize_model_dynamic(str(fp32_path), str(int8_path))
        print()


# ============================================================================
# 7. AUDIO BUFFER OPTIMIZATION
# ============================================================================

class RingBuffer:
    """
    Efficient ring buffer for audio samples.

    Uses numpy for fast operations and fixed memory allocation.
    """

    def __init__(self, max_samples: int):
        """
        Args:
            max_samples: Maximum number of samples to store
        """
        self.max_samples = max_samples
        self.buffer = np.zeros(max_samples, dtype=np.float32)
        self.write_pos = 0
        self.available = 0

    def write(self, samples: np.ndarray) -> None:
        """Write samples to buffer (overwrites old data if full)"""
        n = len(samples)

        if n >= self.max_samples:
            # If input is larger than buffer, just keep the last max_samples
            self.buffer[:] = samples[-self.max_samples:]
            self.write_pos = 0
            self.available = self.max_samples
            return

        # Calculate how much we can write before wrapping
        space_to_end = self.max_samples - self.write_pos

        if n <= space_to_end:
            # No wrap needed
            self.buffer[self.write_pos:self.write_pos + n] = samples
            self.write_pos = (self.write_pos + n) % self.max_samples
        else:
            # Need to wrap around
            self.buffer[self.write_pos:] = samples[:space_to_end]
            remaining = n - space_to_end
            self.buffer[:remaining] = samples[space_to_end:]
            self.write_pos = remaining

        self.available = min(self.available + n, self.max_samples)

    def read_last(self, n: int) -> np.ndarray:
        """Read last n samples (most recent)"""
        if n > self.available:
            n = self.available

        if n == 0:
            return np.array([], dtype=np.float32)

        # Calculate start position
        start = (self.write_pos - n) % self.max_samples

        if start + n <= self.max_samples:
            # No wrap
            return self.buffer[start:start + n].copy()
        else:
            # Wrap around
            part1 = self.buffer[start:]
            part2 = self.buffer[:n - len(part1)]
            return np.concatenate([part1, part2])

    def clear(self) -> None:
        """Clear buffer"""
        self.write_pos = 0
        self.available = 0


def example_ring_buffer_usage():
    """Example: Using ring buffer for owner verification"""

    # Create buffer for 1.6 seconds at 16kHz
    sample_rate = 16000
    window_s = 1.6
    max_samples = int(sample_rate * window_s)

    buffer = RingBuffer(max_samples)

    # Simulate audio streaming
    chunk_size = int(sample_rate * 0.1)  # 100ms chunks

    for i in range(20):  # 2 seconds of audio
        # Generate fake audio chunk
        chunk = np.random.randn(chunk_size).astype(np.float32) * 0.1

        # Write to buffer
        buffer.write(chunk)

        # Read last 1.6 seconds for verification
        verify_samples = buffer.read_last(max_samples)
        print(f"Chunk {i}: buffer has {len(verify_samples)} samples")


# ============================================================================
# 8. COMPLETE OPTIMIZED VOICE WAKE EXAMPLE
# ============================================================================

def optimized_voice_wake_loop_example():
    """
    Complete example showing all optimizations applied to voice wake loop.

    This is a simplified version showing the key patterns.
    """

    # Configuration
    sample_rate = 16000
    samples_per_read = int(sample_rate * 0.1)  # 100ms at 10Hz
    MAX_DECODE_ITERATIONS = 10
    MAX_DECODE_TIME_MS = 50

    # Monitoring
    cpu_monitor = CPUMonitor(interval=1.0)
    profiler = LatencyProfiler()

    # Caching
    keyword_cache = TTLCache(ttl_seconds=3600)

    # Owner verification buffer
    owner_window_s = 1.6
    owner_buffer = RingBuffer(int(sample_rate * owner_window_s))

    print("Starting optimized voice wake loop...")
    cpu_monitor.start()

    try:
        # Simulated main loop
        for iteration in range(100):
            with profiler.measure("total_iteration"):
                # 1. Read audio (blocking, no busy-wait)
                with profiler.measure("audio_read"):
                    # audio, _ = mic.read(samples_per_read)
                    audio = np.random.randn(samples_per_read).astype(np.float32)

                # 2. Preprocess
                with profiler.measure("preprocess"):
                    samples = np.ascontiguousarray(audio.reshape(-1))
                    owner_buffer.write(samples)

                # 3. Feed to model
                with profiler.measure("accept_waveform"):
                    # stream.accept_waveform(sample_rate, samples)
                    pass

                # 4. Decode with safety limits
                with profiler.measure("decode_loop"):
                    decode_start = time.perf_counter()
                    decode_count = 0

                    # Simulated decode loop
                    while decode_count < 3:  # Simulate spotter.is_ready()
                        # Check limits
                        if decode_count >= MAX_DECODE_ITERATIONS:
                            print(f"Decode limit: iterations={decode_count}")
                            break

                        elapsed_ms = (time.perf_counter() - decode_start) * 1000
                        if elapsed_ms > MAX_DECODE_TIME_MS:
                            print(f"Decode limit: time={elapsed_ms:.1f}ms")
                            break

                        # spotter.decode_stream(stream)
                        time.sleep(0.005)  # Simulate 5ms inference
                        decode_count += 1

                # 5. Check result
                with profiler.measure("get_result"):
                    # result = spotter.get_result(stream)
                    pass

            # Print progress every 10 iterations
            if (iteration + 1) % 10 == 0:
                print(f"Completed {iteration + 1} iterations")

    finally:
        # Stop monitoring and print results
        cpu_stats = cpu_monitor.stop()
        print("\n" + "=" * 70)
        print("PERFORMANCE RESULTS")
        print("=" * 70)
        print(f"\nCPU Usage:")
        print(f"  Average: {cpu_stats['avg']:.1f}%")
        print(f"  P95: {cpu_stats['p95']:.1f}%")
        print(f"  Maximum: {cpu_stats['max']:.1f}%")
        print(f"\n{profiler.report()}")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print("Voice Wake CPU Optimization Examples")
    print("=" * 70)
    print()

    # Run examples
    print("1. CPU Monitoring Example")
    print("-" * 70)
    # example_cpu_monitoring()  # Uncomment to run (takes 60s)

    print("\n2. Latency Profiling Example")
    print("-" * 70)
    example_latency_profiling()

    print("\n3. Ring Buffer Example")
    print("-" * 70)
    example_ring_buffer_usage()

    print("\n4. Optimized Voice Wake Loop Example")
    print("-" * 70)
    optimized_voice_wake_loop_example()

    print("\n" + "=" * 70)
    print("Examples completed!")
    print("=" * 70)
