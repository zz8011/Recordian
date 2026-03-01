# 常见问题 (FAQ)

本页面收集了 Recordian 使用过程中的常见问题和解决方案。

---

## 安装相关

### Q1: PyAudio 安装失败怎么办？

**A**: PyAudio 需要系统级依赖。

**Ubuntu/Debian**:
```bash
sudo apt-get install portaudio19-dev python3-dev
pip install pyaudio
```

**macOS**:
```bash
brew install portaudio
pip install pyaudio
```

**Windows**:
```bash
# 下载预编译的 wheel 文件
pip install pipwin
pipwin install pyaudio
```

---

### Q2: 提示缺少 Qt 平台插件？

**A**: 需要安装 Qt 依赖。

**Ubuntu/Debian**:
```bash
sudo apt-get install python3-pyqt6
```

**macOS**:
```bash
pip install PyQt6
```

---

## 使用相关

### Q3: 热键不起作用？

**可能原因**:

1. **热键冲突**
   - 检查是否与其他应用冲突
   - 尝试更换热键组合

2. **权限问题**
   - 确保应用有输入监听权限
   - Linux: 检查 X11 权限

3. **后台运行**
   - 确认应用正在运行
   - 检查系统托盘图标

**解决方案**:
```bash
# 查看应用日志
tail -f ~/.local/share/recordian/recordian.log

# 重启应用
pkill recordian
uv run recordian-tray
```

---

### Q4: 识别结果不准确？

**优化建议**:

1. **硬件优化**
   - 使用高质量麦克风
   - 减少环境噪音
   - 调整麦克风增益

2. **软件优化**
   - 尝试不同的 ASR 引擎
   - 启用文本精炼
   - 添加专业术语到热词库

3. **使用技巧**
   - 说话清晰、语速适中
   - 避免口头禅
   - 使用标准普通话

---

### Q5: 如何处理专业术语？

**方法 1: 添加热词**

编辑配置文件 `~/.config/recordian/config.json`:
```json
{
  "hotwords": [
    "Kubernetes",
    "Docker",
    "微服务",
    "API"
  ]
}
```

**方法 2: 创建专业预设**

创建 `~/.config/recordian/presets/tech.md`:
```markdown
# 技术文档预设

请将以下语音识别文本整理成技术文档格式：
- 保留技术术语的准确性
- 使用专业表达
- 添加适当的标点符号

常见术语：
- Kubernetes (K8s)
- Docker
- 微服务架构
- RESTful API
```

---

### Q6: 文本精炼太慢？

**优化方案**:

1. **使用本地模型**
   - 切换到 llama.cpp
   - 使用较小的模型（如 7B）

2. **调整超时设置**
   ```json
   {
     "policy": {
       "pass2_timeout_ms_cloud": 5000,
       "pass2_timeout_ms_local": 10000
     }
   }
   ```

3. **禁用精炼**
   - 对于高置信度结果跳过精炼
   - 仅在必要时启用

---

### Q7: 如何在 Wayland 下使用？

**A**: Wayland 对输入模拟有限制。

**解决方案**:

1. **使用 XWayland**
   ```bash
   export QT_QPA_PLATFORM=xcb
   uv run recordian-tray
   ```

2. **使用剪贴板模式**
   - 配置为复制到剪贴板
   - 手动粘贴

3. **切换到 X11**
   - 在登录界面选择 X11 会话

---

### Q8: 应用占用内存过高？

**排查步骤**:

1. **检查模型大小**
   - 大型 LLM 模型会占用大量内存
   - 考虑使用较小的模型

2. **检查缓存**
   ```bash
   # 清理缓存
   rm -rf ~/.cache/recordian/temp/*
   ```

3. **重启应用**
   ```bash
   pkill recordian
   uv run recordian-tray
   ```

---

## 配置相关

### Q9: 配置文件在哪里？

**位置**:
- 主配置: `~/.config/recordian/config.json`
- 预设目录: `~/.config/recordian/presets/`
- 日志文件: `~/.local/share/recordian/recordian.log`
- 缓存目录: `~/.cache/recordian/`

---

### Q10: 如何备份配置？

**备份命令**:
```bash
# 备份配置
tar -czf recordian-backup.tar.gz \
  ~/.config/recordian/ \
  ~/.local/share/recordian/

# 恢复配置
tar -xzf recordian-backup.tar.gz -C ~/
```

---

### Q11: 如何重置配置？

**重置步骤**:
```bash
# 备份当前配置（可选）
mv ~/.config/recordian ~/.config/recordian.backup

# 删除配置
rm -rf ~/.config/recordian
rm -rf ~/.local/share/recordian
rm -rf ~/.cache/recordian

# 重启应用，会自动创建默认配置
uv run recordian-tray
```

---

## 错误相关

### Q12: 提示 "Sentry DSN not configured"？

**A**: 这是正常的警告信息。

Sentry 用于错误追踪，不影响正常使用。如需启用：

```bash
export SENTRY_DSN=your_sentry_dsn
```

---

### Q13: 应用崩溃怎么办？

**排查步骤**:

1. **查看日志**
   ```bash
   tail -100 ~/.local/share/recordian/recordian.log
   ```

2. **检查依赖**
   ```bash
   uv sync --reinstall
   ```

3. **提交 Issue**
   - 访问 [GitHub Issues](https://github.com/zz8011/Recordian/issues)
   - 附上日志和错误信息
   - 描述复现步骤

---

### Q14: 录音没有声音？

**检查清单**:

1. **麦克风权限**
   ```bash
   # 测试麦克风
   arecord -d 5 test.wav
   aplay test.wav
   ```

2. **音频设备**
   ```bash
   # 列出音频设备
   arecord -l
   ```

3. **配置文件**
   ```json
   {
     "audio": {
       "device_index": 0,
       "sample_rate": 16000,
       "channels": 1
     }
   }
   ```

---

## 性能相关

### Q15: 如何提高响应速度？

**优化建议**:

1. **使用本地 ASR**
   - Sherpa-ONNX 速度最快
   - 完全离线

2. **禁用精炼**
   - 对于简单输入跳过精炼
   - 仅在必要时启用

3. **调整策略**
   ```json
   {
     "policy": {
       "confidence_threshold": 0.90,
       "min_text_length": 10
     }
   }
   ```

---

## 其他问题

### Q16: 支持哪些语言？

**当前支持**:
- 中文（普通话）
- 英文
- 中英混合

**计划支持**:
- 粤语
- 其他方言

---

### Q17: 如何贡献代码？

请查看 [开发指南](Development-Guide) 了解详情。

---

### Q18: 如何报告 Bug？

1. 访问 [GitHub Issues](https://github.com/zz8011/Recordian/issues)
2. 点击 "New Issue"
3. 选择 "Bug Report" 模板
4. 填写详细信息

---

## 获取帮助

如果以上内容没有解决你的问题：

- 查看 [故障排查](Troubleshooting) 页面
- 访问 [GitHub Discussions](https://github.com/zz8011/Recordian/discussions)
- 提交 [Issue](https://github.com/zz8011/Recordian/issues)

---

**最后更新**: 2026-03-01
