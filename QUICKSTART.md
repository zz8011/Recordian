# Recordian 快速开始指南

5 分钟快速上手 Recordian 语音输入助手。

## 📋 前置要求

- Ubuntu 20.04+ / Debian 11+
- Python 3.10+
- NVIDIA GPU（推荐，可选）

## 🚀 快速安装

### 步骤 1：下载并解压

```bash
# 下载 release
wget https://github.com/yourusername/recordian/releases/download/v0.1.0/recordian-0.1.0.tar.gz

# 解压
tar xzf recordian-0.1.0.tar.gz
cd recordian-0.1.0
```

### 步骤 2：运行安装脚本

```bash
./install.sh
```

安装脚本会自动：
- 创建 Python 虚拟环境
- 安装所有依赖
- 创建桌面快捷方式
- 配置系统服务

### 步骤 3：下载模型

**方案 A：使用 llama.cpp（推荐，低显存）**

```bash
# 安装 huggingface-cli
pip install huggingface-hub

# 下载 GGUF 模型（~400MB）
huggingface-cli download unsloth/Qwen3-0.6B-GGUF \
  Qwen3-0.6B-Q4_K_M.gguf \
  --local-dir ~/.local/share/recordian/models/Qwen3-0.6B-GGUF
```

**方案 B：使用 transformers（更高质量）**

```bash
# 下载 transformers 模型（~1.2GB）
huggingface-cli download Qwen/Qwen3-0.6B \
  --local-dir ~/.local/share/recordian/models/Qwen3-0.6B
```

### 步骤 4：配置

编辑配置文件：

```bash
nano ~/.config/recordian/hotkey.json
```

**llama.cpp 配置**：
```json
{
  "asr_model": "Qwen/Qwen3-ASR-1.7B",
  "refine_provider": "llamacpp",
  "refine_model": "/home/yourusername/.local/share/recordian/models/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q4_K_M.gguf",
  "refine_n_gpu_layers": -1,
  "refine_preset": "default",
  "hotkey": "ctrl_r"
}
```

**transformers 配置**：
```json
{
  "asr_model": "Qwen/Qwen3-ASR-1.7B",
  "refine_provider": "local",
  "refine_model": "Qwen/Qwen3-0.6B",
  "refine_preset": "default",
  "hotkey": "ctrl_r"
}
```

### 步骤 5：启动

从应用菜单启动 Recordian，或运行：

```bash
recordian-hotkey
```

## 🎯 使用方法

### 基本使用

1. **按下右 Ctrl 键**开始录音
2. **说话**（例如："打开浏览器"）
3. **松开右 Ctrl 键**结束录音
4. 等待识别和精炼
5. 文本自动输入到当前光标位置

### 切换 Preset

按 `Ctrl+Shift+P` 快速切换 preset：

- **default**：日常使用，去除重复词
- **formal**：正式书面语
- **technical**：技术文档
- **meeting**：会议纪要

### 系统托盘

右键点击托盘图标：
- **切换 Preset**：快速切换文本风格
- **重新加载配置**：应用新配置
- **退出**：关闭程序

## 📊 性能对比

| 方案 | 显存 | 速度 | 质量 | 推荐场景 |
|------|------|------|------|---------|
| llama.cpp | ~600MB | 很快 | 优秀 | 日常使用、低显存 |
| transformers | ~2GB | 快 | 最佳 | 高质量要求 |

## 🔧 常见问题

### Q1: 按下热键没有反应？

**检查**：
```bash
# 查看日志
journalctl --user -u recordian-hotkey -f

# 或查看文件日志
tail -f ~/.local/share/recordian/logs/hotkey.log
```

**可能原因**：
- 模型未下载
- 配置文件路径错误
- 权限问题

### Q2: 识别速度慢？

**优化方法**：
1. 使用 GPU 模式：`"refine_n_gpu_layers": -1`
2. 使用 llama.cpp 而不是 transformers
3. 使用更小的模型（Q4_K_M）

### Q3: 输出质量不好？

**调整方法**：
1. 切换到 transformers 方案
2. 使用更大的量化模型（Q8_0）
3. 调整 preset（formal/technical）

### Q4: CUDA out of memory？

**解决方法**：
1. 使用 llama.cpp：`"refine_provider": "llamacpp"`
2. 减少 GPU 层数：`"refine_n_gpu_layers": 20`
3. 使用 CPU 模式：`"refine_n_gpu_layers": 0`

### Q5: 如何卸载？

```bash
cd recordian-0.1.0
./uninstall.sh
```

## 📚 进阶使用

### 自定义 Preset

创建自定义 preset：

```bash
nano ~/.config/recordian/presets/custom.md
```

内容示例：
```markdown
# 自定义预设

将以下口语整理为你想要的格式：
- 规则 1
- 规则 2

原文：{text}
```

使用：
```json
{
  "refine_preset": "custom"
}
```

### 调整推理参数

编辑 `src/recordian/providers/llamacpp_text_refiner.py`：

```python
result = self.llm(
    prompt,
    max_tokens=100,        # 调整最大输出长度
    temperature=0.0,       # 调整随机性（0.0-1.0）
    repeat_penalty=1.5,    # 调整重复惩罚（1.0-2.0）
    top_p=0.9,            # 调整核采样（0.0-1.0）
)
```

### 多语言支持

Qwen3 模型支持中英文混合输入：

```
输入：打开 browser 然后进入 homepage
输出：打开浏览器进入主页
```

## 🎓 学习资源

- [完整文档](README.md)
- [llama.cpp 指南](docs/LLAMACPP_GUIDE.md)
- [Preset 说明](presets/README.md)
- [更新日志](CHANGELOG.md)

## 💡 使用技巧

### 技巧 1：快速切换 Preset

为不同场景设置快捷键：
- 日常：`Ctrl+Shift+1` → default
- 邮件：`Ctrl+Shift+2` → formal
- 代码：`Ctrl+Shift+3` → technical

### 技巧 2：批量处理

使用命令行工具批量处理文本：

```bash
echo "你好你好世界世界" | recordian-refine --preset default
```

### 技巧 3：集成到编辑器

在 VS Code 中使用：
1. 安装 "Run on Save" 插件
2. 配置自动精炼注释

## 🆘 获取帮助

- **文档**：查看 `docs/` 目录
- **Issue**：https://github.com/yourusername/recordian/issues
- **邮件**：your.email@example.com

## 🎉 开始使用

现在你已经准备好了！按下右 Ctrl 键，开始你的语音输入之旅吧！

---

**提示**：首次使用时，模型加载需要 5-10 秒，请耐心等待。
