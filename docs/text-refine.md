# Qwen3 文本精炼功能

## 功能说明

在 ASR 识别后，使用 Qwen3-0.6B 模型对文本进行后处理：
- 去除重复的词语和句子
- 去除语气助词（嗯、啊、呃、那个、这个、然后等）
- 添加正确的标点符号
- 保持原意，使文本通顺易读

## 使用方法

### 1. 下载模型

```bash
# 方式 1: 使用下载脚本（推荐）
./scripts/download_qwen3_0.6b.sh

# 方式 2: 手动使用 git clone
cd models
git clone https://www.modelscope.cn/Qwen/Qwen3-0.6B.git

# 方式 3: 使用 modelscope CLI
pip install modelscope
modelscope download --model Qwen/Qwen3-0.6B --local_dir ./models/Qwen3-0.6B
```

### 2. 测试功能

```bash
# 测试文本精炼器
./scripts/test_text_refiner.py
```

### 3. 启用文本精炼

在配置文件中启用（`~/.config/recordian/hotkey.json`）：

```json
{
  "enable_text_refine": true,
  "refine_model": "/home/zz8011/文档/Develop/Recordian/models/Qwen3-0.6B",
  "refine_device": "cuda",
  "refine_max_tokens": 512
}
```

或使用命令行参数：

```bash
recordian-hotkey-dictate \
  --enable-text-refine \
  --refine-model ./models/Qwen3-0.6B \
  --refine-device cuda
```

### 4. 保存配置

```bash
recordian-hotkey-dictate --enable-text-refine --save-config
```

## 性能指标

- **模型大小**: ~1.2GB (FP16)
- **显存占用**: ~1.5GB
- **推理速度**: ~50-100 tokens/s (RTX 4070)
- **延迟**: 通常 100-500ms（取决于文本长度）

## 架构

```
录音 → ASR (Qwen3-ASR) → 文本精炼 (Qwen3-0.6B) → 上屏
                ↓                    ↓
           原始文本            精炼后文本
```

## 示例

**原文**: "嗯那个我想说的就是这个这个项目呢需要添加一个新的功能然后就是可以去除重复的内容"

**精炼后**: "我想说的是，这个项目需要添加一个新功能，可以去除重复的内容。"

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enable_text_refine` | `false` | 是否启用文本精炼 |
| `refine_model` | `Qwen/Qwen3-0.6B` | 模型路径或名称 |
| `refine_device` | `cuda` | 运行设备 |
| `refine_max_tokens` | `512` | 最大生成 token 数 |

## 注意事项

- 文本精炼会增加 100-500ms 延迟
- 需要额外 ~1.5GB 显存
- 如果显存不足，可以设置 `refine_device: "cpu"`（会更慢）
- 可以随时通过配置文件开关此功能
