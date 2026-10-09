# Rust 运行时：构建、打包、部署与回滚

Recordian 的 Rust core 是由 Python 通过 `ctypes` 调用的 Linux 共享库，C ABI 版本为 **1**。
源代码在 `native/recordian-core/`；构建入口为 `scripts/build_native_core.py`。
它同时生成 `cdylib` 与 `rlib`，没有第三方 Rust crate 依赖，通过系统 GIO、GLib、GObject 链接 D-Bus。

本文提供可执行的构建与验收步骤。生产调用点已有下表中的本机 CPU 内核测量；
两种模式的全回归、真实模型候选和隔离 GTK 输入框结果见下文。
自然麦克风和用户日常输入窗口的长时间观察仍未完成；部署状态应以实际服务加载证据为准。

## 迁移边界

| 路径 | Rust core 的职责 | 保留的实现与约束 |
| --- | --- | --- |
| 音频转换 | float32 → PCM16 量化与 RMS | Python 采集、设备管理和线程调度；必须与原转换的边界值、非有限值、截断语义一致 |
| 服务端音频 | PCM 解码、有界 FIFO | Python WebSocket 服务和模型调度；容量溢出显式报错，不静默截断 |
| Fcitx 传输 | 持久 GIO 连接、按唯一 owner 固定会话目标 | Python 输入接口和 Fcitx C++ 插件；可能已生效的写入失败后不得用 busctl 重放 |
| 桌面与业务 | 沿用既有接口 | Python UI、托盘、语音唤醒、Agent、纠词与润色仍保留 |
| 模型 | 不迁移推理引擎 | C++/CUDA 模型仍保留；未证明等价的 encoder/KV-cache 重用不启用 |

Confucius 推理调度继续使用 **160 ms**。此前的 **320 ms** 自适应候选未通过长英文字符与尾部延迟门槛，
因此已被拒绝作为默认值。有界缓冲与减少空 executor 工作不得改变推理前缀、预算、EOS 或取消归属。

## 构建系统依赖

需要 Rust **1.88 或更新版本**（crate 的最低版本与 Rust 2024 edition 要求）、Cargo、系统链接器与 GIO/GLib 开发库。
Ubuntu / Debian 可安装：

```bash
sudo apt-get update
sudo apt-get install -y build-essential libglib2.0-dev pkg-config
```

Arch Linux 的对应系统包为 `base-devel`、`glib2`、`pkgconf` 与 `rust`。
运行时需要兼容的 GIO/GLib/GObject 动态库；D-Bus 路径还需要可访问的会话总线与 Recordian Fcitx 插件。
Python-only 安装不要求 Cargo。没有第三方 Rust 依赖不代表没有系统动态库依赖。

## 在源码目录构建

在项目根目录使用目标 Python 解释器执行：

```bash
python scripts/build_native_core.py
```

默认安装到：

```text
src/recordian/_native/librecordian_core.so
src/recordian/_native/librecordian_core.json
```

可显式选择其他目录（例如隔离测试或一个已安装的 Python 包）：

```bash
python scripts/build_native_core.py --output-dir /path/to/staging/_native
```

脚本只使用 Python 标准库。实际执行 Cargo 的 `build --offline --locked --release`，
从 Cargo 的结构化产物信息取得 `.so` 和 `.rlib` 路径，支持 `CARGO_TARGET_DIR`，
不会挑选残留旧库。`rlib` 保留在 Cargo 的构建目录中，不放进 Python wheel。

构建完成后，脚本把 `.so` 复制到目标文件系统的临时目录，在独立 Python 进程中加载它，
调用 `recordian_core_abi_version()` 并确认返回 1，随后使用原子 rename 替换目标库。
Cargo 失败、库无法加载、ABI 不匹配或构建期间源文件发生变化时，不替换旧库。
原子替换不会截断已被进程映射的旧 inode；已经加载库的进程仍需重新启动才能采用新库。
禁止在音频回调、应用启动的自动加载路径或第一次 D-Bus 调用中编译或安装。

JSON 元数据包含库的 SHA256、ABI、release profile、crate 类型、Rust 编译器版本/host/commit，
以及 `Cargo.toml`、`Cargo.lock`、Rust 源码的逐文件 SHA256。源码键是项目相对路径；
不写入作者、主机用户名、绝对源码路径、环境变量或密钥。元数据是追溯记录，不是运行时加载器的签名验证。
库与 JSON 各自通过原子 rename 发布，两个文件不构成跨文件事务；审计时必须重新计算库 SHA256 并核对 JSON，
如出现不一致应停止部署并重新构建，不能只看元数据断言当前库有效。

