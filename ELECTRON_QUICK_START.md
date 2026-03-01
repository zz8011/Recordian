# Electron 应用自动检测 - 快速开始

## 零配置使用

Recordian 现在会自动检测 Electron 应用并选择最佳输入方式，无需任何配置！

### 支持的应用

- ✅ 微信（WeChat）
- ✅ VS Code
- ✅ Obsidian
- ✅ Typora
- ✅ Discord
- ✅ Slack
- ✅ 其他 Electron 应用

### 使用方法

1. **启动 Recordian**
   ```bash
   recordian-tray
   ```

2. **在任意 Electron 应用中使用**
   - 打开微信聊天窗口
   - 点击输入框
   - 按下快捷键（默认 Ctrl+Alt+V）
   - 说话
   - 再次按下快捷键
   - 文本自动输入到微信 ✨

### 工作原理

Recordian 会：
1. 自动检测当前窗口是否为 Electron 应用
2. 选择最佳输入方式（xdotool-clipboard）
3. 缓存检测结果（5 秒）以提升性能
4. 如果失败，自动尝试备用方式

### 高级配置

#### 启用降级机制（推荐）

```bash
export RECORDIAN_COMMIT_BACKEND=auto-fallback
recordian-tray
```

降级链：xdotool-clipboard → xdotool → wtype → stdout

#### 手动指定输入方式

```bash
export RECORDIAN_COMMIT_BACKEND=xdotool-clipboard
recordian-tray
```

### 故障排查

#### 问题：检测失败

**解决方法**：
```bash
# 安装 xprop（X11 必需）
sudo apt install x11-utils  # Debian/Ubuntu
sudo dnf install xorg-x11-utils  # Fedora
```

#### 问题：输入延迟

**原因**：剪贴板粘贴方式比直接输入慢 ~100ms，这是正常现象。

**优化**：确保系统负载不高，检查是否安装了 xdotool。

#### 问题：Wayland 环境

**解决方法**：使用 X11 会话以获得最佳体验，或使用 wtype 作为备用。

### 更多信息

- 完整文档：[docs/USER_GUIDE.md](docs/USER_GUIDE.md)
- 故障排查：[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- 实现报告：[ELECTRON_DETECTION_REPORT.md](ELECTRON_DETECTION_REPORT.md)

---

**享受零配置的 Electron 应用语音输入体验！** 🎉
