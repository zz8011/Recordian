# Recordian 用户手册

## 目录

1. [简介](#简介)
2. [安装](#安装)
3. [快速开始](#快速开始)
4. [配置](#配置)
5. [功能介绍](#功能介绍)
6. [故障排查](#故障排查)
7. [常见问题](#常见问题)

---

## 简介

Recordian 是一个智能语音输入工具，提供：

- 🎤 **语音识别（ASR）**: 将语音转换为文本
- ✨ **文本精炼**: 使用 LLM 优化识别结果
- ⚡ **快捷键支持**: 快速启动语音输入
- 🔧 **灵活配置**: 支持多种 ASR 和 LLM 提供商

---

## 安装

### 系统要求

- **操作系统**: Linux (Ubuntu 20.04+)
- **Python**: 3.10+
- **依赖**: PortAudio, GTK3

### 安装步骤

#### 1. 安装系统依赖

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y \
    python3-dev \
    portaudio19-dev \
    libgtk-3-dev \
    libappindicator3-dev
```

#### 2. 克隆仓库

```bash
git clone https://github.com/zz8011/Recordian.git
cd Recordian
```

#### 3. 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
```

#### 4. 安装 Python 依赖

```bash
pip install -e .
```

#### 5. 验证安装

```bash
recordian --version
```

---

## 快速开始

### 1. 创建配置文件

```bash
mkdir -p ~/.config/recordian
cat > ~/.config/recordian/config.json << EOF
{
  "version": "1.0",
  "policy": {
    "confidence_threshold": 0.88,
    "english_ratio_threshold": 0.15,
    "pass2_timeout_ms_local": 900,
    "pass2_timeout_ms_cloud": 1500
  }
}
EOF
```

### 2. 启动托盘应用

```bash
recordian-tray
```

### 3. 使用快捷键

- **默认快捷键**: `Ctrl+Alt+V`
- 按下快捷键开始录音
- 再次按下停止录音
- 识别结果自动输入到当前应用

---

## 配置

### 配置文件位置

默认配置文件：`~/.config/recordian/config.json`

### 配置结构

```json
{
  "version": "1.0",
  "policy": {
    "confidence_threshold": 0.88,
    "english_ratio_threshold": 0.15,
    "pass2_timeout_ms_local": 900,
    "pass2_timeout_ms_cloud": 1500
  },
  "hotkey": "<ctrl>+<alt>+v",
  "asr_provider": "qwen",
  "refiner_provider": "qwen"
}
```

### 配置选项说明

#### policy 配置

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `confidence_threshold` | float | 0.88 | 置信度阈值（0.0-1.0） |
| `english_ratio_threshold` | float | 0.15 | 英文比例阈值（0.0-1.0） |
| `pass2_timeout_ms_local` | int | 900 | 本地精炼超时（毫秒） |
| `pass2_timeout_ms_cloud` | int | 1500 | 云端精炼超时（毫秒） |

#### 其他配置

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `hotkey` | string | `<ctrl>+<alt>+v` | 快捷键 |
| `asr_provider` | string | `qwen` | ASR 提供商 |
| `refiner_provider` | string | `qwen` | 精炼器提供商 |

### 修改配置

#### 方法 1: 直接编辑配置文件

```bash
nano ~/.config/recordian/config.json
```

#### 方法 2: 使用 Python API

```python
from recordian.config import ConfigManager

# 加载配置
config = ConfigManager.load("~/.config/recordian/config.json")

# 修改配置
config["policy"]["confidence_threshold"] = 0.9

# 保存配置（自动备份）
ConfigManager.save("~/.config/recordian/config.json", config)
```

### 配置备份

配置文件在保存时会自动备份：

- 备份位置：`~/.config/recordian/config.backup.YYYYMMDD_HHMMSS.json`
- 默认保留最近 5 个备份
- 自动清理旧备份

---

## 功能介绍

### 1. 语音识别（ASR）

#### 支持的 ASR 提供商

- **Qwen ASR**: 阿里云通义千问 ASR
- **本地 ASR**: 本地语音识别模型

#### 使用方法

1. 按下快捷键开始录音
2. 说话（支持中英文混合）
3. 再次按下快捷键停止录音
4. 等待识别结果

#### 热词支持

在配置中添加热词以提高识别准确率：

```json
{
  "hotwords": ["Recordian", "语音输入", "专业术语"]
}
```

#### 常用词与自动词库（推荐）

Recordian 支持两层词增强：

1. **手动常用词**：在托盘菜单 `常用词管理...` 中维护 `asr_context`。
2. **自动词库**：系统会从已成功上屏的文本中自动学习高频词，并在识别时自动注入热词。

常用配置示例（`~/.config/recordian/hotkey.json`）：

```json
{
  "asr_context": "Recordian, Claude, openclaw",
  "enable_auto_lexicon": true,
  "auto_lexicon_db": "~/.config/recordian/auto_lexicon.db",
  "auto_lexicon_max_hotwords": 40,
  "auto_lexicon_min_accepts": 2,
  "auto_lexicon_max_terms": 5000
}
```

#### ASR Context 预设注意事项

- `asr_context_preset` 仅用于 `asr-*.md` 的 ASR 预设。
- 如果你没有自建 ASR 预设，建议保持 `asr_context_preset` 为空字符串 `""`。
- 文本精炼使用的 `refine_preset`（如 `default`）与 ASR 预设是两套系统，不冲突。

#### 自动词库数据库导入/导出

在托盘菜单 `常用词管理...` 窗口中，使用：

- `导出数据库...`：备份当前自动词库数据库
- `导入数据库...`：恢复或迁移自动词库数据库

导入后建议重启后端，以立即刷新内存中的词库缓存。

### 2. 文本精炼

#### 精炼策略

根据识别结果的置信度和英文比例，自动决定是否进行精炼：

- **高置信度 + 低英文比例**: 直接输出
- **低置信度 或 高英文比例**: 使用 LLM 精炼

#### 精炼超时

- 本地模型：900ms
- 云端 API：1500ms

超时后使用原始识别结果。

### 3. 预设管理

#### 预设目录

预设文件位于：`presets/`

#### 创建预设

1. 在 `presets/` 目录创建 `.md` 文件
2. 第一行为标题（可选，会被忽略）
3. 其余内容为 prompt

示例 `presets/custom.md`：

```markdown
# 自定义预设

请将以下文本优化为正式的商务邮件格式。
```

#### 使用预设

```python
from recordian.preset_manager import PresetManager

manager = PresetManager()
prompt = manager.load_preset("custom")
```

### 4. 快捷键

#### 修改快捷键

编辑配置文件：

```json
{
  "hotkey": "<ctrl>+<shift>+v"
}
```

支持的修饰键：
- `<ctrl>`: Ctrl
- `<alt>`: Alt
- `<shift>`: Shift
- `<super>`: Super/Win

---

## 故障排查

### 问题 1: 无法启动托盘应用

**症状**: 运行 `recordian-tray` 报错

**解决方法**:

1. 检查系统依赖：
   ```bash
   sudo apt-get install libgtk-3-dev libappindicator3-dev
   ```

2. 检查 Python 版本：
   ```bash
   python3 --version  # 应该 >= 3.10
   ```

3. 重新安装：
   ```bash
   pip install -e . --force-reinstall
   ```

### 问题 2: 快捷键不响应

**症状**: 按下快捷键没有反应

**解决方法**:

1. 检查快捷键是否被其他应用占用
2. 尝试修改快捷键
3. 检查日志：
   ```bash
   tail -f ~/.local/share/recordian/logs/recordian.log
   ```

### 问题 3: 识别结果不准确

**症状**: 语音识别错误率高

**解决方法**:

1. 添加热词：
   ```json
   {
     "hotwords": ["常用词", "专业术语"]
   }
   ```

2. 调整置信度阈值：
   ```json
   {
     "policy": {
       "confidence_threshold": 0.85
     }
   }
   ```

3. 确保录音环境安静
4. 说话清晰，语速适中

### 问题 4: 配置文件损坏

**症状**: 加载配置时报错

**解决方法**:

1. 恢复备份：
   ```bash
   cd ~/.config/recordian
   ls -lt config.backup.*.json  # 查看备份
   cp config.backup.20240101_120000.json config.json
   ```

2. 或创建新配置：
   ```bash
   rm config.json
   recordian-tray  # 会自动创建默认配置
   ```

### 问题 5: 内存占用过高

**症状**: 应用占用内存过多

**解决方法**:

1. 清除预设缓存：
   ```python
   from recordian.preset_manager import PresetManager
   manager = PresetManager()
   manager.clear_cache()
   ```

2. 重启应用

---

## 常见问题

### Q1: Recordian 支持哪些语言？

A: 目前主要支持中文和英文，以及中英文混合输入。

### Q2: 可以离线使用吗？

A: 部分功能可以离线使用（本地 ASR 和本地 LLM），但云端 API 需要网络连接。

### Q3: 如何提高识别准确率？

A:
1. 使用热词功能
2. 确保录音环境安静
3. 说话清晰，语速适中
4. 调整置信度阈值

### Q4: 配置文件在哪里？

A: 默认位置：`~/.config/recordian/config.json`

### Q5: 如何查看日志？

A: 日志位置：`~/.local/share/recordian/logs/recordian.log`

```bash
tail -f ~/.local/share/recordian/logs/recordian.log
```

### Q6: 支持自定义 ASR 提供商吗？

A: 支持。参考 [开发者指南](DEVELOPER_GUIDE.md) 实现自定义 Provider。

### Q7: 如何卸载？

A:
```bash
pip uninstall recordian
rm -rf ~/.config/recordian
rm -rf ~/.local/share/recordian
```

### Q8: 性能如何？

A: 使用性能基准测试工具测试：

```python
from recordian.performance_benchmark import PerformanceBenchmark

benchmark = PerformanceBenchmark()
# 运行测试...
benchmark.print_summary()
```

---

## 获取帮助

- **GitHub Issues**: https://github.com/zz8011/Recordian/issues
- **文档**: https://github.com/zz8011/Recordian/tree/master/docs
- **API 文档**: [API.md](API.md)
- **开发者指南**: [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)

---

## 更新日志

查看 [CHANGELOG.md](../CHANGELOG.md) 了解版本更新信息。

---

**版本**: 1.0
**最后更新**: 2024年