## Python 加载模式

| 环境变量 | 行为与用途 |
| --- | --- |
| `RECORDIAN_NATIVE_CORE=auto`（默认） | 尝试包内 `_native/librecordian_core.so`；库不存在、无法加载或 ABI 不兼容时使用 Python |
| `RECORDIAN_NATIVE_CORE=required` | 原生路径必须可加载；不可用时抛错，不静默降级。用于部署与原生验收 |
| `RECORDIAN_NATIVE_CORE=python` | 显式使用 Python 路径，便于对照和回滚 |
| `RECORDIAN_NATIVE_LIBRARY=/path/to/librecordian_core.so` | 覆盖库位置，主要用于隔离测试；部署通常使用包内库并清除此覆盖 |

例如，在使用相同源码的环境中检查实际加载结果：

```bash
RECORDIAN_NATIVE_CORE=required python -c 'from recordian.native_core import status; print(status())'
RECORDIAN_NATIVE_CORE=python python -c 'from recordian.native_core import status; print(status())'
RECORDIAN_NATIVE_CORE=required RECORDIAN_NATIVE_LIBRARY=/path/to/staging/_native/librecordian_core.so \
  python -c 'from recordian.native_core import status; print(status())'
```

生产服务应在启动环境明确配置 `required`，并在进入录音/请求处理前做预检。
一个解释器的预检不能代替另一个服务环境的预检；桌面服务与 ASR 服务都要检查。
加载模式只决定使用哪套实现，不允许在一次可能已生效的 D-Bus 写入之后切换传输并重试。

## Wheel 与源码分发

普通打包不调用 Cargo；仅在**包构建时**设置 `RECORDIAN_BUILD_NATIVE=1` 才自动构建：

```bash
python -m pip install build
python -m build --sdist
RECORDIAN_BUILD_NATIVE=1 python -m build --wheel
```

普通 wheel 若发现源码包内已经有构建好的 `.so`，会带上它并使用平台标签。
要构建无原生库的 Python wheel，可从源码 sdist 的干净解压目录执行 `python -m build --wheel`，不设置构建变量。
构建目录中的残留原生文件不会被带入后续的纯 Python wheel。

包含 `.so` 的 wheel 使用 `py3-none-linux_<arch>`，同时设置 `Root-Is-Purelib: false`；
它通过 C ABI 调用，不绑定某个 CPython 小版本，但绑定 Linux 架构和构建系统的动态库兼容性。
它不是 `py3-none-any`，也未经过 auditwheel 认证，不能当作 manylinux 通用分发。
应在目标兼容环境构建并加载验证；不支持通过此脚本交叉编译后免验证发布。

sdist 包含构建脚本、Cargo manifest/lock、Rust 源码与 Rust 测试，
排除 `target/`、打包 `.so`、原型目录与原型二进制。
从 sdist 构建原生 wheel 仍需要上述系统依赖；离线要求针对 Cargo，不包括 Python 构建依赖的下载。
安装已经构建好的 wheel 不运行 Cargo。Editable 安装若设置构建变量，则在打包阶段将库生成到源码 `_native/`。

现有 `release.yml` 没有启用原生构建变量，仍沿用普通发布流程。
`native.yml` 负责原生打包和回归验收，不会自动将原生 wheel 发布到 PyPI。

## 本机已测结果（2026-10-09）

生产 Python 调用点的音频转换 + RMS 微基准：每块 160 ms / 2560 samples，
每轮 200 块，共 11 轮。下表来自本机测量交接；原始证据保留在仓库外的本机状态中，
具体位置与完整运行条件由最终验收摘要汇总，不提交原型二进制或含本机信息的数据报告。

| 实现 | 中位耗时（每块） | 测量范围 |
| --- | --- | --- |
| Python | 491.42 µs | 456.39–666.17 µs |
| Rust | 10.35 µs | 10.17–10.59 µs |

