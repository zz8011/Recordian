# llama.cpp (GGUF) 使用指南

本指南介绍如何在 Recordian 中使用 llama.cpp 进行文本精炼。

## 目录

- [简介](#简介)
- [安装配置](#安装配置)
- [模型下载](#模型下载)
- [配置文件](#配置文件)
- [Preset 使用](#preset-使用)
- [参数调优](#参数调优)
- [故障排查](#故障排查)
- [性能对比](#性能对比)

## 简介

llama.cpp 是一个高性能的 C++ 推理引擎，支持 GGUF 量化模型。相比 transformers 方案：

- ✅ **显存占用更低**：~600MB vs ~2GB
- ✅ **推理速度更快**：优化的 C++ 实现
- ✅ **模型文件更小**：~400MB（Q4_K_M 量化）
- ✅ **支持 CPU 和 GPU**：灵活的硬件支持

## 安装配置

### 1. 安装 llama-cpp-python

**GPU 版本（推荐）**：
```bash
CMAKE_ARGS="-DLLAMA_CUDA=on" pip install llama-cpp-python
```

**CPU 版本**：
```bash
pip install llama-cpp-python
```

### 2. 验证安装

```bash
python3 -c "from llama_cpp import Llama; print('安装成功')"
```

## 模型下载

### 推荐模型

**Qwen3-0.6B-Q4_K_M**（推荐）：
- 大小：~400MB
- 显存：~600MB
- 速度：很快
- 效果：优秀

下载地址：
```bash
# 使用 huggingface-cli
huggingface-cli download unsloth/Qwen3-0.6B-GGUF \
  Qwen3-0.6B-Q4_K_M.gguf \
  --local-dir ./models/Qwen3-0.6B-GGUF
```

### 其他可选模型

| 模型 | 大小 | 显存 | 速度 | 效果 |
|------|------|------|------|------|
| Qwen3-0.6B-Q4_K_M | ~400MB | ~600MB | 很快 | 优秀 |
| Qwen3-0.6B-Q8_0 | ~600MB | ~800MB | 快 | 更好 |
| Qwen3-1.5B-Q4_K_M | ~900MB | ~1.2GB | 中等 | 优秀 |

## 配置文件

编辑 `~/.config/recordian/hotkey.json`：

```json
{
  "refine_provider": "llamacpp",
  "refine_model": "/path/to/Qwen3-0.6B-Q4_K_M.gguf",
  "refine_n_gpu_layers": -1,
  "refine_preset": "default"
}
```

### 配置参数说明

- `refine_provider`: 设置为 `"llamacpp"`
- `refine_model`: GGUF 模型文件的绝对路径
- `refine_n_gpu_layers`: GPU 层数
  - `-1`: 全部使用 GPU（推荐）
  - `0`: 仅使用 CPU
  - `20`: 使用 20 层 GPU，其余 CPU
- `refine_preset`: 预设名称（default/formal/technical/meeting）

## Preset 使用

Recordian 支持 4 种文本精炼 preset，每种都针对 GGUF 模型优化了 Few-shot prompt。

### 1. default（默认）

**功能**：去除重复词和语气助词

**适用场景**：日常语音输入、快速记录

**示例**：
```
输入：嗯，打开打开浏览器，然后呃进入主页
输出：打开浏览器进入主页
```

**配置**：
```json
{
  "refine_preset": "default"
}
```

### 2. formal（正式书面语）

**功能**：转换为正式书面语

**适用场景**：邮件、报告、正式文档

**示例**：
```
输入：嗯，这个方案我觉得还不错，可以试试看
输出：该方案具有可行性，建议实施。
```

**配置**：
```json
{
  "refine_preset": "formal"
}
```

### 3. technical（技术文档）

**功能**：整理为技术文档风格

**适用场景**：技术文档、代码注释、技术博客

**示例**：
```
输入：这个函数就是用来处理数据的
输出：该函数用于数据处理，将输入数据转换为输出格式。
```

**配置**：
```json
{
  "refine_preset": "technical"
}
```

### 4. meeting（会议纪要）

**功能**：整理为会议纪要格式

**适用场景**：会议记录、任务列表

**示例**：
```
输入：我们今天讨论了项目进度，张三负责前端开发
输出：- 讨论项目进度
- 张三负责前端开发
```

**配置**：
```json
{
  "refine_preset": "meeting"
}
```

## 参数调优

### 推理参数（已优化）

当前默认参数已经过优化，通常无需修改：

```python
{
  "temperature": 0.1,        # 稍微增加随机性
  "repeat_penalty": 1.2,     # 避免重复
  "top_p": 0.9,              # 核采样
  "max_tokens": len(text) * 2 + 50,  # 足够的输出空间
  "stop": ["\n\n", "输入：", "<think>", "<|"]  # 停止词
}
```

### 高级调优

如果需要自定义参数，可以修改 `src/recordian/providers/llamacpp_text_refiner.py`：

**增加输出长度**：
```python
max_tokens=min(self.max_new_tokens, len(text) * 3 + 100)
```

**更确定性的输出**：
```python
temperature=0.0  # 完全确定性
```

**减少重复**：
```python
repeat_penalty=1.5  # 更强的重复惩罚
```

## 故障排查

### 问题 1: 输出 `<think>` 或无关内容

**症状**：
```
输入：你好世界
输出：<think>我需要...
```

**原因**：模型使用了错误的 prompt 格式

**解决**：
1. 确认使用的是 Few-shot prompt（已在代码中实现）
2. 检查是否正确加载了最新代码
3. 重启后端：`pkill -f recordian`

### 问题 2: 输出过短或被截断

**症状**：
```
输入：打开浏览器进入主页
输出：打开
```

**原因**：停止词设置不当或 max_tokens 太小

**解决**：
1. 检查停止词是否包含单个 `\n`（应该使用 `\n\n`）
2. 增加 max_tokens 限制
3. 检查模型是否正确加载

### 问题 3: 输出重复或啰嗦

**症状**：
```
输入：你好世界
输出：你好世界你好世界你好世界
```

**原因**：repeat_penalty 太低

**解决**：
1. 增加 repeat_penalty 到 1.3-1.5
2. 降低 temperature 到 0.0
3. 检查 Few-shot 示例是否正确

### 问题 4: 显存不足

**症状**：
```
CUDA out of memory
```

**解决**：
1. 减少 GPU 层数：`"refine_n_gpu_layers": 20`
2. 使用更小的量化模型（Q4_K_M → Q2_K）
3. 使用 CPU 模式：`"refine_n_gpu_layers": 0`

### 问题 5: 推理速度慢

**原因**：使用 CPU 模式或 GPU 层数不足

**解决**：
1. 确认 GPU 可用：`nvidia-smi`
2. 设置全 GPU：`"refine_n_gpu_layers": -1`
3. 检查 CUDA 是否正确安装

## 性能对比

### 显存占用

| 方案 | 模型 | 显存占用 |
|------|------|---------|
| transformers | Qwen3-0.6B | ~2GB |
| llama.cpp | Qwen3-0.6B-Q4_K_M | ~600MB |
| llama.cpp | Qwen3-0.6B-Q8_0 | ~800MB |

### 推理速度

测试环境：RTX 4090

| 方案 | 模型 | 速度 (tokens/s) |
|------|------|----------------|
| transformers | Qwen3-0.6B | ~150 |
| llama.cpp | Qwen3-0.6B-Q4_K_M | ~200 |
| llama.cpp | Qwen3-0.6B-Q8_0 | ~180 |

### 模型文件大小

| 模型 | 大小 |
|------|------|
| Qwen3-0.6B (transformers) | ~1.2GB |
| Qwen3-0.6B-Q4_K_M (GGUF) | ~400MB |
| Qwen3-0.6B-Q8_0 (GGUF) | ~600MB |

### 输出质量

| 方案 | 质量 | 备注 |
|------|------|------|
| transformers | ⭐⭐⭐⭐⭐ | 最佳 |
| llama.cpp Q8_0 | ⭐⭐⭐⭐⭐ | 接近 transformers |
| llama.cpp Q4_K_M | ⭐⭐⭐⭐ | 优秀 |

## 技术细节

### Few-shot Prompt 机制

GGUF 量化模型**必须使用 Few-shot prompt**，不能使用复杂的 Chat Template。

**正确示例**（Few-shot）：
```
整理语音识别文本：
输入：嗯这个这个那个我觉得可以
输出：这个那个我觉得可以
输入：打开打开浏览器然后呃进入主页
输出：打开浏览器进入主页
输入：{text}
输出：
```

**错误示例**（Chat Template）：
```
<|im_start|>system
你是一个文本整理助手。
<|im_end|>
<|im_start|>user
整理：{text}
<|im_end|>
<|im_start|>assistant
```

### 停止词设置

正确的停止词设置：
```python
stop=["\n\n", "输入：", "<think>", "<|"]
```

**注意**：
- 使用 `\n\n` 而不是 `\n`（避免输出过短）
- 包含 `<think>` 防止模型输出思考过程
- 包含 `<|` 防止输出特殊标记

### 参数优化原理

- **temperature=0.1**：稍微增加随机性，避免输出过于死板
- **repeat_penalty=1.2**：适度惩罚重复，不影响正常输出
- **top_p=0.9**：核采样，保持输出多样性
- **max_tokens=len(text)*2+50**：足够的输出空间，避免截断

## 总结

llama.cpp (GGUF) 方案的关键优势：

1. ✅ **低显存占用**：~600MB，适合资源受限环境
2. ✅ **高推理速度**：优化的 C++ 实现
3. ✅ **小模型文件**：~400MB，易于分发
4. ✅ **灵活部署**：支持 CPU/GPU 混合
5. ✅ **多种 preset**：适应不同使用场景

**推荐配置**：
- 模型：Qwen3-0.6B-Q4_K_M
- GPU 层数：-1（全 GPU）
- Preset：根据场景选择

**适用场景**：
- 个人电脑（显存 < 4GB）
- 边缘设备
- 需要快速响应的场景
- 需要离线运行的场景
