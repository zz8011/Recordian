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

Omarchy 本机由 `omarchy-fcitx5.service` 管理输入法，应使用
`systemctl --user restart omarchy-fcitx5.service`。不要在服务仍运行时
另起 `fcitx5 -r` 或调用自重启：自重启进程可能脱离服务，导致服务持续
尝试启动第二个实例。以下 D-Bus 重启方法仅用于没有该服务管理的安装。

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

Wayland 兼容补充：Wayland 虚拟输入上下文可能缓存上一轮输入框的周围文本，
因此每轮使用绑定会话后收到的首次有效快照建立基线；其它前端仍在会话
开始时保存已有有效快照。相同文字和选区的重复回传不使会话失效。
Chromium 编辑器还可能将当前预编辑包含在周围文本中；插件只接受本会话
实际发出过的最近 32 个预编辑在原插入位置的精确回报，正文两侧、Unicode
光标和选区必须匹配。首次快照已包含预编辑时，需要下一次移除或替换该
预编辑的精确回报才能规范化基线。分段提交用去除自身预编辑后的基线预测
回执，避免把已有预览再算一次。空段落的零到两个布局换行只在原点空光标、
本会话正在显示预编辑时兼容。其它文字、光标、选区和有效性变化仍然失效。
这个初始化分支不用于分段提交回执，也不挽救已污染或已失效的会话。
空的 `BeginSession` 不发送多余的空预编辑。

原生 Wayland 编辑器也可能把光标报告在预编辑起点，或在约 4 KiB 的
周围文本窗口边缘裁切。只要移除本会话确实发送过的预编辑后，光标两侧
正文仍精确匹配，插件继续预编辑；发现这类回报后，会话的 `segments`
能力单向降为 0，未提交的文本保留到同一 token 的最终提交。此判断不依赖
应用名称。局部正文变化、选区变化、用户按键或失焦仍使会话失效。

| 方法 | 签名 | 说明 |
|------|------|------|
| `Ping` | `() → s` | 存活探测，返回 `ok` |
| `BeginSession` | `(s) → s` | 把流式会话绑定到**当前焦点**且非 dummy、非密码的 IC；返回 `<token> preedit=<0/1> frontend=<f> program=<p> segments=<0/1>`。开始时周围文本有效才返回 `segments=1` 并允许 `CommitSegment`；未知时返回 `segments=0`，客户端继续预编辑、最终一次提交；旧桥没有该标记 |
| `UpdatePreedit` | `(ss) → s` | 只替换绑定 IC 的 client preedit，不提交、不抢焦点；返回 `updated segments=<0/1>`，若本会话发现不能证明分段回执则单向降为 0 |
| `CommitSegment` | `(sus) → s` | 在**同一 token** 上提交一段。`sequence` 从 1 起，每次接受后恰好 +1。重复或跳号拒绝且不写入、不推进、不消费 token。成功返回 `segment <n> <frontend> <program>`（空文本为 `segment <n> cleared`）。失焦、按键、reset、敏感能力、TTL、外来 preedit 与 `CommitSession` 相同，命中则本段不写 |
| `CommitSession` | `(ss) → s` | 在预输入仍活跃时把最终文本**提交一次**，随后清空 preedit，token 随即失效。分段成功之后仍用开始时的同一个 token |
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
  `InputContextSwitchInputMethod`）。`CommitSegment` 自己的提交会带来
  一次周围文本更新：只接受「提交前文本/选区（Unicode 光标）插入本段后」
  的那一帧，以及与该帧完全相同的重复回执。其它文本或光标变化、Reset、
  失焦仍然失效。待确认标记在回执、取消、失败和 TTL 时清掉；
- 同一焦点上开始新会话（旧 token 失效并**从会话表移除**——容量只统计
  活跃会话，同一焦点的连发 Begin 不会因残留 finished 条目耗尽
  `kMaxSessions`）；
- token 已 Commit/Cancel，或超过 **120s 无活动 TTL**（自最后一次成功
  UpdatePreedit——含 preview-only no-op——或成功的 CommitSegment 起算，
  而非 Begin 后 120s；TTL 到期只清除本会话自己拥有的 preedit，不残留）。

`CommitSegment` 的序号错误（`BadSequence`）不使 token 失效，也不写入。
若预编辑回报使周围文本不再能证明分段回执，`CommitSegment` 在写入前
返回 `SegmentsUnsafe`，也不消费 token。客户端改为缓冲未提交部分，
最后仍在同一 token 上调用一次 `CommitSession`。
回复丢失时客户端必须把该次调用当作终态不确定：不得重试同一序号、不得
改发下一个序号、不得重新 Begin、不得退回 `CommitText`。最终仍由一次
`CommitSession` 消费 token。

### 安全与兼容性约定

- **不写入密码框**：Begin 与每次 Update/Commit 前都检查
  `CapabilityFlag::Password`/`Sensitive`，命中即拒绝。
- **不跳回焦点**：所有操作只作用于 Begin 时绑定的 uuid；失焦即报错，
  绝不通过 `mostRecentInputContext`/重新聚焦把文字塞进别的窗口。
- **preedit 能力协商**：`preedit=0` 的 client（未声明
  `CapabilityFlag::Preedit`）上 UpdatePreedit 是 no-op（仍刷新 TTL 活动时
  钟），Python 端退化为“流式只预览、最终一次提交”。
- **周围文本能力协商**：开始时没有有效周围文本的输入框不能安全预测分段提交回执，返回 `segments=0`；有周围文本但随后出现上述 Wayland 回报差异时也降为 0。其自身预编辑造成的重复未知快照不使会话失效；失焦、按键、Reset 和外来预编辑仍使会话失效。缓冲模式跨识别连接保留有界文字，结束时才在原会话提交一次。
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

## 富文本编辑器的确认顺序

`CommitSession` 和非空 `CommitSegment` 必须先 `commitString(text)`，
再清理本插件拥有的 preedit。先清空再提交会产生空数据的
`compositionend`（取消输入），随后另发 `insertText`；普通 textarea
能够保留文字，但 Electron 41.0.3 + Lexical 0.42.0 可复现预览消失且
最终编辑器状态为空。先提交后清理与普通拼音确认的事件顺序一致，
能让最终文本通过活跃的 composition 保留。

空最终文本、空分段与取消仍只清理预输入；失焦、敏感输入、会话失效、
分段序号与重复提交保护保持原有行为。验证应同时读取富文本编辑器
内部状态和页面可见文字，不能只把 D-Bus 的成功回复当成应用保存成功。
