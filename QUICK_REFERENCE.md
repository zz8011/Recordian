# Recordian 快速参考

## 🎯 常用操作

### 启动和停止
```bash
# 启动托盘程序
recordian-tray

# 停止所有进程
pkill -f recordian

# 重启
pkill -f recordian && recordian-tray
```

### 使用热键
- **Ctrl+R**: 按住录音，松开识别（PTT 模式）
- **Ctrl+R + Space**: 切换录音（Toggle 模式）
- **Ctrl+Alt+Q**: 退出守护进程

## 🔧 配置文件

**位置**: `~/.config/recordian/hotkey.json`

### 关键配置项
```json
{
  "hotkey": "<ctrl_r>",              // 录音热键
  "input_device": "default",         // 音频输入设备
  "asr_provider": "qwen-asr",        // ASR 提供者
  "qwen_model": "./models/...",      // 模型路径
  "enable_text_refine": true,        // 文本精炼
  "refine_preset": "default",        // 精炼预设
  "enable_voice_wake": false,        // 语音唤醒开关
  "wake_prefix": ["嗨", "嘿"],       // 唤醒前缀
  "wake_name": ["小二"],             // 唤醒名字
  "sound_on_path": "assets/wake-on.mp3",   // 全局开始音效
  "sound_off_path": "assets/wake-off.mp3", // 全局结束音效
  "wake_use_webrtcvad": true,        // 使用 WebRTC VAD 判定说话/静音
  "wake_vad_aggressiveness": 2,      // VAD 灵敏度: 0-3
  "wake_vad_frame_ms": 30,           // VAD 帧长: 10/20/30ms
  "wake_no_speech_timeout_s": 2.0,   // 唤醒后未开口超时自动结束
  "wake_auto_stop_silence_s": 1.0,   // 静音自动结束秒数
  "commit_backend": "auto"           // 文本上屏方式
}
```

### 语音唤醒模型（sherpa-onnx）
- 默认模型目录：`models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`
- 默认文件：
  - `encoder-epoch-12-avg-2-chunk-16-left-64.onnx`
  - `decoder-epoch-12-avg-2-chunk-16-left-64.onnx`
  - `joiner-epoch-12-avg-2-chunk-16-left-64.onnx`
  - `tokens.txt`

## 🎨 动画问题

### 动画无响应
**原因**: 设备音量低或采样率不匹配

**快速检查**:
```bash
# 查看设备信息
.venv/bin/python3 -c "
import sounddevice as sd
dev = sd.query_devices(kind='input')
print(f'设备: {dev[\"name\"]}')
print(f'采样率: {dev[\"default_samplerate\"]} Hz')
"

# 测试音量
.venv/bin/python3 -c "
import sounddevice as sd
import numpy as np
import time

def cb(indata, *args):
    rms = float(np.sqrt(np.mean(indata ** 2)))
    print(f'RMS: {rms:.4f}', end='\r')

with sd.InputStream(samplerate=48000, channels=1, callback=cb):
    time.sleep(3)
"
```

**调整增益**:
编辑 `src/recordian/hotkey_dictate.py` 第 407 行：
```python
# 低音量设备（DJI 无线麦克风）
level = min(1.0, max(0.0, rms * 12.0 - 0.05))

# 中等音量设备
level = min(1.0, max(0.0, rms * 8.0 - 0.03))

# 高音量设备
level = min(1.0, max(0.0, rms * 3.0 - 0.02))
```

## 🎤 音频设备

### 查看可用设备
```bash
# PipeWire/PulseAudio
wpctl status | grep -A 10 "Sources"

# ALSA
arecord -l

# sounddevice
.venv/bin/python3 -c "
import sounddevice as sd
for i, dev in enumerate(sd.query_devices()):
    if dev['max_input_channels'] > 0:
        print(f'{i}: {dev[\"name\"]} ({dev[\"default_samplerate\"]} Hz)')
"
```

### 设置默认设备
```bash
# 查看当前默认源
wpctl status | grep "Default Source" -A 5

# 设置默认源（替换 ID）
wpctl set-default 82  # DJI MIC MINI 的 ID
```

### 调整音量
```bash
# 查看音量
wpctl get-volume @DEFAULT_AUDIO_SOURCE@

# 设置音量 100%
wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 1.0

# 设置音量 80%
wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.8
```

## 📝 文本精炼

### 切换预设
编辑配置文件：
```json
{
  "refine_preset": "default"   // 日常口语
  "refine_preset": "formal"    // 正式书面语
  "refine_preset": "meeting"   // 会议纪要
  "refine_preset": "technical" // 技术文档
  "refine_preset": "summary"   // 简洁总结
}
```

### 自定义预设
创建文件 `presets/my-preset.md`：
```markdown
你是一个文本精炼助手。

任务：
- 去除口语化表达
- 修正标点符号
- 保持原意

示例：
输入：那个，我觉得这个方案，嗯，应该可以
输出：我认为这个方案可行。
```

使用：
```json
{
  "refine_preset": "my-preset"
}
```

## 🐛 调试

### 启用详细日志
```json
{
  "debug_diagnostics": true
}
```

### 查看日志
```bash
# 实时日志
tail -f /tmp/recordian.log

# 查看最近 50 行
tail -50 /tmp/recordian.log

# 搜索错误
grep -i error /tmp/recordian.log
```

### 测试单次识别
```bash
# 录音 3 秒并识别
.venv/bin/recordian-linux-dictate \
  --duration 3 \
  --commit-backend stdout

# 识别已有音频文件
.venv/bin/recordian \
  --wav test.wav \
  --commit-backend stdout
```

### 检查进程状态
```bash
# 查看运行中的进程
ps aux | grep recordian

# 查看进程树
pstree -p | grep recordian

# 查看资源占用
top -p $(pgrep -f recordian | tr '\n' ',')
```

## 🔍 常见问题

### 动画无响应
→ 查看 [ANIMATION_FIX.md](ANIMATION_FIX.md)

### 录音无声音
→ 检查输入设备和音量设置

### 识别不准确
→ 调整模型、语言设置、音频质量

### 热键不响应
→ 检查热键冲突、后端进程状态

### 文本上屏失败
→ 检查 commit_backend 设置

详细排查步骤: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)

## 📚 文档索引

- **README.md** - 项目介绍和快速开始
- **docs/USER_GUIDE.md** - 完整用户指南
- **docs/TROUBLESHOOTING.md** - 故障排查指南
- **ANIMATION_FIX.md** - 动画修复技术文档
- **QUICK_REFERENCE.md** - 本文档（快速参考）

## 💡 提示

### 性能优化
- 使用轻量模型 (Qwen3-ASR-0.6B)
- 关闭不需要的文本精炼
- 启用 GPU 加速

### 准确度优化
- 使用大模型 (Qwen3-ASR-1.7B)
- 添加热词 (hotword)
- 调整语言设置

### 兼容性
- X11: 使用 xdotool
- Wayland: 使用 wtype
- Electron 应用: 自动使用剪贴板模式

## 🆘 获取帮助

遇到问题？
1. 查看 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
2. 启用 debug_diagnostics 查看日志
3. 在 GitHub 提交 Issue

---

**最后更新**: 2026-02-26
**版本**: 0.1.0
