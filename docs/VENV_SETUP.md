# 虚拟环境配置说明

## PyGObject (gi) 模块配置

### 问题
虚拟环境中无法直接安装 PyGObject，因为它依赖系统级的 GTK 库。

### 解决方案
通过 `.pth` 文件让虚拟环境访问系统的 PyGObject：

```bash
# 创建 .pth 文件
cat > .venv/lib/python3.12/site-packages/gi.pth << 'EOF'
/usr/lib/python3/dist-packages
EOF
```

### 验证
```bash
# 测试导入
.venv/bin/python -c "import gi; print('OK')"

# 测试 AppIndicator3
.venv/bin/python -c "
import gi
gi.require_version('AppIndicator3', '0.1')
from gi.repository import AppIndicator3
print('AppIndicator3 OK')
"
```

### 系统依赖
确保已安装系统依赖：
```bash
sudo apt install gir1.2-appindicator3-0.1 python3-gi
```

### 注意事项
- 这个配置只需要做一次
- 如果重新创建虚拟环境，需要重新配置
- Python 版本变化时需要更新路径（如 python3.12 -> python3.13）

### 自动化脚本
可以在 `install.sh` 中添加这个配置步骤。
