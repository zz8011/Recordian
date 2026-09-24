# Recordian Commit — fcitx5 流式上屏插件

把外部程序的语音识别结果通过真正的输入法通道写入当前焦点的
InputContext（IC），支持绑定会话的流式预编辑（preedit）与一次性提交。

## 构建

```sh
fcitx/recordian-commit/build.sh            # 输出到 /tmp/recordian-commit-build
fcitx/recordian-commit/build.sh /tmp/mydir # 自定义构建目录
```

需要 `cmake`、`g++`、`fcitx5` 开发头文件（`/usr/include/Fcitx5`，
本机已安装 Fcitx5 **5.1.7**）。构建产物 `librecordian-commit.so` 放在构建目录；
本仓库不把 .so 纳入 git，也不自动安装/重启用户 fcitx。

### 安装到系统插件目录（手动，需自行执行）

`CMakeLists.txt` 在未显式传入时使用：

- `FCITX_INSTALL_ADDONDIR` = `${CMAKE_INSTALL_FULL_LIBDIR}/fcitx5`
- `FCITX_INSTALL_ADDONDESCDIR` = `${CMAKE_INSTALL_FULL_DATAROOTDIR}/fcitx5/addon`

在 Debian/Ubuntu 上把安装前缀设为 `/usr` 时，GNUInstallDirs 的多架构
`libdir` 解析为 `/usr/lib/x86_64-linux-gnu`，因此上述两个变量分别为：

- `/usr/lib/x86_64-linux-gnu/fcitx5`（本机该目录已存在，属 root）
- `/usr/share/fcitx5/addon`（描述文件 `recordian-commit.conf`）

```sh
cmake -S fcitx/recordian-commit -B /tmp/recordian-commit-build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr
cmake --build /tmp/recordian-commit-build -j"$(nproc)"
# 确认解析结果，应看到上面两个绝对路径：
cmake -L -N /tmp/recordian-commit-build | grep FCITX_INSTALL_
```

替换前先备份已安装文件（尚无该插件时，备份命令会失败，可跳过）：

```sh
sudo cp -a /usr/lib/x86_64-linux-gnu/fcitx5/librecordian-commit.so \
  /usr/lib/x86_64-linux-gnu/fcitx5/librecordian-commit.so.bak
sudo cp -a /usr/share/fcitx5/addon/recordian-commit.conf \
  /usr/share/fcitx5/addon/recordian-commit.conf.bak
sudo cmake --install /tmp/recordian-commit-build
```

Fcitx5 在进程启动时加载插件。替换 `.so` 后，先结束当前输入，再在已有
图形会话里重启 Fcitx5（也可退出并重新登录）：

```sh
gdbus call --session --dest org.fcitx.Fcitx5 --object-path /controller \
  --method org.fcitx.Fcitx.Controller1.Restart
# 等待输入法重新出现后验证新插件：
gdbus call --session --dest org.fcitx.Fcitx5 --object-path /recordian \
  --method org.fcitx.Fcitx.Recordian1.Ping
# 预期：('ok',)
```

`fcitx5-remote -r` 只重新加载配置，不能作为新插件已经加载的证据。
2026-09-24 本机实装时，通过上面的重启方法及 `Ping` 验证了插件加载。

回滚：

```sh
sudo cp -a /usr/lib/x86_64-linux-gnu/fcitx5/librecordian-commit.so.bak \
  /usr/lib/x86_64-linux-gnu/fcitx5/librecordian-commit.so
sudo cp -a /usr/share/fcitx5/addon/recordian-commit.conf.bak \
  /usr/share/fcitx5/addon/recordian-commit.conf
gdbus call --session --dest org.fcitx.Fcitx5 --object-path /controller \
  --method org.fcitx.Fcitx.Controller1.Restart
```

不在这里假设 `~/.local` 或其他用户级库搜索路径；上面只使用 CMake
安装变量在 `/usr` 前缀下的系统插件目录。

## DBus 协议（org.fcitx.Fcitx5 /recordian org.fcitx.Fcitx.Recordian1）

| 方法 | 签名 | 说明 |
|------|------|------|
| `Ping` | `() → s` | 存活探测，返回 `ok` |
| `BeginSession` | `(s) → s` | 把流式会话绑定到**当前焦点**且非 dummy、非密码的 IC；返回 `<token> preedit=<0/1> frontend=<f> program=<p>` |
| `UpdatePreedit` | `(ss) → s` | 只替换绑定 IC 的 client preedit，不提交、不抢焦点 |
| `CommitSession` | `(ss) → s` | 清空 preedit 后把最终文本**提交一次**，token 随即失效 |
| `CancelSession` | `(s) → s` | 清空 preedit、丢弃会话，不提交 |
| `CommitText` | `(s) → s` | 旧非流式接口：严格要求当前有焦点 IC，无 `mostRecentInputContext` 回退 |

