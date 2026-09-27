# Recordian 本机 Omarchy 适配

安装位置：`/home/zz8011/Projects/recordian`。本说明对应 2026-09-27 的本机安装。

## 日常使用

在应用菜单打开 **Recordian**。启动器按需启动本地模型，等待就绪后显示托盘。

- 右 Ctrl：按住说话，松开提交。
- 右 Alt：点按开始连续听写，再按一次结束。
- 默认使用系统麦克风；安装时系统默认是 DJI MIC MINI。
- 输入框保持焦点；文字输入后不会自动按回车。
- 常用词和一般设置通过托盘菜单管理。
- 在本机 Wayland 模式中，按键由 Hyprland 管理，因此程序里的快捷键字段只读。

模型现已设为启动常驻（用户管理器 Linger=yes），桌面服务登录后自动启动。当前使用官方 Q4_K_M + Q8 音频编码器的 CUDA 流式后端；详细实测见 [系统语音说明](SYSTEM-VOICE-INPUT.zh-CN.md)。托盘退出后，如需释放模型显存：

```sh
systemctl --user stop recordian-confucius-asr.service
```

下次从应用菜单启动时会自动重新加载。模型加载通常需要数十秒，以就绪通知为准。

## 安装结构

- `.venv`：本机系统 Python 3.14 桌面环境，复用系统 GTK/PyGObject。
- `.venv-asr`：独立 Python 3.11，torch 2.9.1、vLLM 0.14.0、qwen-asr 0.0.6、transformers 4.57.6、websockets 17.1。
- `models/Confucius4-R2T2`：从旧实验目录补迁的原始权重。
- `runtime/Confucius4-R2T2`：对应推理源码，commit c4611929bc3592b38dab34e96a8c9940d6da3755。
- `~/.config/recordian/hotkey.json`：已迁移并修正路径的配置。
- `~/.config/recordian/auto_lexicon.db`：迁移的词库（包含旧数据库 WAL 中的内容）。
- `~/.local/bin/recordian-confucius-launch.sh`：菜单启动入口。
- `~/.local/bin/recordian-confucius-asr-start.sh`：模型服务入口。
- `~/.local/bin/recordian-control`：桌面热键调用的用户级控制入口。
- `~/.config/systemd/user/recordian-confucius-asr.service`：开机常驻模型服务。
- `/usr/lib/fcitx5/librecordian-commit.so`：针对本机 Fcitx5 重新编译的 C++20 插件。

服务只监听 `127.0.0.1:8321`；口令在用户配置目录内，权限 0600。普通听写默认关闭远程 SemIf 纠词、文本润色、语音唤醒、远程粘贴和额外纠词样本采集。

必要适配：Ayatana 托盘支持、Tk 系统库、Hyprland 原生按下/松开绑定、用户私有控制 socket、Fcitx 新版 C++20 构建，以及设置窗口浮动和高分屏测试。

旧虚拟环境备份在 `.scratch/migration-backup/venv`；旧 `.venv-vllm` 仍保留。其他改动前备份也在 `.scratch/migration-backup`。挂载盘原文件未修改。

## 早期后端对比（Vulkan，历史记录）

同一公开中文样本，使用项目服务的相同 160 ms 分段逻辑，按音频时长实时投喂。长样本由 6.74 秒音频重复三次构成，共 20.22 秒。

| 项目 | vLLM / CUDA | GGUF Q4_K_M / 本机 Vulkan 构建 |
|---|---:|---:|
| 热态短样本平均计算每块 | 48 ms | 82 ms |
| 热态短样本录完后收尾 | 0.087 s | 0.175 s |
| 20.22 s 样本录完后收尾 | 0.155 s | 2.409 s |
| 长样本后的桌面总显存占用 | 6032 MiB | 2733 MiB |
| 热态首个非空结果，从音频起点计 | 约 1.85 s | 约 1.85 s |

两者均识别出该样本中的三次原句。上述首字时间包含音频本身的等待，不能当成纯推理延迟。显存为采样时的整机 GPU 占用，不是进程峰值。这里比较的是本机两套实际构建，不能推广为所有 CUDA/GGUF 后端或所有语音的结论。

以上是早期 Vulkan 构建结果。2026-09-27 改用官方 Q4 的 CUDA 构建后已通过持续流式与纠词测试，并切换为正式默认服务；当前读数见系统语音说明，不能沿用旧 Vulkan 数据判断当前性能。

## 已完成的验证

