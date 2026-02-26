# Recordian 故障排查指南

## 动画无响应问题

### 症状
- 录音功能正常
- 语音识别正常
- 屏幕底部动画不随音量变化

### 常见原因

#### 1. 采样率不匹配
**检查方法：**
```bash
.venv/bin/python3 -c "
import sounddevice as sd
dev = sd.query_devices(kind='input')
print(f'设备: {dev[\"name\"]}')
print(f'采样率: {dev[\"default_samplerate\"]} Hz')
"
```

**解决方案：**
代码已自动检测设备采样率（修复于 2026-02-26）

#### 2. 音量增益不足
**检查方法：**
```bash
.venv/bin/python3 -c "
import sounddevice as sd
import numpy as np
import time

def callback(indata, frames, time_info, status):
    rms = float(np.sqrt(np.mean(indata ** 2)))
    print(f'RMS: {rms:.4f}')

with sd.InputStream(samplerate=48000, channels=1, callback=callback):
    time.sleep(3)
"
```

**正常值：**
- 说话时 RMS > 0.01
- 如果 RMS < 0.005，说明音量太低

**解决方案：**
调整增益系数（src/recordian/hotkey_dictate.py:407）：
```python
# 低音量设备（DJI 无线麦克风）
level = min(1.0, max(0.0, rms * 12.0 - 0.05))

# 中等音量设备（USB 麦克风）
level = min(1.0, max(0.0, rms * 8.0 - 0.03))

# 高音量设备（内置麦克风）
level = min(1.0, max(0.0, rms * 3.0 - 0.02))
```

#### 3. sounddevice 未安装
**检查方法：**
```bash
.venv/bin/python3 -c "import sounddevice; print('已安装')"
```

**解决方案：**
```bash
.venv/bin/pip install sounddevice
```

#### 4. 设备权限问题
**检查方法：**
```bash
arecord -l  # 查看是否能列出设备
```

**解决方案：**
```bash
# 添加用户到 audio 组
sudo usermod -aG audio $USER
# 重新登录生效
```

## 录音无声音

### 症状
- 动画正常
- 识别结果为空或错误

### 常见原因

#### 1. 输入设备配置错误
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep input_device
```

**解决方案：**
```bash
# 查看可用设备
pactl list sources short

# 修改配置
vim ~/.config/recordian/hotkey.json
# 设置 "input_device": "设备名称"
```

#### 2. 设备音量过低
**检查方法：**
```bash
wpctl get-volume @DEFAULT_AUDIO_SOURCE@
```

**解决方案：**
```bash
# 设置音量为 100%
wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 1.0
```

#### 3. 设备被其他程序占用
**检查方法：**
```bash
lsof /dev/snd/*
```

**解决方案：**
关闭占用设备的程序

## 识别结果不准确

### 症状
- 录音正常
- 识别结果错误或不完整

### 常见原因

#### 1. 模型路径错误
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep qwen_model
ls -la $(cat ~/.config/recordian/hotkey.json | grep qwen_model | cut -d'"' -f4)
```

**解决方案：**
确保模型文件存在，路径正确

#### 2. 音频质量差
**检查方法：**
录制测试音频并播放：
```bash
arecord -d 3 -f S16_LE -r 16000 test.wav
aplay test.wav
```

**解决方案：**
- 调整麦克风位置
- 提高输入音量
- 减少环境噪音

#### 3. 语言设置不匹配
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep qwen_language
```

**解决方案：**
```json
{
  "qwen_language": "auto"  // 自动检测
  // 或
  "qwen_language": "Chinese"  // 中文
  "qwen_language": "English"  // 英文
}
```

## 热键不响应

### 症状
- 按热键无反应
- 托盘图标正常

### 常见原因

#### 1. 热键冲突
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep hotkey
```

**解决方案：**
更改热键配置，避免与系统快捷键冲突

#### 2. 后端进程未运行
**检查方法：**
```bash
ps aux | grep hotkey_dictate
```

**解决方案：**
```bash
pkill -f recordian
recordian-tray
```

#### 3. X11/Wayland 权限问题
**检查方法：**
```bash
echo $XDG_SESSION_TYPE  # 查看会话类型
```

**解决方案：**
- X11: 确保 xdotool 已安装
- Wayland: 确保 wtype 已安装

## 文本上屏失败

### 症状
- 识别正常
- 文本未输入到应用

### 常见原因

#### 1. 上屏后端不兼容
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep commit_backend
```

**解决方案：**
- 推荐先使用自动模式：
```json
{
  "commit_backend": "auto"
}
```

- 手动指定时可选值：
  - X11: `xdotool` 或 `xdotool-clipboard`（Electron 应用优先推荐 `xdotool-clipboard`）
  - Wayland: `wtype`
  - 调试：`stdout`

#### 2. Electron 应用兼容性
**症状：**
微信、Obsidian 等 Electron 应用无法接收输入

**解决方案：**
使用剪贴板模式（已自动处理）

#### 3. 焦点窗口丢失
**检查方法：**
查看日志中的 target_window_id

**解决方案：**
确保录音时焦点在目标窗口

## 性能问题

### 症状
- 识别延迟高
- 系统卡顿

### 常见原因

#### 1. GPU 未启用
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep device
nvidia-smi  # 查看 GPU 使用情况
```

**解决方案：**
```json
{
  "device": "cuda"  // 使用 GPU
}
```

#### 2. 模型过大
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep qwen_model
du -h $(cat ~/.config/recordian/hotkey.json | grep qwen_model | cut -d'"' -f4)
```

**解决方案：**
使用轻量模型：
```json
{
  "qwen_model": "./models/Qwen3-ASR-0.6B"  // 替代 1.7B
}
```

#### 3. 文本精炼开启
**检查方法：**
```bash
cat ~/.config/recordian/hotkey.json | grep enable_text_refine
```

**解决方案：**
如不需要，可关闭：
```json
{
  "enable_text_refine": false
}
```

## 调试技巧

### 启用详细日志
```json
{
  "debug_diagnostics": true
}
```

### 查看实时日志
```bash
tail -f /tmp/recordian.log
```

### 测试单次识别
```bash
.venv/bin/recordian-linux-dictate \
  --duration 3 \
  --commit-backend stdout
```

### 检查配置
```bash
cat ~/.config/recordian/hotkey.json | jq .
```

### 重置配置
```bash
mv ~/.config/recordian/hotkey.json ~/.config/recordian/hotkey.json.bak
recordian-tray  # 将生成默认配置
```

## 常用命令

```bash
# 启动托盘
recordian-tray

# 停止所有进程
pkill -f recordian

# 重启
pkill -f recordian && recordian-tray

# 查看进程
ps aux | grep recordian

# 查看设备
.venv/bin/python3 -c "import sounddevice; print(sounddevice.query_devices())"

# 测试录音
arecord -d 3 -f S16_LE -r 16000 test.wav && aplay test.wav

# 查看配置
cat ~/.config/recordian/hotkey.json

# 编辑配置
vim ~/.config/recordian/hotkey.json
```

## 获取帮助

- GitHub Issues: https://github.com/your-repo/recordian/issues
- 文档: docs/USER_GUIDE.md
- 技术细节: ANIMATION_FIX.md
