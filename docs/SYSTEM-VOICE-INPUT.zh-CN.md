# Recordian 系统默认语音输入与模型运行方案

日期：2026-09-27。已完成本机默认语音入口替换、录音静音恢复、常驻服务配置；已按用户选择切换为官方 GGUF Q4_K_M + Q8 音频编码器，使用 llama.cpp CUDA 常驻流式推理。

## 本轮后续修复与比较（2026-09-27）

Q4 的 Python 日志回调现已在解释器清理前解绑，真实测试服务两次正常退出，正式服务也完成识别、正常退出与重新启动。原进程卸载时的一次已知旧崩溃保留证据，修复后未发现新 core。隔离输入测试现在于私有 D-Bus 启动之前设置专用门户配置，不再启动临时 Hyprland 门户；系统真实门户未重启。

同一组 8 个样本（66.58 秒音频）比较：native Q4 / hybrid Q4 / audio.cpp Q8 的进程显存采样峰值分别为 2488 / 3932 / 3272 MiB；20.22 秒中文收尾分别约 200 / 105 / 394 ms。hybrid 在一段英文长句中比 native Q4 少一处词语错误，但耗用更多显存。中英拼接样本三者都漏掉英文段，不能当成完整中英能力验收。

保持 native Q4 为正式后端。audio.cpp 主分支已列社区 Q4 支持，但本轮发布版 CUDA 程序仍拒绝该 Q4 权重，未得到推理结果。139 项服务/客户端测试通过；35 秒隔离 Codex 上屏通过。额外 GTK 光标点击/重置保护仍有三项失败，本轮未解决，完整证据在输出报告，不声称所有测试通过。

## 已生效的系统接管

- Voxtype 用户服务已停止并禁用自动启动。软件和模型文件保留，可回退。
- F9：按住开始录音，松开结束，已从 Voxtype 改接 Recordian。
- Super+Ctrl+X：开始/结束切换，已从 Voxtype 改接 Recordian。
- Super+Alt+D：原旧式离线听写入口改接 Recordian 开始/结束。
- 右 Ctrl 按住说话、右 Alt 连续录音切换保持可用。
- Omarchy 状态栏的语音指示器改读 Recordian 状态，显示录音/识别；点击使用 Recordian 切换控制。采用用户插件副本 `zz8011.indicators`，未改 `/usr/share/omarchy`。

## 录音静音

桌面服务启用 `RECORDIAN_MUTE_OUTPUT=1`。在当前按住说话/切换录音控制器中，开始采集前保存并静音现有音频输出，停止采集后立即恢复，不必等待识别结束。麦克风和音量数值均不修改；本来静音的设备保持静音。

开始录音失败、停止录音失败和正常停止都执行恢复。进程退出后的服务清理和下次启动前清理会读取恢复记录；锁保证清理程序不会打断仍在录音的会话。恢复只针对同一设备编号和名称，避免误操作新设备。录音中刚热插入的新输出不在开始时的快照内。

## 常驻与启动

`recordian-confucius-asr.service` 已启用到用户 `default.target`，本机 `Linger=yes`，用户管理器在系统启动时即可加载模型，不必等首次按录音键。`recordian-desktop.service` 已启用到 `graphical-session.target`，登录桌面后启动托盘/快捷键服务，并等待模型就绪。

当前两个服务 active/running，控制接口 ready/idle，桌面服务重启计数为 0。模型服务已切换为官方 Q4 并重新预热；未为验证而重启整机。

## 验证与范围

- 133 项相关测试通过，3 项原有测试跳过；覆盖静音还原、录音失败、重复停止、恢复锁、部分恢复失败、控制接口、按键状态及 Codex 路由。
- 虚拟 PulseAudio/PipeWire 音箱实测：正常恢复、原本静音保持、失去进程持锁后的恢复通过；实体输出和麦克风的静音/音量状态在测试前后完全一致。
- Hyprland 重载无配置错误；实际按键表 F9 两条按下/松开、Super+Ctrl+X 均为 Recordian。
- 系统状态栏已加载 Recordian 状态读取进程，旧 Voxtype 状态进程消失。
- 没有移动前台鼠标或向真实应用注入测试按键/文字；本轮不重复前台 ASR 测试。真实录音时的音箱效果可在日常使用中确认。

测试期间曾有一次 pytest 进程在断言完成后崩溃（PID 505014，Python 3.14，线程 recordian-wake-）。核心记录指向测试进程中的音频监控/CFFI 调用，主线程正退出；旧控制器测试用空假音频触发真实音频依赖。已在该单元测试文件中隔离真实音频监控，复测正常退出。运行中的 Recordian/模型进程未崩溃；本次测试产生的旧提示已关闭。

## 官方 Q4 部署与实测

当前使用官方 `Confucius4-R2T2-Q4_K_M.gguf` 和 `mmproj-Confucius4-R2T2-Q8_0.gguf`，两文件 SHA256 与官方仓库一致。推理源码固定为 `26d55a54ce5670cff9947a167d8ed95d569fd4d9`，本机为 Python 3.11 重编原生扩展，使用 CUDA 库和兼容本机 CPU 的 AVX2 库。

相同 6.74 秒公开中文样本按真实速度输入：vLLM/Q4 的首字时间为 1.873/1.832 秒，平均分块计算为 73.1/62.6 毫秒，结束收尾为 140/119 毫秒。原 vLLM 进程显存读数 5630 MiB，Q4 的约 35 秒长录音测试采样峰值为 2484 MiB，约少 56%；两者是本轮观测值，不是所有负载峰值保证。