- 相关自动化测试：343 项通过，3 项跳过。
- 托盘与设置窗口实际启动；高分屏下检查设置界面显示。
- 本机 Fcitx5 插件加载和通信通过；Wayland 与 XWayland 的 GTK 输入框中，预览、分段提交及最终提交通过。
- 真实桌面右 Ctrl 热键 → 录音 → 本地模型 → 输入法 → 输入框的完整流程通过，公开中文样本只提交一次。
- 隔离桌面的连续听写通过：33.7 秒公开音频（同一短句重复五次），录音会话约 35.6 秒，分两段上屏；五次原句完整，无重复提交。
- 默认 DJI MIC MINI 麦克风完成短时采集检查，收到非零音频信号；完整识别测试使用公开样本经虚拟音频输入，未将其当作用户真人说话的准确率测试。
- 本机模型服务、菜单入口、桌面快捷键和输入法插件复核通过。

长样本测试使用隔离显示、输入法与控制入口，避免用户同时操作桌面对焦点或热键造成干扰。没有在微信等所有目标应用中逐一实测；应用是否正确支持输入法，仍会影响预览显示。

GGUF 流式能力参考：[Confucius4-R2T2 官方 llama 推理说明](https://github.com/netease-youdao/Confucius4-R2T2/blob/master/r2t2_llama/README.md)。性能数字来自上述本机实测。

## 排查

```sh
systemctl --user status recordian-confucius-asr.service
journalctl --user -u recordian-confucius-asr.service -n 40
recordian-control ping
gdbus call --session --dest org.fcitx.Fcitx5 --object-path /recordian --method org.fcitx.Fcitx.Recordian1.Ping
```

测试与对比记录保存在项目 `.scratch/`。未在目标应用中实际验证的行为不作兼容性保证。

## 2026-09-27 提示球窗口修复

原先的安装缺少 `Recordian Overlay` 的窗口规则，提示球会参与 Hyprland 平铺布局。已在用户 `~/.config/hypr/hyprland.lua` 中针对其窗口类和标题添加精确匹配：浮动、禁止初始焦点和后续焦点、禁止激活抢焦点，去掉窗口边框、阴影、背景模糊和布局动画。

修复前后的实际桌面检测：修复前提示球 `floating=false`，出现时改变了 Obsidian 的窗口尺寸；修复后 `floating=true`，其他窗口位置和尺寸不变，提示球不会获得键盘焦点。没有关闭输入法防止错窗口提交的保护。

本次补充验证涵盖真正的托盘和提示球；此前只测试后台听写的结果不能证明提示球在平铺桌面下的行为正确。

包含托盘和提示球的补充集成测试在隔离桌面完成：33.7 秒公开音频，录音会话约 35.7 秒，五次原句全部识别、两段提交，无重复。隔离桌面用于避免用户同时操作其他窗口干扰焦点；用户实际目标应用的兼容性仍需针对该应用确认。

## Codex 兼容输入与多屏提示球

1. 未开启 Wayland IME 的 Codex 使用整句剪贴板兼容输入。运行中的 `chatgpt` / `codex` 进程带 `--enable-wayland-ime` 时，保留 Fcitx 流式预编辑与纠词路径。判断读取实际运行进程，兼容 Electron 将参数改写成单一进程标题的情况；只保存启动配置但未重启不会误切换。
2. 提示球原来只在托盘启动时选择屏幕，且混用了 XWayland 像素位置与桌面逻辑坐标。现在每次出现后都根据当前应用所在屏幕重新定位、抬到普通窗口上方，保持浮动且不抢焦点。实测 Codex 与提示球都在 monitor 2 / workspace 3，提示球尺寸 280 × 224 逻辑像素。
3. 菜单入口统一启动用户服务 `recordian-desktop.service`，日志持续保存，避免手动菜单启动后日志丢到 `/dev/null`。后续已添加模型开机常驻和桌面登录自动启动。

## 当前使用方式与边界

- Codex 已启用 IME 时：光标放入输入框，按住右 Ctrl 说话，识别假设通过输入法预编辑原位更新，允许替换当前待确认字词；松开后定稿。未启用 IME 的兼容模式仍在松开后整句粘贴。
- 提示球跟随开始听写时的活动应用屏幕，显示在屏幕下方。
- Codex 兼容模式保留短会话时长保护；长篇连续听写仍使用支持 Fcitx 分段会话的应用。不要把其他输入框的长篇测试结果当作 Codex 无限连续输入的保证。
- 结束时如果活动窗口已变化，拒绝粘贴，不自动切回旧窗口。
- 正常配置恢复为系统默认麦克风（DJI MIC MINI）。本次完整识别测试用公开样本注入虚拟音频输入，没有录制用户私人对话。

## 先前兼容模式验证

- Codex 输入框真实语音链路通过：10.19 秒录音会话，输出“之前有顾客自己带酒水也没加收钱或者不让喝”，`wayland-clipboard` 提交成功。
- 提示球浮动、所在屏幕与 Codex 一致、Codex 焦点不变，截图检查通过。
- 相关自动化测试：164 项通过、3 项跳过；新增模块及测试的静态检查通过。

代码与配置备份位于项目 `.scratch/codex-fix/`；原挂载盘项目未改动。

## 2026-09-27 流式恢复验证与限制

- Codex 实际预编辑接口已确认 `preedit=1 frontend=wayland_v2 program=chatgpt`；在真实输入框依次显示“流失”“流式”“流式上屏测试”，随后取消，不发送消息。
- Fcitx 桥接现在区分 Wayland 延迟到达的首次周围文本快照、相同快照回传和实际文字/选区变化。空的 BeginSession 不再额外发送空预编辑。分段提交仍须通过原有回执校验，失焦、键入、reset 和敏感输入限制保留。
- 隔离 Xvfb 与独立 D-Bus/Fcitx 环境的实际语音测试通过：35.71 秒，五次公开样本，实时显示文字并执行测试专用“顾客→客人”纠词，两个分段提交，最终无重复。没有把测试替换规则写入日常配置。
- 153 项相关测试通过，包括编译真实 C++ 策略的回归测试。完整原生 GTK 套件仍有 3 项光标/reset 测试失败；使用修改前的已安装插件复测也出现同样三项失败，不能宣称整个原生套件通过。
- Codex 完整语音测试出现了会话失效，尚未完成最终验收；隔离 GTK 的成功不能替代 Codex 实际语音验收。
- 用户要求测试不得移动前台鼠标或切换窗口。已停止前台测试。按 jev-use 进行后台观察时，Codex 的 AT-SPI 未暴露输入框；Jev 启动器的凭据笔记不可用，未执行 Jev 桌面动作，也未降级到前台动作。
- 后续桌面验证应使用后台语义操作或独立测试环境；现有桌面测试脚本不可直接重跑。证据在 `.scratch/codex-stream/`。

### 17:17 后续修复

用户实际尝试仍出现 `ime_stale`。日志进一步确认：Wayland 的 BeginSession
读到了上轮缓存（50 字节或一个换行），随后 Codex 回报当前空编辑框
（两个换行），触发了错误的变化判定。现在 Wayland 每轮都从绑定后的
首次新协议快照建立基线，忽略 Begin 时的缓存；之后的变化仍受原保护。
102 项针对性回归测试通过。17:17:55 已重启 Fcitx 加载实际安装的新插件，
随后 Recordian 控制接口返回 ready，ASR 服务正常。完整 Codex 实用效果
仍待用户本人操作确认，不允许自动前台测试。

17:05:31 和 17:06:48 的 `python3.14` 转储实际属于 `recordian-tray`。
崩溃线程在 Tk/XIM 的 X11 回调中，另一个线程遇到 X11 I/O 错误；时间正好
对应旧隔离测试显示退出。旧 `Xvfb -displayfd` 选中了桌面显示编号，造成
连接冲突。测试启动器已改为高编号独立显示，桌面 X11 连接已恢复，旧的
Python 崩溃弹窗已关闭。未降级 Python，也未删除项目和聊天数据。

## 2026-09-27 17:35：输入中途断开

17:22:59 的实际中断由 Fcitx 周围文本更新事件触发 `ime_stale`，不是 ASR
服务退出。此前同类 Codex 日志出现从 5 字节/光标 1 到 11 字节/光标 3 的
周围文本变化；首次快照策略没有覆盖编辑器把自身预编辑算入正文的情况。

本轮插件只按本会话实际发出的预编辑历史识别精确回报，保留原插入位置和
正文两侧的校验。它兼容首次快照已经包含预编辑、异步旧回报和空段落布局
换行；分段提交预测排除自己的预编辑，避免将预览重复计算。焦点、按键、
reset、敏感输入、真正的光标/选区/外部正文变化仍会终止会话。

首段尚未提交就意外中断时，最新已识别文本现在保存在 Recordian 的最后
结果中，可使用“复制最后识别文本”；不会自动重试写入其它输入框。

同时将实际运行的输入法交回 `omarchy-fcitx5.service`。先前手动重启留下
服务外实例，系统服务持续争抢 D-Bus 名称；现为一个受管理实例，重启计数
为 0。后续部署插件使用 `systemctl --user restart omarchy-fcitx5.service`。
当前加载文件校验前缀 `b42ffafaa365bb44`，Recordian 控制接口返回 ready。

验证：155 项相关回归通过；真实 C++ 判定策略覆盖自身回报、Unicode 光标、
纠词、延迟回报和拒绝外部编辑。独立 Sway/Wayland、Fcitx、Chromium 输入框
使用公开样本完成 34.96 秒真实 ASR：75 次 composition 更新、两个分段提交，
五次样本文本无重复/丢失，“顾客→客人”在预编辑中生效。测试规则不写入
用户配置。该 Sway 测试未向 Fcitx 转发周围文本，不能代替 Hyprland/Codex
对本次周围文本规则的现场验收；该规则由实际 C++ 回归覆盖。

用户前台鼠标、焦点和 Codex 输入框没有被测试程序操作。jev-use 后台观察
仍缺乏可操作的编辑控件且凭据不可用，未声称真实 Jev/Codex 自动验收通过。

## 2026-09-27 18:35：Codex 中文候选与流式输入兼容通道

用户继续报告原生 Wayland 下无拼音预览、无候选窗口且流式会话失效。
已保存 Codex 专用 XWayland + GTK 3 + Fcitx 启动配置，覆盖上文旧的
原生 Wayland 启动建议。参数为 `--ozone-platform=x11 --gtk-version=3`；
用户桌面入口限定 `GTK_IM_MODULE=fcitx GDK_BACKEND=x11 XMODIFIERS=@im=fcitx`。
必须完整重启 Codex 才生效，用户已授权重启。

本机 Codex 原生运行时在独立 Xvfb/D-Bus 测试编辑器中显示 `ni hao`
和“你好”候选，确认输入成功。空白框与选中旧草稿的实时纠词、分段提交
通过。公开音频 35.17 秒，194 次 composition 更新、两个分段提交，
五遍样本文本完整无重复；临时纠词未保存到用户配置。路由测试 10 项通过。
没有操作用户鼠标、焦点或真实输入框；隔离测试不是实际 Codex 编辑器验收。
重启协调程序只验证新窗口启动配置，不向用户窗口注入文字。
详细证据位于 `.scratch/codex-stream/x11-engine-asr/`。

## 2026-09-27 系统默认语音输入与常驻

Voxtype 服务已禁用，F9/Super+Ctrl+X/Super+Alt+D 改接 Recordian，右 Ctrl/右 Alt 保留。状态栏用户插件副本改读 Recordian。模型服务启用到 default.target（本机 Linger=yes），桌面服务启用到 graphical-session.target，覆盖此前“未添加自动启动”的状态。录音前静音输出、停止后恢复，进程退出由 ExecStopPost 补偿恢复。133 项测试通过、3 项跳过，虚拟音箱集成检查通过。详见 [系统接管与模型运行调研](SYSTEM-VOICE-INPUT.zh-CN.md)。


## 2026-09-27：ZCode 与富文本最终提交修复（用户验收通过）

ZCode 3.14.3 原生 Wayland 下缺少稳定的流式输入通道；应用专用启动器
使用 `--ozone-platform=x11 --gtk-version=3`，并设置
`GTK_IM_MODULE=fcitx GDK_BACKEND=x11 XMODIFIERS=@im=fcitx`。桌面入口统一
调用启动器。该版本启动时会重建 `zcode.desktop`：用户入口采用自定义
Comment，并在仅应用的 XDG_DATA_DIRS 中包含用户数据目录，使其协议注册
保留用户入口。重启后已核验真实进程加载 XWayland、GTK 3 和 Fcitx。

进一步复现“预输入可见，松开录音键后文字消失”：相同 Electron 41.0.3
运行引擎下，普通 textarea/contenteditable 正常，ZCode 所用的
Lexical 0.42.0 编辑器在 Recordian 先清空预输入、再单独提交的事件顺序下
最终状态为空。普通拼音确认正常。插件的 CommitSession 与非空 CommitSegment
现改为先 commitString、后清理自己的 preedit，避免把确认变成取消再插入。
这是通用输入法桥与富文本编辑器的兼容修复，不按 ZCode 名称特判。

三类输入框的最终提交、分段、拼音、选中替换、取消、空提交均通过；
Codex 运行引擎上的相同 9 项复测通过。Lexical 完成 35.21 秒公开音频、
199 次实时更新、两次分段提交，实时纠词生效，内部状态与可见文字一致。
用户随后确认真实 ZCode 中“完全正常”。测试全程使用隔离虚拟显示与私有
D-Bus，没有操控用户前台鼠标或真实输入框。

本轮提交前相关自动检查为 385 项通过、4 项跳过；GTK 原有三个同输入框
光标移动/reset 场景仍失败，另有两个已知工具包限制，不能声称全部 GUI
边界已解决。这个限制与上述松键提交丢字问题分别记录。
