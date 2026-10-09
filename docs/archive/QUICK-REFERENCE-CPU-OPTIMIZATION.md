# Voice Wake CPU Optimization - Quick Reference Card

**Target**: Reduce CPU from 400-800% to <100%
**Date**: 2026-03-03

---

## 🚨 Critical Fix (Do This First!)

### Fix Infinite Loop in voice_wake.py

**File**: `src/recordian/voice_wake.py`
**Lines**: 507-509

**Add these constants at the top of the _run method (around line 387)**:
```python
MAX_DECODE_ITERATIONS = 10
MAX_DECODE_TIME_MS = 50
```

**Replace lines 507-509**:
```python
# OLD CODE (REMOVE):
while spotter.is_ready(stream):
    spotter.decode_stream(stream)

# NEW CODE (ADD):
decode_start = time.perf_counter()
decode_count = 0

while spotter.is_ready(stream):
    # Check iteration limit
    if decode_count >= MAX_DECODE_ITERATIONS:
        self._emit({"message": f"voice_wake_decode_limit: iterations={decode_count}"})
        break

    # Check time limit
    elapsed_ms = (time.perf_counter() - decode_start) * 1000
    if elapsed_ms > MAX_DECODE_TIME_MS:
        self._emit({"message": f"voice_wake_decode_limit: time={elapsed_ms:.1f}ms"})
        break

    spotter.decode_stream(stream)
    decode_count += 1
```

**Expected Result**: CPU drops by 50-100%

---

## 🚀 Model Quantization (Do This Second!)

### Quick Start

```bash
# 1. Install tools
pip install onnxruntime onnx

# 2. Create quantization script
cat > quantize_models.py << 'EOF'
from pathlib import Path
from onnxruntime.quantization import quantize_dynamic, QuantType

model_dir = Path("models/sherpa-onnx-kws")

for name in ["encoder", "decoder", "joiner"]:
    fp32 = model_dir / f"{name}.onnx"
    int8 = model_dir / f"{name}_int8.onnx"

    if not fp32.exists():
        print(f"Skip {name}: not found")
        continue

    if int8.exists():
        print(f"Skip {name}: already quantized")
        continue

    print(f"Quantizing {name}...")
    quantize_dynamic(
        model_input=str(fp32),
        model_output=str(int8),
        weight_type=QuantType.QUInt8,
        optimize_model=True,
        per_channel=True,
    )

    fp32_mb = fp32.stat().st_size / (1024*1024)
    int8_mb = int8.stat().st_size / (1024*1024)
    print(f"  {fp32_mb:.1f}MB -> {int8_mb:.1f}MB ({(1-int8_mb/fp32_mb)*100:.0f}% smaller)")

print("\nDone! Update config to use *_int8.onnx models")
EOF

# 3. Run quantization
python quantize_models.py

# 4. Update config (choose one):

# Option A: Command line
python -m recordian.tray_gui \
    --wake-encoder models/sherpa-onnx-kws/encoder_int8.onnx \
    --wake-decoder models/sherpa-onnx-kws/decoder_int8.onnx \
    --wake-joiner models/sherpa-onnx-kws/joiner_int8.onnx

# Option B: Config file (~/.config/recordian/config.json)
# Edit and change:
#   "wake_encoder": "models/sherpa-onnx-kws/encoder_int8.onnx"
#   "wake_decoder": "models/sherpa-onnx-kws/decoder_int8.onnx"
#   "wake_joiner": "models/sherpa-onnx-kws/joiner_int8.onnx"
```

**Expected Result**: CPU drops by 60-75%, 2-4x faster inference

---

## 📊 Verify Results

### Check CPU Usage

```bash
# Monitor for 60 seconds
python -c "
import psutil
import time

process = psutil.Process()
samples = []

print('Monitoring CPU for 60 seconds...')
for i in range(60):
    cpu = process.cpu_percent(interval=1.0)
    samples.append(cpu)
    if (i+1) % 10 == 0:
        avg = sum(samples) / len(samples)
        print(f'{i+1}s: current={cpu:.1f}% avg={avg:.1f}%')

avg = sum(samples) / len(samples)
p95 = sorted(samples)[int(len(samples)*0.95)]
print(f'\nFinal: avg={avg:.1f}% p95={p95:.1f}% max={max(samples):.1f}%')

if avg < 100:
    print('✅ SUCCESS: CPU < 100%')
else:
    print(f'❌ FAIL: CPU still {avg:.1f}% (target <100%)')
"
```

### Benchmark Performance

```bash
# Compare FP32 vs INT8
python benchmark_voice_wake.py --compare --iterations 100
```

---

## 🎯 Success Criteria

- ✅ CPU usage < 100% (average)
- ✅ No infinite loop warnings in logs
- ✅ Latency < 50ms per frame
- ✅ Accuracy > 99%

---

## 📚 Full Documentation

- **Research**: `docs/python-performance-optimization-research.md`
- **Code Examples**: `docs/voice-wake-optimization-examples.py`
- **Quantization Guide**: `docs/onnx-quantization-guide.md`
- **Summary**: `docs/voice-wake-optimization-summary.md`
- **Benchmark Script**: `benchmark_voice_wake.py`

---

## 🐛 Troubleshooting

### Issue: CPU still > 100% after both fixes

**Check**:
1. Verify infinite loop fix is applied (check logs for "decode_limit" messages)
2. Verify INT8 models are loaded (check file sizes, should be ~25% of FP32)
3. Check num_threads setting (should be 2)
4. Monitor with: `htop` or `top` to see which process is using CPU

**Additional fixes**:
- Reduce num_threads to 1
- Check for other CPU-intensive processes
- Profile with: `python -m cProfile -o profile.stats your_script.py`

### Issue: Quantization fails

**Error**: "Model has unsupported operators"

**Fix**:
```python
# Try without optimization
quantize_dynamic(
    model_input="encoder.onnx",
    model_output="encoder_int8.onnx",
    weight_type=QuantType.QUInt8,
    optimize_model=False,  # Disable optimization
)
```

### Issue: Accuracy loss > 1%

**Fix**:
- Use per-channel quantization: `per_channel=True`
- Try static quantization with calibration data
- See: `docs/onnx-quantization-guide.md` section "Static Quantization"

---

## 📞 Support

For detailed information, see:
- `docs/python-performance-optimization-research.md` - Complete research
- `docs/onnx-quantization-guide.md` - Quantization details
- `docs/voice-wake-optimization-examples.py` - Working code examples

---

**Quick Summary**:
1. Fix infinite loop → -50-100% CPU
2. Quantize models → -60-75% CPU
3. Total reduction: 400-800% → 80-200% (target: <100%)
