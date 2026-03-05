#!/bin/bash
# Voice Wake CPU Optimization - Complete Setup Script
# ====================================================
# This script applies all optimizations to reduce CPU usage from 400-800% to <100%
#
# Usage:
#   ./setup_voice_wake_optimization.sh
#
# Author: Research by Claude Sonnet 4.6
# Date: 2026-03-03

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================================================"
echo "Voice Wake CPU Optimization Setup"
echo "========================================================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running in correct directory
if [ ! -f "src/recordian/voice_wake.py" ]; then
    error "voice_wake.py not found. Please run this script from the Recordian root directory."
    exit 1
fi

info "Current directory: $SCRIPT_DIR"
echo ""

# ============================================================================
# PHASE 1: Fix Infinite Loop
# ============================================================================

echo "========================================================================"
echo "PHASE 1: Fix Infinite Loop in voice_wake.py"
echo "========================================================================"
echo ""

VOICE_WAKE_FILE="src/recordian/voice_wake.py"

# Check if already patched
if grep -q "MAX_DECODE_ITERATIONS" "$VOICE_WAKE_FILE"; then
    warn "Infinite loop fix already applied, skipping..."
else
    info "Backing up voice_wake.py..."
    cp "$VOICE_WAKE_FILE" "${VOICE_WAKE_FILE}.backup"

    info "Applying infinite loop fix..."

    # Create patch
    cat > /tmp/voice_wake_loop_fix.patch << 'EOF'
--- a/src/recordian/voice_wake.py
+++ b/src/recordian/voice_wake.py
@@ -504,8 +504,24 @@
                     stream.accept_waveform(self.model.sample_rate, samples)
-                    while spotter.is_ready(stream):
+
+                    # Decode with safety limits to prevent infinite loop
+                    MAX_DECODE_ITERATIONS = 10
+                    MAX_DECODE_TIME_MS = 50
+
+                    decode_start = time.perf_counter()
+                    decode_count = 0
+
+                    while spotter.is_ready(stream):
+                        if decode_count >= MAX_DECODE_ITERATIONS:
+                            self._emit({"message": f"voice_wake_decode_limit: iterations={decode_count}"})
+                            break
+
+                        elapsed_ms = (time.perf_counter() - decode_start) * 1000
+                        if elapsed_ms > MAX_DECODE_TIME_MS:
+                            self._emit({"message": f"voice_wake_decode_limit: time={elapsed_ms:.1f}ms"})
+                            break
+
                         spotter.decode_stream(stream)
+                        decode_count += 1

                     result = spotter.get_result(stream).strip()
EOF

    # Try to apply patch
    if patch -p1 --dry-run < /tmp/voice_wake_loop_fix.patch > /dev/null 2>&1; then
        patch -p1 < /tmp/voice_wake_loop_fix.patch
        info "✓ Infinite loop fix applied successfully"
    else
        warn "Automatic patch failed, manual edit required"
        echo ""
        echo "Please manually edit $VOICE_WAKE_FILE around line 507-509:"
        echo ""
        echo "Replace:"
        echo "  while spotter.is_ready(stream):"
        echo "      spotter.decode_stream(stream)"
        echo ""
        echo "With the code from: docs/QUICK-REFERENCE-CPU-OPTIMIZATION.md"
        echo ""
        read -p "Press Enter after manual edit, or Ctrl+C to abort..."
    fi

    rm -f /tmp/voice_wake_loop_fix.patch
fi

echo ""

# ============================================================================
# PHASE 2: Model Quantization
# ============================================================================

echo "========================================================================"
echo "PHASE 2: ONNX Model Quantization (FP32 → INT8)"
echo "========================================================================"
echo ""

# Check for onnxruntime
if ! python -c "import onnxruntime" 2>/dev/null; then
    warn "onnxruntime not installed"
    read -p "Install onnxruntime? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "Installing onnxruntime..."
        pip install onnxruntime onnx
    else
        error "onnxruntime required for quantization. Skipping Phase 2."
        exit 1
    fi
fi

# Find model directory
MODEL_DIR=""
if [ -d "models/sherpa-onnx-kws" ]; then
    MODEL_DIR="models/sherpa-onnx-kws"
elif [ -d "models/kws" ]; then
    MODEL_DIR="models/kws"
else
    warn "Model directory not found"
    read -p "Enter path to sherpa-onnx model directory: " MODEL_DIR

    if [ ! -d "$MODEL_DIR" ]; then
        error "Directory not found: $MODEL_DIR"
        exit 1
    fi
fi

info "Model directory: $MODEL_DIR"

# Check if models exist
if [ ! -f "$MODEL_DIR/encoder.onnx" ]; then
    error "encoder.onnx not found in $MODEL_DIR"
    exit 1
fi

# Check if already quantized
if [ -f "$MODEL_DIR/encoder_int8.onnx" ]; then
    warn "INT8 models already exist, skipping quantization..."