### 会话失效条件（之后所有 Update/Commit 返回 StaleSession 错误）

- 绑定的 IC 失焦（FocusOut）或销毁（Destroyed）；
- 用户在绑定 IC 上按下会改字或移动光标的键（KeyEvent press）。
  **单独的修饰键按下不失效**：`Key::isModifier()` 为真的键
  （Fcitx 5.1.7：左右 Shift / Control / Meta / Alt / Super / Hyper；
  不看状态位，因此已经带上 Ctrl 状态的 Control_R 仍算修饰键）。
  字符、空格、退格、方向键/翻页，以及主键不是修饰键的组合
  （Ctrl+A、Ctrl+Left 等）仍然失效。按键释放本来就不失效；
- IC capability 变为 Password/Sensitive（含 Begin 之后）；
- toolkit Reset（同输入框内鼠标点击 / 应用主动 reset）、光标或
  SurroundingText 变化、手动切换输入法（真实事件 watcher：
  `InputContextReset` / `InputContextSurroundingTextUpdated` /
  `InputContextSwitchInputMethod`）；
- 同一焦点上开始新会话（旧 token 失效并**从会话表移除**——容量只统计
  活跃会话，同一焦点的连发 Begin 不会因残留 finished 条目耗尽
  `kMaxSessions`）；
- token 已 Commit/Cancel，或超过 **120s 无活动 TTL**（自最后一次成功
  UpdatePreedit——含 preview-only no-op——起算，而非 Begin 后 120s；
  TTL 到期只清除本会话自己拥有的 preedit，不残留）。

### 安全与兼容性约定

- **不写入密码框**：Begin 与每次 Update/Commit 前都检查
  `CapabilityFlag::Password`/`Sensitive`，命中即拒绝。
- **不跳回焦点**：所有操作只作用于 Begin 时绑定的 uuid；失焦即报错，
  绝不通过 `mostRecentInputContext`/重新聚焦把文字塞进别的窗口。
- **preedit 能力协商**：`preedit=0` 的 client（未声明
  `CapabilityFlag::Preedit`）上 UpdatePreedit 是 no-op（仍刷新 TTL 活动时
  钟），Python 端退化为“流式只预览、最终一次提交”。
- **preedit 不是可回滚保证，CommitSession 也不是唯一的上屏来源**：
  native GTK 实测 toolkit 会在 focus-out / 点击时自行把 client preedit
  commit 掉，`set_text` 也可能不经 IM Reset 落进输入框。inline preedit
  只是预览。本插件承诺的边界是：**由本插件发起的写入绝不重复、绝不写
  错窗口**（一切写入走 Begin 绑定的会话或严格聚焦的 CommitText；被取
  代/失效/外来的会话一律拒绝而非到处回滚）。“所有应用都能回滚 preedit”
  是已知平台限制（Recordian-22t），不做承诺。
- 不经过 Rime，不更新 Rime 用户词典。

### 录音热键与预编辑

已在真实输入上下文里验证的路径是**按住说话**：先按下 Control_R，
再 Begin / Update 预编辑，松开后 Commit 恰好一次。开始和停止请使用
纯修饰键（例如 `hotkey` / `stop_hotkey` 为 `<ctrl_r>`，
`toggle_hotkey` 为 `<alt_r>`）。`trigger_mode=ptt` 且
`hotkey=<ctrl_r>`、`toggle_hotkey=<alt_r>`、`stop_hotkey=<ctrl_r>`
就是这个形状：单独按下 Control_R 或 Alt_R 不再因按键本身使会话失效，
随后的 CommitSession 仍可走原 token。

字符键和会移动光标的键不能当停止键。Ctrl+A 仍会使会话失效；这是
保留的保护，不是快捷键白名单。失焦、Reset、周围文本、切换输入法、
密码框这些条件也不因修饰键豁免而放宽。

用修饰键停止时，若 toolkit 仍自行把预览预编辑提交进输入框（GTK 在
失焦/点击时会这样做），需要另案处理。本改动不宣称该路径已经在桌面上
复验通过，也不承诺所有应用都能回滚 preedit。
