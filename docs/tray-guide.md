# 托盘功能使用指南

## 🎯 功能概述

Recordian 托盘应用提供系统托盘常驻功能，支持状态图标切换和完整的配置管理。

## 🚀 启动托盘

```bash
source .venv/bin/activate
recordian-tray
```

或使用快捷脚本：

```bash
./scripts/run_recordian_assistant.sh
```

## 🎨 状态图标

托盘图标会根据当前状态自动切换：

| 状态 | 图标 | 颜色 | 说明 |
|------|------|------|------|
| **空闲** | logo.svg | 青绿色 | 等待录音触发 |
| **录音中** | logo-recording.svg | 青蓝色 | 正在录音，带脉动动画 |
| **识别中** | logo-recording.svg | 青蓝色 | 正在识别处理 |
| **错误** | logo-error.svg | 红色 | 发生错误 |
| **已停止** | logo.svg | 青绿色 | 后端已停止 |

## 📋 托盘菜单

### 菜单访问方式

托盘菜单的访问方式取决于你的系统环境：

#### 方式 1：右键菜单（支持右键的系统）
右键点击托盘图标，显示完整菜单：
- **Status: xxx** - 显示当前状态（不可点击）
- **Start Backend** - 启动后端服务
- **Stop Backend** - 停止后端服务
- **Settings...** - 打开设置面板（默认操作）
- **Preview Orb** - 预览波纹动画效果
- **Quit** - 退出托盘应用

#### 方式 2：左键菜单（X11/不支持右键的系统）
**如果右键菜单不显示，请尝试左键点击托盘图标**，会弹出简化菜单：
- **Settings...** - 打开设置面板
- **Quit** - 退出托盘应用

> **提示**：大多数 X11 系统会使用方式 2（左键点击）。如果你的系统是 X11 并且右键无反应，这是正常的，请使用左键点击。

## ⚙️ 设置面板

点击 "Settings..." 打开完整的配置面板，包含以下设置：

### 1. 热键设置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| 触发热键 | 开始录音的热键 | `<ctrl_r>` |
| 停止热键 | Toggle 模式的停止键 | 空 |
| 切换热键 | Toggle 模式的切换键 | 空 |
| 退出热键 | 退出程序的热键 | `<ctrl>+<alt>+q` |
| 触发模式 | ptt 或 toggle | `ptt` |
| 冷却时间 | 防止连击的冷却时间（毫秒） | `300` |

### 2. 录音设置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| 录音时长 | 单次录音时长（秒） | `4.0` |
| 录音后端 | auto/ffmpeg/arecord | `auto` |
| 录音格式 | ogg/wav/mp3 | `ogg` |
| 采样率 | 音频采样率 | `16000` |
| 声道数 | 音频声道数 | `1` |
| 输入设备 | 麦克风设备名称 | `default` |

### 3. ASR 设置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| ASR Provider | qwen-asr/funasr | `qwen-asr` |
| Qwen ASR 模型路径 | 本地模型路径 | 空 |
| Qwen 语言 | Chinese/auto | `Chinese` |
| Qwen Max Tokens | 最大生成 token 数 | `1024` |
| 计算设备 | cpu/cuda/auto | `cuda` |

### 4. 文本精炼设置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| 启用文本精炼 | true/false | `false` |
| 精炼 Provider | local/cloud | `local` |
| 精炼预设 | default/formal/summary/meeting/technical | `default` |
| 本地精炼模型路径 | Qwen3-0.6B 模型路径 | 空 |
| 精炼设备 | cpu/cuda | `cuda` |
| 精炼 Max Tokens | 最大生成 token 数 | `512` |
| 启用 Thinking 模式 | 允许模型输出思考过程 | `false` |
| 云端 API Base | API 端点 URL | 空 |
| 云端 API Key | API 密钥 | 空 |
| 云端 API 模型 | 模型名称 | 空 |