隔离的 Codex 运行时输入测试通过：35.17 秒录音、199 次预编辑更新、2 次分段提交，公开中文短句重复五遍正文完整，实时纠词生效；标点略有差异。138 项服务/客户端测试通过。未移动前台鼠标或向用户真实输入框注入文字，也未用该样本声称完成正式准确率评测。

原 vLLM 启动脚本备份：`.scratch/gguf-q4/vllm-start.before.sh`；当前启动脚本：`~/.local/bin/recordian-confucius-asr-start.sh`。正式监听地址和客户端配置不变。

## 切换前的模型运行调研（历史记录）

本机 RTX 4070 Laptop 8 GiB；切换前 Confucius4-R2T2 + vLLM 的识别进程占 **5630 MiB（约 5.50 GiB）** 显存，是当前时点读数，不是严格峰值。现有启动参数为 0.60 显存比例、4096 上下文、单并发，已限制了服务器默认的大缓存。

### 1. 官方 r2t2_llama：优先做本机对照测试

[官方流式适配文档](https://github.com/netease-youdao/Confucius4-R2T2/blob/master/r2t2_llama/README.md) 明确提供纯 llama.cpp 流式 `stream_llama`，以及保留 PyTorch 音频编码器、替换解码器的 `stream_llama_hybrid`。官方推荐 hybrid 的准确性，原因是纯 llama.cpp 编码器对轻声开头的鲁棒性不及原编码器。hybrid 仍依赖 PyTorch，不能称为完全轻量部署。

[官方 GGUF 权重](https://huggingface.co/netease-youdao/Confucius4-R2T2-GGUF) 提供 Q8 解码器约 1.7 GiB、Q4_K_M 解码器约 1.0 GiB，以及 Q8 音频投影文件约 0.3 GiB / F16 约 0.6 GiB。以上都是磁盘文件大小，不是运行显存承诺。

本机已经有 Q4_K_M（1,107,404,736 字节）和 Q8 mmproj（348,336,544 字节），合计约 1.36 GiB，也有 Python 3.12 的本地原生扩展与一次性识别脚本；可复用做独立实验。但现有 `r2t2-asr` 是逐次加载的一次性入口，不能直接当成 Recordian 的常驻流式服务。

建议首先测试官方 Q8 解码器的 hybrid 与纯 llama.cpp 两条路径，比较中文轻声开头、中英混说、首字延迟、分块耗时、连续一分钟显存峰值；Q4 作为更低占用档继续比较。Q8优先是质量与占用的折中判断，不是已测得最优结论。接入时保留现有 Recordian WebSocket 协议、分段和纠词机制。

### 2. audio.cpp：依赖最简的候选，仍需 NVIDIA 实测

[项目](https://github.com/0xShug0/audio.cpp) 是 C++/ggml 音频运行时，支持 Linux CUDA 构建；[Confucius 流式支持 PR](https://github.com/0xShug0/audio.cpp/pull/604) 已于 2026-09-19 合并。[模型文档](https://github.com/davidxifeng/audio.cpp/blob/40dec7147128649a4180a5c982a7808cd272fc3e/docs/community_models/r2t2.md) 提供边上传 PCM 边返回文字的 live 接口，符合实时输入需求，并非只能处理录完的文件。

它使用 [audio.cpp 专用 GGUF](https://huggingface.co/davidxifeng/Confucius4-R2T2-gguf)：Q8 为 2.31 GiB 单文件，包含必要元数据；当前该模型实现明确拒绝 Q4/Q5，因此不能直接拿本机 llama.cpp 的 Q4 文件通用替换。

作者公开的 Q8 离线 RTF 约 0.13 来自 Apple M3（约 8 倍实时），并报告该机服务模型加载后的 RSS 约 6.77 GiB。这是另一平台的内存/离线性能，不是 NVIDIA 显存或逐块延迟数据，也提醒我们文件小不代表实际驻留一定更小。公开验证主要为少量中英文样本的参考一致性，尚不能代替本机长句和中英混说验证。

### 3. CrispASR：服务集成候选

[CrispASR](https://github.com/CrispStrobe/CrispASR) 及其 [服务文档](https://github.com/CrispStrobe/CrispASR/blob/main/docs/server.md) 提供常驻 C++ 服务和流式 API，并列有 Confucius4-R2T2 适配。它可作为比较对象；目前没有足够的本机同条件测量证明它比前两种更快、更省显存，因此不优先替换当前已可用后端。

## 当前结论

用户选择 Q4 后已完成本机隔离对照测试并切换正式服务。当前优先使用官方 Q4 CUDA，保留原 vLLM 文件便于回退；audio.cpp Q8 和 hybrid Q4 已完成同样本比较，结果见本文开头，正式后端保持 native Q4。

## 文件与回退

配置备份：`/home/zz8011/Projects/recordian/.scratch/system-default-20260927/`（bindings.lua.before、shell.json.before、两个 service.before）。代码新增 `src/recordian/output_mute.py`；控制接口增加 status/watch-status/toggle。

回退时先正常停止 Recordian 桌面服务（自动恢复输出），恢复备份配置并重新加载桌面与用户服务，再恢复 Voxtype 服务的启用状态。不要直接删除恢复记录。原模型权重、原 Voxtype 安装和当前 Codex 输入法配置均保留。