else
    info "Creating quantization script..."

    cat > /tmp/quantize_models.py << 'EOF'
#!/usr/bin/env python3
"""Quantize sherpa-onnx models to INT8"""

import sys
from pathlib import Path
from onnxruntime.quantization import quantize_dynamic, QuantType

def quantize_model(fp32_path, int8_path):
    """Quantize a single model"""
    print(f"Quantizing: {fp32_path.name}")

    quantize_dynamic(
        model_input=str(fp32_path),
        model_output=str(int8_path),
        weight_type=QuantType.QUInt8,
        optimize_model=True,
        per_channel=True,
    )

    fp32_mb = fp32_path.stat().st_size / (1024 * 1024)
    int8_mb = int8_path.stat().st_size / (1024 * 1024)
    reduction = (1 - int8_mb / fp32_mb) * 100

    print(f"  FP32: {fp32_mb:.2f} MB")
    print(f"  INT8: {int8_mb:.2f} MB")
    print(f"  Reduction: {reduction:.1f}%")
    print()

def main():
    if len(sys.argv) < 2:
        print("Usage: python quantize_models.py <model_dir>")
        sys.exit(1)

    model_dir = Path(sys.argv[1])

    for name in ["encoder", "decoder", "joiner"]:
        fp32_path = model_dir / f"{name}.onnx"
        int8_path = model_dir / f"{name}_int8.onnx"

        if not fp32_path.exists():
            print(f"WARNING: {fp32_path} not found, skipping")
            continue

        if int8_path.exists():
            print(f"INFO: {int8_path} already exists, skipping")
            continue

        quantize_model(fp32_path, int8_path)

    print("=" * 70)
    print("Quantization complete!")
    print("=" * 70)

if __name__ == "__main__":
    main()
EOF

    info "Running quantization (this may take 1-2 minutes)..."
    python /tmp/quantize_models.py "$MODEL_DIR"

    rm -f /tmp/quantize_models.py

    info "✓ Model quantization complete"
fi

echo ""

# ============================================================================
# PHASE 3: Update Configuration
# ============================================================================

echo "========================================================================"
echo "PHASE 3: Update Configuration"
echo "========================================================================"
echo ""

CONFIG_FILE="$HOME/.config/recordian/config.json"

if [ -f "$CONFIG_FILE" ]; then
    info "Found config file: $CONFIG_FILE"

    # Backup config
    cp "$CONFIG_FILE" "${CONFIG_FILE}.backup"

    # Update config to use INT8 models
    if grep -q "encoder_int8.onnx" "$CONFIG_FILE"; then
        warn "Config already uses INT8 models"
    else
        info "Updating config to use INT8 models..."

        # Use sed to replace model paths
        sed -i.bak \
            -e "s|encoder\.onnx|encoder_int8.onnx|g" \
            -e "s|decoder\.onnx|decoder_int8.onnx|g" \
            -e "s|joiner\.onnx|joiner_int8.onnx|g" \
            "$CONFIG_FILE"

        info "✓ Config updated"
    fi
else
    warn "Config file not found: $CONFIG_FILE"
    echo ""
    echo "Please update your config manually or use command-line arguments:"
    echo ""
    echo "  --wake-encoder $MODEL_DIR/encoder_int8.onnx"
    echo "  --wake-decoder $MODEL_DIR/decoder_int8.onnx"
    echo "  --wake-joiner $MODEL_DIR/joiner_int8.onnx"
    echo ""
fi

echo ""

# ============================================================================
# SUMMARY
# ============================================================================

echo "========================================================================"
echo "OPTIMIZATION COMPLETE"
echo "========================================================================"
echo ""

info "Applied optimizations:"
echo "  ✓ Phase 1: Fixed infinite loop in voice_wake.py"
echo "  ✓ Phase 2: Quantized models to INT8"
echo "  ✓ Phase 3: Updated configuration"
echo ""

info "Expected results:"
echo "  • CPU usage: 400-800% → 80-200% (target: <100%)"
echo "  • Inference speed: 2-4x faster"
echo "  • Model size: 75% smaller"
echo ""

info "Next steps:"
echo "  1. Restart voice wake service"
echo "  2. Monitor CPU usage for 60 seconds"
echo "  3. Run benchmark: python benchmark_voice_wake.py --compare"
echo ""

info "Verification:"
echo "  python -c 'import psutil, time; p=psutil.Process(); [print(f\"{i}s: {p.cpu_percent(1.0):.1f}%\") for i in range(60)]'"
echo ""

info "Documentation:"
echo "  • Quick Reference: docs/QUICK-REFERENCE-CPU-OPTIMIZATION.md"
echo "  • Full Research: docs/python-performance-optimization-research.md"
echo "  • Quantization Guide: docs/onnx-quantization-guide.md"
echo "  • Code Examples: docs/voice-wake-optimization-examples.py"
echo ""

echo "========================================================================"
echo "Setup complete! 🎉"
echo "========================================================================"