### 5. 上屏设置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| 上屏后端 | auto/wtype/xdotool/pynput | `auto` |

### 6. 其他设置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| 启动时预热 | 启动时加载模型 | `true` |
| 调试诊断 | 输出详细日志 | `false` |
| 通知后端 | auto/notify-send/none | `auto` |

## 💾 保存配置

1. 在设置面板中修改参数
2. 点击 **"保存并重启"** 按钮
3. 配置会保存到 `~/.config/recordian/hotkey.json`
4. 后端服务会自动重启以应用新配置

## 🔧 技术细节

### SVG Logo 加载

托盘应用使用 `cairosvg` 库将 SVG 转换为 PNG 图标：

```python
import cairosvg
from PIL import Image
import io

png_data = cairosvg.svg2png(url=str(svg_path), output_width=64, output_height=64)
image = Image.open(io.BytesIO(png_data))
```

如果 `cairosvg` 未安装，会自动回退到简单的圆形图标。

### 状态映射

```python
logo_map = {
    "idle": "logo.svg",
    "recording": "logo-recording.svg",
    "processing": "logo-recording.svg",
    "error": "logo-error.svg",
    "stopped": "logo.svg",
}
```

### 配置文件位置

默认配置文件：`~/.config/recordian/hotkey.json`

可通过 `--config-path` 参数指定其他位置：

```bash
recordian-tray --config-path /path/to/config.json
```

## 📝 使用示例

### 示例 1：启用文本精炼（本地模型）

1. 打开设置面板
2. 设置以下参数：
   - 启用文本精炼：`true`
   - 精炼 Provider：`local`
   - 本地精炼模型路径：`/path/to/models/Qwen3-0.6B`
   - 精炼预设：`default`
3. 点击 "保存并重启"

### 示例 2：启用文本精炼（云端 API）

1. 打开设置面板
2. 设置以下参数：
   - 启用文本精炼：`true`
   - 精炼 Provider：`cloud`
   - 云端 API Base：`https://api.minimaxi.com/anthropic`
   - 云端 API Key：`your-api-key`
   - 云端 API 模型：`claude-3-5-sonnet-20241022`
   - 精炼预设：`meeting`
3. 点击 "保存并重启"

### 示例 3：切换热键模式

从 PTT 模式切换到 Toggle 模式：

1. 打开设置面板
2. 设置：
   - 触发模式：`toggle`
   - 触发热键：`<ctrl_r>`（开始录音）
   - 停止热键：`<ctrl_r>`（停止录音）
3. 点击 "保存并重启"

## 🐛 故障排查

### 托盘图标不显示

1. 检查是否安装了 GUI 依赖：
   ```bash
   pip install -e .[gui]
   ```

2. 检查系统托盘是否启用（某些桌面环境需要扩展）

### 菜单不显示或无法点击

**最常见原因：你的系统使用左键菜单而不是右键菜单**

1. **尝试左键点击托盘图标**（而不是右键）
   - X11 系统通常使用左键点击
   - 会弹出 tkinter 菜单窗口

2. 运行测试脚本诊断：
   ```bash
   source .venv/bin/activate
   ./scripts/test_tray_menu.py
   ```

   查看输出中的 `HAS_MENU` 值：
   - `HAS_MENU: True` - 支持右键菜单
   - `HAS_MENU: False` - 使用左键菜单

3. 如果是 GNOME 用户，可以安装 AppIndicator 扩展：
   ```bash
   sudo apt install gnome-shell-extension-appindicator
   ```

### 设置保存失败

1. 检查配置目录权限：
   ```bash
   ls -la ~/.config/recordian/
   ```

2. 手动创建目录：
   ```bash
   mkdir -p ~/.config/recordian/
   ```

## 📚 相关文档

- [Logo 使用指南](logo-guide.md)
- [文本精炼说明](text-refine.md)
- [快速切换指南](quick-switch.md)

---

**版本**：v2.0
**最后更新**：2026-02-20
