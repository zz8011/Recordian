# 虚拟环境配置说明

## PyGObject (gi) 模块配置

### 问题
默认隔离的虚拟环境无法导入系统安装的 PyGObject。自行安装 PyGObject 还需要匹配的系统 GTK 与开发库。

### 解决方案
使用能导入 `gi` 的系统 Python，通过 `--system-site-packages` 创建虚拟环境。下面以 `/usr/bin/python3` 为例：

```bash
/usr/bin/python3 -c "import gi"
/usr/bin/python3 -m venv --system-site-packages .venv
```

已有 `.venv` 只有在 Python 主次版本与系统解释器一致时才能按此方式更新。版本不同时先保留旧环境并另建环境，不要原地覆盖；`install.sh` 会检测这种情况并停止安装。

若无法使用 `--system-site-packages`，也可通过 `.pth` 文件让虚拟环境访问系统的 PyGObject：

```bash
# 创建 .pth 文件（以 Debian/Ubuntu 为例）
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
该配置已由 `install.sh` 自动处理（自动检测系统 Python 并启用 `--system-site-packages`，若检测到既有隔离环境会自动升级，并由目标解释器实际验证 gi）。