这是本机 CPU 内核及其 Python 桥接调用的结果，只覆盖转换与 RMS，
不代表用户端到端速度、ASR 模型/GPU 性能、D-Bus 延迟或自然麦克风体验。
集成前的全回归基线为 **1205 passed、40 skipped、2 warnings，40.75 s**；
这是基线，不能写作本次 Rust 集成后的最终回归结果。
随后更新代码的全回归结果如下，来自负责验收的父任务交接（2026-10-09）：

| 运行模式 / 验收 | 结果 | 耗时 |
| --- | --- | --- |
| Python 模式全回归 | 1319 passed、41 skipped、2 warnings | 42.92 s |
| required-native / Rust 全回归 | 1319 passed、41 skipped、2 warnings | 43.49 s |
| 服务端真实编译 ABI 专项验收 | 108 passed | 约 12 s |

`src/` 与 `tests/` Ruff 检查也已通过。两条 warning 为既有的未知 `integration` marker 与
PyGI 弃用提示；41 项 skip 包含新增的隔离 GTK opt-in；其余与原有环境边界保持一致，不能算作通过的用例。
原生/服务端专项结果验证相应契约，不能用全回归总时间推算用户速度。
同一套本机 C++/CUDA 模型以 160 ms 速率接收四段公开中英文 PCM；旧服务基线与 Rust 候选的最终文本完全一致，均收到 final reset 并以 1000 正常关闭。输入样本完整性通过 SHA256 和样本数核对。

| 公开样本 | 首字差值 | EOS 到最终结果差值 | 可对齐前缀字符的最大差值 |
| --- | --- | --- | --- |
| 短英文 | -10.0 ms | -4.3 ms | -10.0 ms |
| 短中文 | -7.8 ms | -2.2 ms | -1.7 ms |
| 长英文混合（17.52 s） | +3.7 ms | +71.3 ms | +61.2 ms |
| 长中文混合（18.40 s） | +8.2 ms | +21.9 ms | +147.3 ms |

差值为候选减基线，客户端每 4 ms 轮询；字符比较使用双方已显示且与最终文本匹配的前缀，排除仅在 EOS 出现的字符。这是每段各一次的有限公开样本验收，未测 p99 或长期抖动；通过本次冻结的首字 +50 ms、尾部 +100 ms、可对齐字符 +200 ms 门槛。

隔离 GTK/Fcitx 测试使用生产 Rust client，验证 Unicode preedit、只提交一次、取消与失焦拒绝。追加的真实模型用例把公开英文音频交给生产 realtime worker，再通过 Rust 输入到真实 GTK 文本框：最终缓冲精确等于原内容加一次最终文本，并观察到至少两个不同 preedit 状态。该用例通过（1 passed，9.45 s）；它使用受控音频输入，不等于自然麦克风或所有用户应用的兼容性验收。

## 验收命令与边界

先构建，再验收；不要用缺少原生库而跳过的测试作为原生验收依据：

```bash
cargo test --manifest-path native/recordian-core/Cargo.toml --offline --locked
cargo clippy --manifest-path native/recordian-core/Cargo.toml --offline --locked --all-targets -- -D warnings
python scripts/build_native_core.py
RECORDIAN_NATIVE_CORE=required python -c 'from recordian.native_core import status; s = status(); assert s["backend"] == "rust" and s["abi"] == 1, s'
RECORDIAN_NATIVE_CORE=required python -m pytest \
  tests/test_native_core.py tests/test_native_bus_transport.py tests/test_native_server_buffer.py -v
RECORDIAN_NATIVE_CORE=required python -m pytest tests/ -v
```

隔离 D-Bus 测试依赖 `dbus-daemon` 和系统 `/usr/bin/python3` 的 PyGObject；
CI 安装 `dbus`、`python3-gi`、`gir1.2-glib-2.0`。测试只操作私有总线，不向桌面输入窗口写入。
CI 使用 Python 3.10 与 3.12，不把本机 Python 3.14 专有依赖问题作为原生验收环境，也不以此修改或绕过回归断言。

真实 GTK/Fcitx 验收是单独的 opt-in：在 `native.yml` 手动运行时选择 `rust-gui`，
才额外安装 GTK、Fcitx 开发包、Xvfb、xdotool 与 CMake，并运行
`RECORDIAN_RUST_GUI=1 RECORDIAN_NATIVE_CORE=required python -m pytest tests/test_rust_fcitx_runtime.py -v`。
该测试使用私有显示、私有总线和隔离配置；普通 CI 不要求 GUI 环境，不改变既有环境 skip 的处理方式。
本机隔离窗口结果见上表与说明；GitHub 手动 opt-in 的 GUI job 尚未运行。

