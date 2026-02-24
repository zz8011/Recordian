#!/bin/bash
# Recordian Release 打包脚本

set -e

echo "=== Recordian Release 打包 ==="
echo ""

# 获取版本号
VERSION=$(grep 'version = ' pyproject.toml | head -1 | cut -d'"' -f2)
echo "版本: $VERSION"

# 创建 release 目录
RELEASE_DIR="recordian-$VERSION"
RELEASE_ARCHIVE="recordian-$VERSION.tar.gz"

echo "清理旧的 release 文件..."
rm -rf "$RELEASE_DIR" "$RELEASE_ARCHIVE"

echo "创建 release 目录..."
mkdir -p "$RELEASE_DIR"

# 复制必要文件
echo "复制项目文件..."
cp -r src "$RELEASE_DIR/"
cp -r assets "$RELEASE_DIR/"
cp -r presets "$RELEASE_DIR/"
cp -r docs "$RELEASE_DIR/"
cp -r tests "$RELEASE_DIR/"
cp pyproject.toml "$RELEASE_DIR/"
cp README.md "$RELEASE_DIR/"
cp QUICKSTART.md "$RELEASE_DIR/"
cp CHANGELOG.md "$RELEASE_DIR/"
cp RELEASE_NOTES.md "$RELEASE_DIR/"
cp LICENSE "$RELEASE_DIR/"
cp INSTALL.md "$RELEASE_DIR/"
cp install.sh "$RELEASE_DIR/"
cp uninstall.sh "$RELEASE_DIR/"

# 复制配置示例
if [ -d "config_examples" ]; then
    cp -r config_examples "$RELEASE_DIR/"
fi

# 创建 README
cat > "$RELEASE_DIR/README.txt" << 'EOF'
Recordian - Linux 语音输入助手
================================

快速开始：
1. 运行 ./install.sh 安装
2. 从应用菜单启动 Recordian
3. 使用右 Ctrl 键触发语音输入

详细说明请查看 INSTALL.md

EOF

echo "打包..."
tar czf "$RELEASE_ARCHIVE" "$RELEASE_DIR"

echo "清理临时文件..."
rm -rf "$RELEASE_DIR"

echo ""
echo "=== 打包完成！ ==="
echo "Release 文件: $RELEASE_ARCHIVE"
echo "大小: $(du -h $RELEASE_ARCHIVE | cut -f1)"
echo ""
echo "发布步骤："
echo "1. 上传 $RELEASE_ARCHIVE 到 GitHub Releases"
echo "2. 用户下载后解压: tar xzf $RELEASE_ARCHIVE"
echo "3. 用户运行: cd $RELEASE_DIR && ./install.sh"
echo ""
