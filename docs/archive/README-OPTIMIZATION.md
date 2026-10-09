# Voice Wake CPU Optimization - Documentation Index

**Date**: 2026-03-03
**Research by**: Claude Sonnet 4.6
**Goal**: Reduce CPU usage from 400-800% to <100%

---

## 📚 Documentation Overview

This directory contains comprehensive research and implementation guides for optimizing the sherpa-onnx voice wake CPU usage.

### Quick Start

**If you just want to fix the issue quickly**:
1. Read: [`QUICK-REFERENCE-CPU-OPTIMIZATION.md`](QUICK-REFERENCE-CPU-OPTIMIZATION.md)
2. Run: `../setup_voice_wake_optimization.sh`

**If you want to understand the details**:
1. Read: [`voice-wake-optimization-summary.md`](voice-wake-optimization-summary.md)
2. Study: [`python-performance-optimization-research.md`](python-performance-optimization-research.md)
3. Review: [`voice-wake-optimization-examples.py`](voice-wake-optimization-examples.py)

---

## 📖 Document Guide

### 1. Quick Reference (Start Here!)

**File**: [`QUICK-REFERENCE-CPU-OPTIMIZATION.md`](QUICK-REFERENCE-CPU-OPTIMIZATION.md)
- **Size**: 5.5 KB
- **Read Time**: 5 minutes
- **Purpose**: Quick implementation guide with copy-paste code

**Contents**:
- Critical fix for infinite loop (immediate)
- Model quantization quick start (1-2 days)
- Verification commands
- Troubleshooting tips

**Use this when**: You want to fix the issue NOW

---

### 2. Implementation Summary

**File**: [`voice-wake-optimization-summary.md`](voice-wake-optimization-summary.md)
- **Size**: 9.3 KB
- **Read Time**: 10-15 minutes
- **Purpose**: Complete implementation roadmap

**Contents**:
- Problem analysis
- Optimization plan (3 phases)
- Expected results
- Implementation checklist
- Key insights
- Monitoring and validation

**Use this when**: You want a complete overview before starting

---

### 3. Research Document

**File**: [`python-performance-optimization-research.md`](python-performance-optimization-research.md)
- **Size**: 18 KB
- **Read Time**: 30-45 minutes
- **Purpose**: Comprehensive research on Python performance patterns

**Contents**:
- Current issue analysis
- Loop limiting patterns (3 approaches)
- Thread optimization strategies
- TTL + LRU caching patterns
- ONNX quantization techniques
- Benchmarking code
- Performance impact analysis

**Use this when**: You want to understand WHY and HOW

---

### 4. Code Examples

**File**: [`voice-wake-optimization-examples.py`](voice-wake-optimization-examples.py)
- **Size**: 22 KB
- **Lines**: 684
- **Purpose**: Complete working code examples

**Contents**:
- Loop limiting implementations
- TTL cache class
- LRU cache patterns
- CPU monitoring tools
- Latency profiling
- Ring buffer for audio
- ONNX quantization functions
- Complete optimized voice wake loop

**Use this when**: You want working code to copy/adapt

---

### 5. Quantization Guide

**File**: [`onnx-quantization-guide.md`](onnx-quantization-guide.md)
- **Size**: 17 KB
- **Read Time**: 20-30 minutes
- **Purpose**: Step-by-step ONNX model quantization

**Contents**:
- Quantization overview (FP32 → INT8)
- Dynamic vs static quantization
- Quick start guide
- Validation and testing scripts
- Advanced static quantization
- Troubleshooting

**Use this when**: You're ready to quantize models

---

### 6. Benchmark Script

**File**: [`../benchmark_voice_wake.py`](../benchmark_voice_wake.py)
- **Size**: 15 KB
- **Lines**: 487
- **Purpose**: Comprehensive performance benchmarking

**Features**:
- FP32 vs INT8 comparison
- CPU, latency, memory metrics
- Automated assessment
- JSON output for tracking
- Simulated mode (works without sherpa-onnx)

**Usage**:
```bash
# Compare FP32 vs INT8
python benchmark_voice_wake.py --compare --iterations 100

# Benchmark single model
python benchmark_voice_wake.py --model-dir models/sherpa-onnx-kws
```

---

### 7. Setup Script

**File**: [`../setup_voice_wake_optimization.sh`](../setup_voice_wake_optimization.sh)
- **Size**: 8.5 KB
- **Purpose**: Automated setup for all optimizations

**What it does**:
1. Backs up voice_wake.py
2. Applies infinite loop fix
3. Installs onnxruntime (if needed)
4. Quantizes models to INT8
5. Updates configuration
6. Provides verification commands

**Usage**:
```bash
cd /home/zz8011/文档/Develop/Recordian
./setup_voice_wake_optimization.sh
```

---

## 🎯 Implementation Roadmap

### Phase 1: Critical Fix (30 minutes)

**Goal**: Fix infinite loop
**Expected**: -50-100% CPU

