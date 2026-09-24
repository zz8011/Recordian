# Recordian 流式输入法验证记录

验证日期：2026-09-24。目标平台为当前 Linux/Fcitx5 环境，用户优先场景为中文夹英文，以及浏览器、微信、编辑器。此文件记录可复查的实现与运行证据；任务状态以 beads 为准。

## 本轮实现与分工

协调者负责项目评估、方案、CLI 工作分配、独立反例要求和最终结果审阅。产品代码及测试由现有 token-plan CLI 执行：GLM 5.3 通过兼容 CLI 完成输入法会话、实时 worker 和后处理衔接；Kimi K3 完成 Confucius provider、配置、服务打包、原生应用测试，并交叉审查 GLM 的改动；Grok 4.7 完成热词/SemIf 策略、修饰键修复，并独立审查 ASR 协议和服务端。没有安排 GPT 子代理编写产品代码或测试。DeepSeek dsh 的 token-plan 路由本轮未确认，因此没有调用。

工作从用户原有未提交改动的隔离快照 `e3af4ffa2d32616a753b44cf477eeca8b8adef3a` 开始；这份基线不是本轮新编写的代码。所有优化先在候选分支完成，原工作区的既有内容以 SHA-256 清单核对后再集成。

| 原先的不足 | 本轮实现 | 对使用的影响 |
| --- | --- | --- |
| 即时转写靠退格修订已经提交的正文，光标及焦点变化难以控制 | Fcitx BeginSession / UpdatePreedit / CommitSession / CancelSession，绑定原输入上下文 | 说话时修订预编辑区，正常结束提交一次；用户编辑、失焦或会话不确定时禁止重新找窗口兜底 |
| 累计 WAV 包装与真实增量能力混在一起 | Confucius WebSocket 增量 provider；Qwen transformers 包装如实声明非实时能力 | 依据后端实际能力选择流式路径，避免将失败的部分结果当成成功终稿 |
| ASR 收发、EOS 与异常关闭缺少严格合同 | 独立发送/接收、有限队列、明确错误与取消、最终 reset 和正常关闭联合判断 | 慢网、断连、迟到消息和取消不再冒充完整识别 |
| 同音热词容易无条件替换，模型错误可能积累成词频 | 显式别名和英文写法规整优先，歧义候选有界，自动词库区分来源 | 保留数字、URL、代码及否定表达；模型输出不自动晋升为用户确认词 |
| SemIf 有辅助文件但没有可信的实际判断证据 | 接入真实候选判断、预算、缓存和过期结果失效 | 默认关闭；只在有限候选里选择，失败或分数不足保留原文 |
| 重模型与基础安装耦合、无本机复现路径 | 独立模型环境、轻量可选依赖、本机认证服务、配置样例、构建安装说明 | 可以单独运行本地模型与客户端；私有认证文件放在仓库外 |

## 输入法与真实应用

`kimi-native-r7.report.md` 对应 Fcitx C++ 源码 `956e4e5475dc…`，通过真实构建、私有 Xvfb、私有 DBus 和独立应用配置进行测试。没有使用用户麦克风、重启用户 Fcitx 或操作登录账户。

