# Recordian Logo 使用指南

## 📁 Logo 文件

### 主 Logo
- **logo.svg** - 默认状态 logo
  - 青绿色渐变圆形
  - 双层圆环结构（浅色外环 + 深色中环）
  - 两个椭圆形眼睛
  - 用于：应用图标、文档、官网

### 状态 Logo

#### 1. 录音中 (Recording)
- **logo-recording.svg**
  - 配色：青蓝色渐变
  - 特效：脉动光晕动画 + 眼睛闪烁 + 声波指示器
  - 表情：专注的眼睛
  - 用于：托盘图标（录音时）、录音状态提示

#### 2. 录音完成 (Success)
- **logo-success.svg**
  - 配色：绿色渐变
  - 表情：弯曲的开心眼睛 + 微笑嘴巴 😊
  - 用于：托盘图标（识别完成）、成功通知

#### 3. 错误状态 (Error)
- **logo-error.svg**
  - 配色：红橙色渐变
  - 表情：圆形惊讶眼睛 + O形嘴巴 😮
  - 用于：托盘图标（错误时）、错误通知

## 🎨 设计规范

### 配色方案

| 状态 | 主色调 | 内圆渐变 | 外环颜色 | 中环颜色 |
|------|--------|----------|----------|----------|
| 默认 | 青绿色 | #A7F3D0 → #6EE7B7 → #5EEAD4 | #CCFBF1 → #99F6E4 | #5F7C6F → #6B8A7A |
| 录音中 | 青蓝色 | #A5F3FC → #67E8F9 → #22D3EE | #CFFAFE → #A5F3FC | #5F7C8A → #6B8A9A |
| 完成 | 绿色 | #BBF7D0 → #86EFAC → #4ADE80 | #DCFCE7 → #BBF7D0 | #5F7C6F → #6B8A7A |
| 错误 | 红橙色 | #FECACA → #FCA5A5 → #F87171 | #FEE2E2 → #FECACA | #7C5F5F → #8A6B6B |

### 设计元素

- **双层圆环**：外层浅色 + 中层深色，营造立体感
- **椭圆眼睛**：深色椭圆形，表达不同情绪
- **表情变化**：通过眼睛和嘴巴的形状传达状态

### 尺寸规范

- **标准尺寸**：200x200px
- **最小尺寸**：32x32px（托盘图标）
- **推荐尺寸**：
  - 应用图标：128x128px, 256x256px, 512x512px
  - 文档展示：120x120px
  - 网页使用：64x64px, 96x96px

### 使用场景

```
应用场景                    使用 Logo
─────────────────────────────────────────
应用图标                    logo.svg
README 文档                 logo.svg
托盘图标（空闲）            logo.svg
托盘图标（录音中）          logo-recording.svg
托盘图标（识别完成）        logo-success.svg
托盘图标（错误）            logo-error.svg
桌面通知（成功）            logo-success.svg
桌面通知（失败）            logo-error.svg
波纹动画窗口                logo-recording.svg
```

## 💻 代码集成示例

### Python (托盘图标)

```python
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"

LOGO_STATES = {
    "idle": ASSETS_DIR / "logo.svg",
    "recording": ASSETS_DIR / "logo-recording.svg",
    "success": ASSETS_DIR / "logo-success.svg",
    "error": ASSETS_DIR / "logo-error.svg",
}

def update_tray_icon(state: str):
    icon_path = LOGO_STATES.get(state, LOGO_STATES["idle"])
    # 更新托盘图标
    tray.icon = Image.open(icon_path)
```

### HTML (网页展示)

```html
<!-- 默认 logo -->
<img src="assets/logo.svg" width="120" height="120" alt="Recordian">

<!-- 录音中状态 -->
<img src="assets/logo-recording.svg" width="64" height="64" alt="Recording">

<!-- 成功状态 -->
<img src="assets/logo-success.svg" width="64" height="64" alt="Success">

<!-- 错误状态 -->
<img src="assets/logo-error.svg" width="64" height="64" alt="Error">
```

## 📝 注意事项

1. **SVG 格式**：所有 logo 均为 SVG 矢量格式，可无损缩放
2. **动画效果**：logo-recording.svg 包含 CSS 动画，在支持的环境中自动播放
3. **颜色一致性**：所有状态 logo 保持统一的圆形结构和同心圆元素
4. **背景透明**：所有 logo 背景透明，适配深色/浅色主题

## 🔄 状态转换流程

```
空闲 (logo.svg)
    ↓ 按下热键
录音中 (logo-recording.svg) ← 动画播放
    ↓ 松开热键
识别处理中 (logo-recording.svg)
    ↓
    ├─ 成功 → 完成 (logo-success.svg) → 2秒后 → 空闲
    └─ 失败 → 错误 (logo-error.svg) → 3秒后 → 空闲
```

## 📦 导出其他格式

如需 PNG 格式，可使用以下命令转换：

```bash
# 安装 Inkscape 或 ImageMagick
sudo apt install inkscape

# 转换为 PNG（多种尺寸）
for size in 32 64 128 256 512; do
  inkscape assets/logo.svg \
    --export-filename=assets/logo-${size}.png \
    --export-width=${size} \
    --export-height=${size}
done
```

---

**设计版本**：v1.0
**最后更新**：2026-02-20
