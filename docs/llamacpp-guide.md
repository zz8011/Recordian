# llama.cpp 集成指南

## 简介

llama.cpp 是一个高性能的 LLM 推理引擎，相比 transformers 有以下优势：

- **速度更快**：50-150ms（比 transformers 快 2-3x）
- **显存更低**：量化后只需 ~400MB（原来 1.5GB）
- **支持 CPU**：GPU 不够时可以降级到 CPU
- **轻量级**：不依赖庞大的 PyTorch

## 安装

### 1. 安装 llama-cpp-python（带 CUDA 支持）

```bash
CMAKE_ARGS="-DLLAMA_CUDA=on" pip install llama-cpp-python
```

如果遇到编译问题，可以尝试：

```bash
# 指定 CUDA 路径
CMAKE_ARGS="-DLLAMA_CUDA=on -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc" pip install llama-cpp-python

# 或者使用预编译版本（可能不支持你的 GPU）
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121
```

### 2. 转换模型为 GGUF 格式

```bash
# 克隆 llama.cpp
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp

# 编译
make

# 转换 Qwen3-0.6B 为 FP16 GGUF
python convert_hf_to_gguf.py /home/zz8011/文档/Develop/Recordian/models/Qwen3-0.6B --outtype f16

# 量化为 Q4_K_M（推荐，减少 75% 显存）
./llama-quantize models/Qwen3-0.6B/ggml-model-f16.gguf models/Qwen3-0.6B/qwen3-0.6b-q4_k_m.gguf Q4_K_M
```

量化选项：
- **Q4_K_M**：推荐，平衡速度和质量
- **Q5_K_M**：质量更高，稍慢
- **Q8_0**：接近 FP16 质量，显存占用中等

## 配置

### 方法 1：修改配置文件

编辑 `~/.config/recordian/hotkey.json`：

```json
{
  "enable_text_refine": true,
  "refine_provider": "llamacpp",
  "refine_model": "/home/zz8011/文档/Develop/Recordian/models/Qwen3-0.6B/qwen3-0.6b-q4_k_m.gguf",
  "refine_n_gpu_layers": -1,
  "refine_max_tokens": 512,
  "refine_preset": "default"
}
```

参数说明：
- `refine_provider`: 设置为 `llamacpp`
- `refine_model`: GGUF 模型文件路径
- `refine_n_gpu_layers`: GPU 层数（-1 = 全部放 GPU，0 = 全部用 CPU）
- `refine_max_tokens`: 最大生成 token 数

### 方法 2：通过 GUI Settings

1. 点击托盘图标 → Settings
2. 修改：
   - **精炼 Provider**: `llamacpp`
   - **本地精炼模型路径**: `/path/to/qwen3-0.6b-q4_k_m.gguf`
   - **llama.cpp GPU 层数**: `-1`
3. 保存并重启

## 性能对比

| 方案 | 延迟 | 显存占用 | 安装难度 |
|------|------|----------|----------|
| transformers（当前） | 100-300ms | ~1.5GB | 简单 |
| **llama.cpp Q4** | **50-150ms** | **~400MB** | 中等 |
| llama.cpp FP16 | 50-120ms | ~1.2GB | 中等 |
| Groq 云端 | 100-200ms | 0 | 简单 |

## 故障排除

### 1. 编译失败

```bash
# 确保安装了 CUDA toolkit
nvcc --version

# 确保安装了 cmake
cmake --version

# 如果还是失败，尝试 CPU 版本
pip install llama-cpp-python
```

### 2. 模型加载失败

检查：
- GGUF 文件路径是否正确
- 文件是否完整（没有下载中断）
- 是否有读取权限

### 3. 显存不足

```json
{
  "refine_n_gpu_layers": 20  // 只放部分层到 GPU
}
```

或者完全使用 CPU：

```json
{
  "refine_n_gpu_layers": 0  // 全部用 CPU
}
```

## 推荐配置

### 高性能（GPU 充足）

```json
{
  "refine_provider": "llamacpp",
  "refine_model": "/path/to/qwen3-0.6b-q4_k_m.gguf",
  "refine_n_gpu_layers": -1
}
```

### 低显存（< 2GB 可用）

```json
{
  "refine_provider": "llamacpp",
  "refine_model": "/path/to/qwen3-0.6b-q4_k_m.gguf",
  "refine_n_gpu_layers": 10  // 部分层用 GPU
}
```

### CPU 模式

```json
{
  "refine_provider": "llamacpp",
  "refine_model": "/path/to/qwen3-0.6b-q4_k_m.gguf",
  "refine_n_gpu_layers": 0  // 全部用 CPU
}
```

## 其他支持的模型

llama.cpp 支持多种模型架构，你可以尝试：

- **Qwen2.5-0.5B**: 更小更快
- **Llama-3.2-1B**: Meta 的小模型
- **Phi-3-mini**: 微软的 3.8B 模型（质量更高）

只需转换为 GGUF 格式即可使用。
