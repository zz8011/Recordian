# Recordian Release 安装指南

## 快速安装

1. 解压 release 包到任意目录
2. 运行安装脚本：
   ```bash
   cd Recordian
   ./install.sh
   ```

3. 安装完成后，可以通过以下方式启动：
   - **应用菜单**：搜索 "Recordian" 点击启动
   - **快速启动**：双击 `recordian-launch.sh`
   - **命令行**：`./recordian-launch.sh`

## 功能特性

- 🎤 本地语音识别（支持 FunASR 和 Qwen3-ASR）
- ⌨️ 全局热键触发（PTT 和 Toggle 模式）
- 🎨 实时音频可视化动画
- 📋 智能剪贴板上屏
- 🔧 系统托盘控制

## 默认热键

- **右 Ctrl（按住）**：PTT 模式录音
- **右 Ctrl + Space**：Toggle 模式开始/停止
- **Ctrl + Alt + Q**：退出程序

## 配置文件

配置文件位置：`~/.config/recordian/`

- `hotkey.json` - 热键配置
- `app.json` - 应用配置

## 卸载

运行卸载脚本：
```bash
./uninstall.sh
```

## 系统要求

- Linux (X11 或 Wayland)
- Python 3.10+
- 推荐：NVIDIA GPU（用于 Qwen3-ASR 加速）

## 故障排除

### 托盘图标不显示
确保安装了以下依赖之一：
- `gir1.2-appindicator3-0.1` (Ubuntu/Debian)
- `libappindicator-gtk3` (Arch)

### 热键不响应
检查是否有其他程序占用了相同的热键组合。

### 音频设备问题
运行 `recordian-tray` 后查看终端输出，确认音频设备正确识别。

## 更多信息

项目主页：https://github.com/your-repo/Recordian
