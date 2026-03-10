# Recordian 快速参考

## 启动

```bash
recordian-tray
```

默认热键：

- `右 Ctrl`：按住录音，松开识别
- `Ctrl+Alt+Q`：退出守护进程

## 核心文件

- 配置文件：`~/.config/recordian/hotkey.json`
- 自动词库：`~/.config/recordian/auto_lexicon.db`
- 预设目录：`presets/`

## 常见配置片段

```json
{
  "hotkey": "<ctrl_r>",
  "trigger_mode": "ptt",
  "commit_backend": "auto",
  "asr_provider": "qwen-asr",
  "qwen_model": "./models/Qwen3-ASR-1.7B",
  "enable_text_refine": true,
  "refine_preset": "default",
  "enable_auto_lexicon": true,
  "enable_voice_wake": false
}
```

## 文本上屏方式

推荐：

- `auto`
- `auto-fallback`

其他可选：

- `xdotool-clipboard`
- `xdotool`
- `wtype`
- `stdout`
- `none`

### 自动检测支持的应用

- 微信
- VS Code
- Obsidian
- Typora
- Discord
- Slack

### auto-fallback

`auto-fallback` 会在主方式失败时自动切换到备用方式，适合环境复杂、窗口类型多变的桌面。

### 降级链

1. `xdotool-clipboard`
2. `xdotool`
3. `wtype`
4. `stdout`

## 语音唤醒

默认关键词配置：

```json
{
  "enable_voice_wake": true,
  "wake_prefix": ["嗨", "嘿"],
  "wake_name": ["小二"]
}
```

诊断命令：

```bash
recordian-wake-diagnose
```

默认模型文件：

- `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx`
- `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx`
- `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx`
- `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/tokens.txt`

## 常用命令

```bash
# 生成/保存默认配置
recordian-hotkey-dictate --save-config

# 仅启动热键守护进程
recordian-hotkey-dictate

# 音频文件转写
recordian --mode utterance --wav sample.wav --pass1 http --pass1-endpoint http://127.0.0.1:8000/v1/audio/transcriptions
```

## 更多文档

- `README.md`
- `docs/USER_GUIDE.md`
- `docs/TROUBLESHOOTING.md`