1. Read: `QUICK-REFERENCE-CPU-OPTIMIZATION.md` (Section: Critical Fix)
2. Edit: `src/recordian/voice_wake.py` (Lines 507-509)
3. Test: Monitor CPU for 60 seconds
4. Verify: CPU should drop significantly

### Phase 2: Model Quantization (1-2 days)

**Goal**: Quantize models to INT8
**Expected**: -60-75% CPU, 2-4x faster

1. Read: `onnx-quantization-guide.md` (Section: Quick Start)
2. Install: `pip install onnxruntime onnx`
3. Run: Quantization script
4. Update: Configuration to use INT8 models
5. Test: Accuracy and performance
6. Benchmark: `python benchmark_voice_wake.py --compare`

### Phase 3: Caching (Optional, 2-4 hours)

**Goal**: Add caching for keyword files
**Expected**: -5-10% CPU, faster startup

1. Read: `voice-wake-optimization-examples.py` (TTLCache class)
2. Implement: Cache in voice_wake.py
3. Test: Cache hit rates
4. Measure: Performance improvement

---

## 📊 Expected Results

| Metric | Before | After Phase 1 | After Phase 2 | Target |
|--------|--------|---------------|---------------|--------|
| CPU Usage | 400-800% | 300-700% | **80-200%** | <100% |
| Inference | 60ms | 60ms | 15-30ms | <50ms |
| Model Size | 100 MB | 100 MB | 25 MB | - |
| Memory | 200 MB | 200 MB | 80 MB | - |

---

## 🔍 Verification Commands

### Check CPU Usage
```bash
python -c "
import psutil, time
p = psutil.Process()
samples = []
for i in range(60):
    cpu = p.cpu_percent(1.0)
    samples.append(cpu)
    if (i+1) % 10 == 0:
        print(f'{i+1}s: {cpu:.1f}% (avg={sum(samples)/len(samples):.1f}%)')
avg = sum(samples) / len(samples)
print(f'\nFinal: avg={avg:.1f}% (target: <100%)')
print('✅ PASS' if avg < 100 else '❌ FAIL')
"
```

### Benchmark Performance
```bash
python benchmark_voice_wake.py --compare --iterations 100
```

### Check Model Sizes
```bash
ls -lh models/sherpa-onnx-kws/*.onnx | awk '{print $9, $5}'
```

---

## 🐛 Troubleshooting

### Issue: CPU still > 100%

**Check**:
1. Verify infinite loop fix applied: `grep -n "MAX_DECODE_ITERATIONS" src/recordian/voice_wake.py`
2. Verify INT8 models loaded: `ls -lh models/sherpa-onnx-kws/*_int8.onnx`
3. Check num_threads: Should be 2
4. Profile: `python -m cProfile -o profile.stats your_script.py`

**Solutions**:
- Reduce num_threads to 1
- Check for other CPU bottlenecks
- See: `QUICK-REFERENCE-CPU-OPTIMIZATION.md` (Troubleshooting section)

### Issue: Quantization fails

**Error**: "Model has unsupported operators"

**Solution**: Try without optimization
```python
quantize_dynamic(
    model_input="encoder.onnx",
    model_output="encoder_int8.onnx",
    weight_type=QuantType.QUInt8,
    optimize_model=False,  # Disable
)
```

### Issue: Accuracy loss

**Solution**: Use per-channel quantization
```python
quantize_dynamic(
    ...,
    per_channel=True,  # Better accuracy
)
```

---

## 📚 Additional Resources

### Python Performance
- [Python Performance Tips](https://wiki.python.org/moin/PythonSpeed/PerformanceTips)
- [Profiling Python Code](https://docs.python.org/3/library/profile.html)

### ONNX Quantization
- [ONNX Runtime Quantization](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)
- [Quantization Best Practices](https://github.com/microsoft/onnxruntime/blob/main/onnxruntime/python/tools/quantization/README.md)

### Sherpa-ONNX
- [Sherpa-ONNX Documentation](https://k2-fsa.github.io/sherpa/onnx/)
- [Keyword Spotting Guide](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)
- [Performance Tuning](https://k2-fsa.github.io/sherpa/onnx/performance.html)

---

## 📞 Support

For questions or issues:
1. Check: `QUICK-REFERENCE-CPU-OPTIMIZATION.md` (Troubleshooting)
2. Review: `python-performance-optimization-research.md` (Detailed analysis)
3. Study: `voice-wake-optimization-examples.py` (Working code)

---

## ✅ Summary

**Total Documentation**: 2,768 lines across 5 files
- Quick reference: 5.5 KB
- Research: 18 KB
- Code examples: 22 KB
- Quantization guide: 17 KB
- Summary: 9.3 KB
- Benchmark script: 15 KB
- Setup script: 8.5 KB

**Expected Outcome**:
- CPU: 400-800% → 80-200% (target: <100%)
- Speed: 2-4x faster inference
- Size: 75% smaller models
- Accuracy: >99% preserved

**Status**: ✅ Research complete, ready for implementation
