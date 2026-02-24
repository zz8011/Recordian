# Recordian v0.1.0 Release Notes

发布日期：2025-02-24

## 🎉 主要更新

### 新增 llama.cpp (GGUF) 支持

本版本新增了对 llama.cpp 量化模型的支持，带来显著的性能提升和资源优化：

- ✅ **显存占用降低 70%**：从 ~2GB 降至 ~600MB
- ✅ **推理速度提升**：优化的 C++ 实现
- ✅ **模型文件缩小 67%**：从 ~1.2GB 降至 ~400MB
- ✅ **支持 4 种 preset**：default、formal、technical、meeting

### 核心特性

#### 1. Few-shot Prompt 机制
专为 GGUF 量化模型设计的 Few-shot prompt，确保高质量输出：

```
整理语音识别文本：
输入：嗯这个这个那个我觉得可以
输出：这个那个我觉得可以
输入：打开打开浏览器然后呃进入主页
输出：打开浏览器进入主页
输入：{text}
输出：
```

#### 2. 多种文本精炼 Preset

**default（默认）**：
- 输入：`嗯，打开打开浏览器，然后进入主页`
- 输出：`打开浏览器进入主页`

**formal（正式书面语）**：
- 输入：`嗯，这个方案我觉得还不错，可以试试看`
- 输出：`该方案具有可行性，建议实施。`

**technical（技术文档）**：
- 输入：`这个函数就是用来处理数据的`
- 输出：`该函数用于数据处理，将输入数据转换为输出格式。`

**meeting（会议纪要）**：
- 输入：`我们今天讨论了项目进度，张三负责前端`
- 输出：`- 讨论项目进度\n- 张三负责前端开发`

#### 3. 优化的推理参数

- `temperature`: 0.1（平衡确定性和灵活性）
- `repeat_penalty`: 1.2（避免重复）
- `top_p`: 0.9（核采样）
- `stop`: `["\n\n", "输入：", "<think>", "<|"]`

## 📦 安装方法

### 1. 下载 Release

```bash
wget https://github.com/yourusername/recordian/releases/download/v0.1.0/recordian-0.1.0.tar.gz
tar xzf recordian-0.1.0.tar.gz
cd recordian-0.1.0
```

### 2. 运行安装脚本

```bash
./install.sh
```

### 3. 下载模型

**推荐模型：Qwen3-0.6B-Q4_K_M**

```bash
# 安装 huggingface-cli
pip install huggingface-hub

# 下载模型
huggingface-cli download unsloth/Qwen3-0.6B-GGUF \
  Qwen3-0.6B-Q4_K_M.gguf \
  --local-dir ~/.local/share/recordian/models/Qwen3-0.6B-GGUF
```

### 4. 配置

编辑 `~/.config/recordian/hotkey.json`：

```json
{
  "refine_provider": "llamacpp",
  "refine_model": "/home/yourusername/.local/share/recordian/models/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q4_K_M.gguf",
  "refine_n_gpu_layers": -1,
  "refine_preset": "default"
}
```

## 📚 文档

- [安装指南](INSTALL.md)
- [llama.cpp 使用指南](docs/LLAMACPP_GUIDE.md)
- [Preset 说明](presets/README.md)
- [更新日志](CHANGELOG.md)

## 🔧 技术细节

### 支持的模型

| 模型 | 大小 | 显存 | 速度 | 效果 |
|------|------|------|------|------|
| Qwen3-0.6B-Q4_K_M | ~400MB | ~600MB | 很快 | 优秀 |
| Qwen3-0.6B-Q8_0 | ~600MB | ~800MB | 快 | 更好 |
| Qwen3-1.5B-Q4_K_M | ~900MB | ~1.2GB | 中等 | 优秀 |

### 系统要求

**最低配置**：
- CPU：4 核
- 内存：4GB
- 显存：2GB（GPU 模式）或 0GB（CPU 模式）
- 存储：2GB

**推荐配置**：
- CPU：8 核
- 内存：8GB
- 显存：4GB（GPU 模式）
- 存储：5GB

### 依赖项

- Python 3.10+
- PyTorch 2.0+
- transformers
- llama-cpp-python（GPU 版本需要 CUDA）
- 其他依赖见 `pyproject.toml`

## 🐛 已知问题

1. **首次启动较慢**：模型加载需要 5-10 秒
2. **CUDA 版本要求**：需要 CUDA 11.8+
3. **部分 GPU 不支持**：需要 Compute Capability 6.0+

## 🔄 从旧版本升级

如果你使用的是 v0.0.1，请按以下步骤升级：

1. 备份配置文件：
```bash
cp ~/.config/recordian/hotkey.json ~/.config/recordian/hotkey.json.bak
```

2. 卸载旧版本：
```bash
./uninstall.sh
```

3. 安装新版本：
```bash
cd recordian-0.1.0
./install.sh
```

4. 恢复配置（如需要）：
```bash
cp ~/.config/recordian/hotkey.json.bak ~/.config/recordian/hotkey.json
```

## 🙏 致谢

感谢以下项目和贡献者：

- [llama.cpp](https://github.com/ggerganov/llama.cpp) - 高性能推理引擎
- [Qwen](https://github.com/QwenLM/Qwen) - 优秀的开源模型
- [transformers](https://github.com/huggingface/transformers) - 模型加载框架

## 📝 反馈

如有问题或建议，请：
- 提交 Issue：https://github.com/yourusername/recordian/issues
- 发送邮件：your.email@example.com

## 📄 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE) 文件。