`native.yml` 还验证失败构建/错误 ABI 保留旧库、元数据哈希、sdist 排除规则、
从 sdist 构建的原生与 Python wheel 标签、安装后的库加载以及显式 Python 回滚。
这些检查只证明相应构建和接口契约。最终验收还需要：

- 对量化、非有限值、部分帧、原 RMS 语义与有界 FIFO 做 Python/Rust 对照。
- 验证同 token 有序提交、唯一 Fcitx owner 固定、超时与不确定回复不重放。
- 验证已接纳音频在 EOS 前完整处理，取消与预算归属不变。
- 使用有公开许可证的短、长音频，在真实模型和隔离输入窗口上测量生产 Python/Rust 桥接路径。
- 记录输入文本、错误与尾部延迟的等价性，并说明测试音频与自然麦克风使用之间仍存在的差距。

原型独立耗时、单次加载成功、私有总线测试或一套 CI 通过都不能替代最后两项。

## 部署与回滚交接

部署前保存原提交 SHA、旧原生库与其元数据、桌面/ASR 服务配置及环境；
原始原型归档保留在仓库外，不把模型、录音、二进制或带本机信息的报告盲目提交。

审核并合并通过验收的代码后，分别找到桌面与 ASR Python 环境的包目录。
可使用各环境的解释器定位目标：

```bash
/path/to/runtime/bin/python -c 'import recordian; from pathlib import Path; print(Path(recordian.__file__).parent / "_native")'
/path/to/runtime/bin/python scripts/build_native_core.py --output-dir /path/to/runtime/site-packages/recordian/_native
```

每个环境先执行 `required` 预检并核对元数据哈希，然后配置服务环境，
由负责部署的操作者重启已授权的桌面与 ASR 服务，核对新 PID 的实际库映射和请求行为。
仅源码目录有 `.so` 不代表已安装的服务使用了它，预检也不代表已有进程切换了库。

回滚时先停止相关服务，将它们的启动环境设为 `RECORDIAN_NATIVE_CORE=python` 并清除测试库覆盖，
按预留的提交和配置恢复，再由部署操作者启动服务并核对 Python 状态与实际听写。
需要恢复旧原生实现时，使用已保存的库/元数据及匹配源码，重新执行 ABI 和 SHA256 校验后再启动。
不要在已经提交或回复不确定的输入会话里重放写入来验证回滚。


## 本机交付记录（2026-10-09）

[PR #2](https://github.com/zz8011/Recordian/pull/2) 的代码经独立审查后已合并，合并提交为 `3979f7909151253146cd1c3bddff8a35f884cd87`。PR 最终代码 head 为 `e6b30da110bfcb7cda9444c69ae43773ad0ad774`；普通 CI 的 Python 3.10/3.11/3.12 和 lint，以及原生 CI 的 Python 3.10/3.12 均通过。第三方 Cursor 两个检查给出 neutral，不能写作安全审查通过。

本机已从合并后的源码构建 release ABI1 库，核对库与逐文件源码哈希。桌面与 ASR 两套解释器 required 预检通过；两个服务都配置为 required 并已重启。ASR 服务通过 PYTHONPATH 使用同一源码包，桌面沿用其已有 editable 安装。实际 ASR 进程与桌面 hotkey 进程都映射新库，服务 active 且无自动重启；桌面为 idle，Rust transport 对当前桌面 Fcitx Ping 成功。

原型已在仓库外完整归档并逐文件校验。旧提交、服务配置、启动脚本和恢复说明均保留。交付后的运行请求验收与证据放在本机状态目录，不提交密钥、模型或音频；自然麦克风及其他应用的长时间观察由 beads 中的后续验收项跟进。


重启后四段公开音频再次得到相同最终文本，并收到 final reset / 正常关闭；EOS 到最终结果为 62.1、62.1、149.1、142.9 ms。合并后源码与正式服务上的真实模型 → GTK 用例再次通过（1 passed，9.22 s）：91 字符、18 个 preedit 状态、精确一次最终追加。本次 Rust 工作树已归档并清理；其他历史 worktree 与既有 stash 保留。交付项 `Recordian-89` 完成，自然麦克风与日常应用长期观察记录为 `Recordian-90`。
