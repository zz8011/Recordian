#!/bin/bash
# Recordian Release 打包脚本

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Recordian Release 打包 ==="
echo ""

VERSION=$(awk -F'"' '/^version = / {print $2; exit}' pyproject.toml)
if [ -z "$VERSION" ]; then
    echo "错误: 无法从 pyproject.toml 解析版本号"
    exit 1
fi
echo "版本: $VERSION"

RELEASE_DIR="recordian-$VERSION"
RELEASE_ARCHIVE="$RELEASE_DIR.tar.gz"

copy_required() {
    local path="$1"
    if [ ! -e "$path" ]; then
        echo "错误: 缺少必需文件/目录: $path"
        exit 1
    fi
    cp -r "$path" "$RELEASE_DIR/"
}

copy_optional() {
    local path="$1"
    if [ -e "$path" ]; then
        cp -r "$path" "$RELEASE_DIR/"
        return
    fi
    echo "提示: 可选文件不存在，已跳过: $path"
}

echo "清理旧的 release 文件..."
rm -rf "$RELEASE_DIR" "$RELEASE_ARCHIVE"

echo "创建 release 目录..."
mkdir -p "$RELEASE_DIR"

echo "复制核心文件..."
required_paths=(
    src
    assets
    presets
    docs
    server
    tests
    pyproject.toml
    README.md
    LICENSE
    install.sh
    uninstall.sh
    recordian-launch.sh
)

optional_paths=(
    QUICK_REFERENCE.md
    ANIMATION_FIX.md
    uv.lock
)

for path in "${required_paths[@]}"; do
    copy_required "$path"
done

for path in "${optional_paths[@]}"; do
    copy_optional "$path"
done

echo "清理缓存和构建残留..."
find "$RELEASE_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$RELEASE_DIR" -type d -name "*.egg-info" -prune -exec rm -rf {} +
find "$RELEASE_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

cat > "$RELEASE_DIR/README.txt" << 'EOF'
Recordian - Linux 语音输入助手
================================

快速开始：
1. 运行 ./install.sh 安装
2. 从应用菜单启动 Recordian
3. 使用右 Ctrl 键触发语音输入

详细说明请查看：
- README.md
- docs/USER_GUIDE.md

EOF

echo "打包..."
tar czf "$RELEASE_ARCHIVE" "$RELEASE_DIR"

echo "清理临时目录..."
rm -rf "$RELEASE_DIR"

echo ""
echo "=== 打包完成！ ==="
echo "Release 文件: $RELEASE_ARCHIVE"
echo "大小: $(du -h "$RELEASE_ARCHIVE" | cut -f1)"
echo ""
echo "发布步骤："
echo "1. 上传 $RELEASE_ARCHIVE 到 GitHub Releases"
echo "2. 用户下载后解压: tar xzf $RELEASE_ARCHIVE"
echo "3. 用户运行: cd recordian-$VERSION && ./install.sh"
echo ""