- GTK：直接读取 [Gtk.Entry::preedit-changed 的当前字符串参数](https://docs.gtk.org/gtk3/signal.Entry.preedit-changed.html)，证实实际输入框在提交前收到非空预编辑；Unicode/中文/英文/Emoji 提交一次；取消清理；重复和无效 token 拒绝；失焦、实际 Reset、用户键入、Ctrl+A、切换输入法使旧会话失效；已有拼音预编辑受保护。
- 热键：按住右 Ctrl → Begin/更新 → 松开 → 提交一次；预编辑中点按右 Ctrl 停止也可提交。普通编辑键仍使旧会话失效。
- 会话连续创建：同一个输入上下文连续 12 次 Begin，旧会话被取代并移除，最新 token 提交一次。
- Chrome 152：实际 textarea 与 contenteditable 均逐字验证中英混输和 Emoji。
- Cursor 3.18.9：实际编辑区，通过已知种子文件、窗口标题和读回值先确认焦点，之后经真实输入法通道提交混合文本，最终逐字相等。
- 微信：本轮未验证，不能用 Chrome 或 Cursor 结果替代。

必须保留两个平台限制。GTK 在失焦或鼠标点击时可能自行把客户端预编辑写入正文；这不是 addon 再次调用 CommitSession。GtkEntry.set_text 的程序化改写实测未发送 Reset/SurroundingText 更新，addon 无法可靠观察该变化。两项均记录在 `Recordian-22t`；测试结果显式标记 LIMITATION/INFO，不改写成 PASS。当前不承诺所有应用都能撤回工具包自行提交的草稿。120 秒 inactivity TTL 的服务端清理经源码审阅，未进行真实等待 120 秒的原生计时验收。

## 交叉审查与反例

Kimi 的独立 IME 审查发现了 UpdatePreedit 回包丢失后残留草稿、空流重试失去原 token、流式 refine 未执行既有清洗规则等问题，已由 GLM 修复。追加的真实 worker/controller/pipeline 组合测试覆盖 BeginSession 尚在飞行时控制器超时：零兜底写入，迟到 Begin 只取消；无组合能力的旧后端仍保留一次整句兜底。来源不兼容的自动词库接口不再收到无 source 的重试。该定向集为 177 passed、3 skipped、0 xfail；最终全量计数另见下文。

Grok 使用真实 loopback 复现并复查 ASR 结束判定。普通 reset 后还有 keepalive 或增量、随后 close 1000，现均报 missing EOF；健康最终 reset + close 1000 连续 40/40 成功。provider 测试 81 passed，包含 10 个真实 loopback，0 skipped。EOS 在 send 返回前已被接收线程处理的确定性竞态也已覆盖。

服务端最初的单元测试未覆盖满队列退出。Grok 独立复现了 receiver 清理死锁、取消 asyncio 等待却提前释放模型占用、warmup 漏掉 auto 路径等问题；修复过程中协调者又发现结束哨兵挤掉已接收音频的反例，退回改为带外结束信号。最终 `grok-server-final-review.report.md` 在同一哈希 `d6225d6d5eef…` 上完成独立复验：

- 容量1/2队列满后正常EOS，模型收到的音频分别为15360/20480字节，与发送内容逐字节相等；正常reset与close1000成立。
- 溢出显式error+close1008，不伪装成截断后的成功终稿。
- 满队列TCP abort和send失败，在真实模型调用返回后释放锁，下一客户端被接纳，服务可退出。
- handler取消期间第二客户端收到4429，模型最大并发深度保持1；旧推理真正返回后才释放占用。
- Chinese/None双warmup、非法/非有限配置、临时端口READY、认证及仓库外0600 token均独立复测。该服务测试为45 passed。

上游 v1 没有独立的 final 消息类型。如果一个在途普通 reset 与正常最终 reset 在消息序列上完全一样，且恰好紧接 close 1000，客户端无法凭该协议区分。已在 provider 文档保留此边界，不能宣称可识别任何故障服务端。

## 真实模型到输入框的完整链路

`kimi-native-r8.report.md` 将已冻结的实际 Confucius 服务、实际 provider、实际 realtime worker、FcitxCommitter 和真实 Gtk.Entry 连在一起。输入是官方6.74秒公开音频，16kHz mono PCM16转float32后按实时节奏回放，没有打开麦克风，没有模拟键入最终识别文字。

- 输入框收到14个不同的非空PREEDIT值，均在最终TEXT提交之前；Python部分结果事件13个仅作为旁证。
- 第一个实际应用预编辑出现在回放开始后1989ms。该数字包含识别、worker和输入法路径，与单独provider首增量1.677秒属于不同测量边界。
- worker最终状态committed、错误为空；输入框最终缓冲区逐字等于已有正文加识别终稿，终稿只出现一次。
- 完整原生套件27个合同场景PASS，0 SKIP，已知GTK程序化改写仍单独为LIMITATION。
- 本轮唯一测试辅助变更是等待异步到达的真实GTK缓冲区事件（最多5秒），没有放宽逐字相等/一次提交/实际预编辑断言；普通pytest不收集该原生辅助目录。
- 服务停止后端口释放、READY文件删除、GPU回到351MiB基线。只清理本轮私有进程和目录。

原生验收日志为逐字核对记录了公开样本转写；服务运行日志不输出转写正文，认证值均未进入日志。这与记录用户的日常语音不同。中文样本成功不能证明中文夹英文识别准确率；混合文本传入Chrome/Cursor的能力另有原生应用用例覆盖。

## 识别速度、模型与部署判断

采用 [Confucius 固定源码 c4611929](https://github.com/netease-youdao/Confucius4-R2T2/tree/c4611929bc3592b38dab34e96a8c9940d6da3755) 和公开权重。本机 RTX 4070 Laptop 8GB 已实际加载模型并输出增量结果。192.168.5.111 是 AMD 平台且运行已有模型，本轮没有向该机器盲目部署 CUDA 环境或替换已有服务。

8GB 实测配置为 gpu_memory_utilization=0.80、max_model_len=4096、max_num_seqs=1、audio limit=1、eager。此前真实模型测量峰值 6836 MiB（包含桌面基线）；停止服务后回到约 351 MiB。

官方6.74秒中文公开样本按真实时间配速输入，最终冻结产品服务（`kimi-packaging-r4.report.md`，auto语言）测得首个稳定增量1.677秒、EOS后49毫秒、14条增量，最终reset+close1000通过客户端合同。较早的Chinese语言产品测试为1.683秒/52毫秒；原型、上游、不同语言和不同版本结果分别记录，不能混成一个性能样本。首个可见结果 p95 ≤800 毫秒仍是优化目标；单个中文样本不证明 p95、噪声条件或中英混输准确率。

当前服务每次音频明确限制 30 秒，超限报错；长篇听写需松键分段，自动分段另记 `Recordian-dfs`。取消不能杀死已经执行的模型线程，服务必须等真实推理返回后才释放单模型占用；这与承诺任意挂死推理都能限时回收不同。

[Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)、[FunASR 两遍识别](https://github.com/modelscope/FunASR/blob/main/runtime/quick_start.md) 和 [sherpa-onnx 解码热词偏置](https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html) 是后续同语料比较路线。当前没有证据证明替换其中任何一个就一定比本机 Confucius 更快或更准。

## 热词与 SemIf 实测

确定性显式替换和英文名称规范化覆盖 9/9 控制例。SemIf 使用 192.168.5.111:42032 的现有服务，实际 health 为 semif-qwen35-4b / semif-direct-options-v1。同一组 12 句中，5 句应改、7 句应留；裸词候选修对 0/5，加入完整候选句后修对 2/5，误改均 0/7。样本很小，不能推广为生产准确率。

因此 SemIf 默认关闭，最高概率至少0.70、领先至少0.20只是保守判定门槛。超时默认120ms、上限350ms，一次快照最多8个选项、词表最多64项、最多一个在途请求加一个最新请求。缓存有界且取消后失效。自动学习只有 user_confirmed 来源计为确认；ASR/refined/corrected 只累计观察次数。详情见 [热词策略](HOTWORD-POLICY.md)。质量语料和延迟分位数工作保留为 `Recordian-cie`。

## 最终仓库检查与版本边界

最终普通回归由Kimi K3在私有Python3.12验证环境执行，确认 `recordian.__file__` 指向候选树。使用真实websockets依赖，协议loopback没有跳过。最后一次在类型整理之后运行：**859 passed、5 skipped、0 failed、0 xfail（37.79s）**。

5项跳过分别为原生GUI入口1项（已经由R8单独实际运行通过）、已移除的semantic helper历史标记3项、尚未实现的旧paste-shortcut功能1项。没有把跳过计入通过。全项目Python覆盖率57%，这不是完整产品体验覆盖率，也不是仅本轮新增代码覆盖率。

`ruff check src/ tests/`全部通过；本轮修改的 `server/qwen_streaming_server.py` 与 `server/confucius_streaming_server.py`也通过。未修改的旧 `server/test_asr_server.py`存在两处基线F541提示，已原样对照确认。

交付检查另发现旧Qwen启动脚本需要继续优先使用已有本地权重。Grok将默认值修正为项目模型目录的绝对路径，保留环境变量及显式命令行覆盖，并在进入项目目录失败时退出。`sh -n`和私有假Python参数探针通过，覆盖带空格的项目路径、环境变量、显式模型、参数转发及目录切换失败；这是启动参数验证，没有再次加载Qwen模型或测量网络行为。原始证据为 `grok-launcher-absolute.report.md` 与对应validation日志。

类型整理补充了动态调用和候选跨度的类型边界，将两处保序去重推导式改成等价循环，并显式标注close code。协调者审阅了这些局部差异，未发现业务行为改变。其后全量回归复验通过；没有重跑GPU性能测量，真实模型/输入框的测量证据仍明确对应R4/R8版本。服务端与C++源码在这一轮没有变化。

当前mypy仍报告10项，不能写成类型检查全通过：缺少requests类型存根3处（含新SemIf导入处）、Gtk动态类型名称6处、旧绘图字体参数1处。CI对mypy配置为continue-on-error。类型报告的临时目录基线没有携带同一份pyproject配置，因此9/10的基线差异不作为最终增量判定依据；最终结论采用当前逐条原始输出。剩余质量工作记录为 `Recordian-ded`。

过程中曾发现一个开发CLI把共享editable导入指向候选树；协调者已恢复源项目映射，并将后续验证切换到独立环境。最终日志记录了私有环境路径和实际导入文件；没有用环境名来代替隔离验证。相关审计保留在运行目录的 `environment-isolation-audit.json`、`environment-restore.log`。

本机未执行Python3.10/3.11的完整CI矩阵，也未宣称云端CI或生产部署通过。模型服务的Python3.11环境已单独完成实际GPU验证。

最终关键文件SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| server/confucius_streaming_server.py | d6225d6d5eef4c39a1fe8bd0e514a8857cd5ba7f8e374bdee8de4124dda9ecdc |
| fcitx/recordian-commit/recordian-commit.cpp | 956e4e5475dcf40057dc21ceb61a6c4ed3ecd1789d4d6615196e5937b69947ef |
| src/recordian/providers/confucius_asr.py | cbf2fbb867d76154c18f97992054782eeceded1a3c7eefe1d17dc4592db4fcb8 |
| src/recordian/realtime_asr.py | dcf2cef0b9efa8b8dbc931ec22daa7e727dcc1420d0a0c3675bde4a39e145aaf |
| src/recordian/linux_commit.py | c48de7cfec6704993630e46314fa37b1eae7ca3f382d7cd6fafe7e86518f16a7 |
| src/recordian/postprocess_pipeline.py | d21dcd5b54335f94f0da979696677c8363c3168eae8f4e05a756b9ccbde8aef4 |
| src/recordian/streaming_correction.py | f2a917e4238f190e170e7ec89aba735c6dc53667557a9365fb4337586a66f55b |

## 使用与证据位置

- 方案：[STREAMING-IME-PLAN.zh-CN.md](STREAMING-IME-PLAN.zh-CN.md)。
- 服务依赖、启动、认证、停止及配置：[server/README-confucius.md](../server/README-confucius.md)。
- 原生插件构建、安装和回滚：[fcitx/recordian-commit/README.md](../fcitx/recordian-commit/README.md)。
- 配置样例：[hotkey.confucius-asr.local.json](../examples/hotkey.confucius-asr.local.json)。样例显式设置 `qwen_language="auto"`、右Ctrl按住说话、右Alt切换录音、自动回车关闭。热键入口是 `recordian-hotkey-dictate --config-path <私有配置>`，不是 `recordian --config-path`。
- 本机原始 CLI 报告、日志、公开样本和模型运行数据在 `/home/zz8011/文档/Develop/Recordian-control/.runs/20260924-streaming/`。认证值和模型权重不纳入 git。
- 本轮尚未安装到用户日常 Fcitx 会话，也没有启动常驻麦克风或输入守护进程；桌面激活与微信体验验收保留为 `Recordian-91k`。

## 当前机器的运行入口

下面使用本轮已经准备好的模型、独立模型环境和私有认证配置。先按上面的Fcitx插件说明构建、安装和重载输入法，再启动服务；本轮验收没有替换用户正在使用的输入法进程。

第一个终端运行模型服务，等待日志出现READY：

```bash
RECORDIAN_RT="/home/zz8011/文档/Develop/Recordian-control/.runs/20260924-streaming/runtime"
PYTHONPATH="$RECORDIAN_RT/pylibs" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  /home/zz8011/文档/Develop/Recordian/.venv-vllm/bin/python \
  /home/zz8011/文档/Develop/Recordian/server/confucius_streaming_server.py \
  --model-dir "$RECORDIAN_RT/models/Confucius4-R2T2" \
  --r2t2-source "$RECORDIAN_RT/Confucius4-R2T2" \
  --token-file "$RECORDIAN_RT/confucius_server_token.txt" \
  --warmup-wav "$RECORDIAN_RT/Confucius4-R2T2/resources/test.wav" \
  --host 127.0.0.1 --port 8321
```

第二个终端运行热键入口，采用同一个token生成的私有配置：

```bash
/home/zz8011/文档/Develop/Recordian/.venv/bin/python -m recordian.hotkey_dictate \
  --config-path /home/zz8011/文档/Develop/Recordian-control/.runs/20260924-streaming/runtime/recordian-confucius.local.json
```

该配置使用自动语言、右Ctrl按住说话、右Alt切换录音、关闭自动回车、关闭SemIf和文本精炼。认证值不出现在以上命令中；配置文件权限为0600。停止时在各自终端按Ctrl+C。当前路径只适用于这台机器；其它安装环境按服务README准备依赖与权重。
