# 快速入门

本指南将帮助你快速开始使用 Recordian。

---

## 前置条件

确保已完成 [安装指南](Installation) 中的所有步骤。

---

## 启动方式

Recordian 提供三种启动模式：

### 1. 托盘 GUI 模式（推荐）

```bash
uv run recordian-tray
```

**特点**:
- 系统托盘图标
- 图形化设置界面
- 实时状态显示
- 适合日常使用

### 2. 热键模式

```bash
uv run recordian-hotkey
```

**特点**:
- 后台运行
- 热键触发录音
- 轻量级
- 适合键盘流用户

### 3. 命令行模式

```bash
uv run recordian-cli
```

**特点**:
- 命令行交互
- 脚本友好
- 适合自动化场景

---

## 基本使用

### 第一次录音

1. **启动应用**
   ```bash
   uv run recordian-tray
   ```

2. **触发录音**
   - 默认热键: `Ctrl+Space`
   - 或点击托盘图标选择"开始录音"

3. **说话**
   - 清晰地说出你想输入的内容
   - 支持中英文混合

4. **停止录音**
   - 再次按 `Ctrl+Space`
   - 或等待自动停止（静音检测）

5. **查看结果**
   - 识别结果会自动输入到当前光标位置
   - 或显示在通知中

---

## 配置热键

### 修改触发热键

1. 打开设置界面
2. 找到"热键设置"
3. 点击"触发热键"输入框
4. 按下你想要的组合键
5. 点击"保存"

**推荐热键**:
- `Ctrl+Space` (默认)
- `Alt+Space`
- `F1`
- `Ctrl+Shift+V`

### 修改退出热键

同样在设置界面中配置，用于快速退出应用。

---

## 选择 ASR 引擎

Recordian 支持多种 ASR 引擎：

### 本地引擎

**Sherpa-ONNX** (推荐):
- 完全离线
- 速度快
- 隐私保护

**配置**:
```json
{
  "asr": {
    "provider": "sherpa-onnx",
    "model_path": "~/.cache/recordian/models/sherpa-onnx"
  }
}
```

### 云端引擎

**阿里云 ASR**:
- 识别准确
- 支持方言
- 需要网络

**配置**:
```json
{
  "asr": {
    "provider": "aliyun",
    "app_key": "your_app_key",
    "access_key_id": "your_access_key_id",
    "access_key_secret": "your_access_key_secret"
  }
}
```

---

## 使用文本精炼

文本精炼可以自动修正识别错误、添加标点符号。

### 启用精炼

1. 打开设置界面
2. 找到"文本精炼"选项
3. 勾选"启用精炼"
4. 选择精炼引擎

### 精炼引擎选项

**本地 LLM**:
- 使用 llama.cpp
- 完全离线
- 需要下载模型

**云端 LLM**:
- OpenAI GPT
- Anthropic Claude
- 阿里云通义千问

### 配置示例

```json
{
  "refiner": {
    "provider": "openai",
    "api_key": "your_api_key",
    "model": "gpt-4",
    "base_url": "https://api.openai.com/v1"
  }
}
```

---

## 使用预设

预设可以为不同场景定制精炼提示词。

### 创建预设

1. 在 `~/.config/recordian/presets/` 创建 `.md` 文件
2. 编写提示词

**示例** (`email.md`):
```markdown
# 邮件写作预设

请将以下语音识别文本整理成正式的邮件格式：
- 添加适当的称呼和结尾
- 使用礼貌用语
- 修正语法错误
- 添加标点符号
```

### 使用预设

1. 打开设置界面
2. 在"预设管理"中选择预设
3. 点击"应用"

---

## 常用技巧

### 1. 提高识别准确度

- 使用高质量麦克风
- 在安静环境录音
- 说话清晰、语速适中
- 添加常用词到热词库

### 2. 快速输入

- 使用 PTT (按住说话) 模式
- 配置顺手的热键
- 启用自动提交

### 3. 处理专业术语

- 创建专业领域预设
- 添加术语到热词库
- 使用高精度模式

---

## 故障排查

### 录音没有反应

1. 检查麦克风权限
2. 确认热键没有冲突
3. 查看日志: `~/.local/share/recordian/recordian.log`

### 识别结果不准确

1. 检查麦克风质量
2. 尝试其他 ASR 引擎
3. 启用文本精炼

### 应用崩溃

1. 查看错误日志
2. 检查依赖是否完整
3. 提交 Issue 到 GitHub

---

## 下一步

- 阅读 [配置说明](Configuration) 了解详细配置
- 查看 [ASR 识别](ASR-Recognition) 了解识别原理
- 参考 [预设管理](Preset-Management) 创建自定义预设
- 访问 [常见问题](FAQ) 解决常见问题

---

**最后更新**: 2026-03-01
