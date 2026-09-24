# Failure Ledger

## 2026-09-13 语音输入「总是报错 / 后端停止」，却查不到任何错误日志
- 现象：用户反馈语音输入经常报错、托盘后端偶尔变回「启动后端」；journald 里只有 `result text=...`，零条 error。
- 根因（逐条复现过的证据）：
  1. `enable_text_refine=true` 时精炼 100% 失败：`refine_api_model=Qwen3.6-35B-A3B-UD-Q4_K_XL`（`http://192.168.5.111/v1`）没有运行实例 → HTTP 404 `Model not found or no running instances available`（直接调用 `CloudLLMRefiner.refine()` 复现）；`_run_refinement` 只发 `log` 事件，采样记录里表现为 `refine_latency_ms≈10ms` 且文本无变化。
  2. 报错本身不可见：`setup_logging()` 全项目无人调用（`~/.local/share/recordian/recordian.log` 恒为 0 字节）；托盘 `_handle_event` 的 `error/busy/backend_exited` 分支只更新 overlay 不落日志；录音 ffmpeg 在 monitor 模式下 `stderr=DEVNULL`。→ 事后完全无法定位，只能重启。
  3. 「后端停止」：`exit_hotkey=<ctrl>+<q>` 是全局监听，任何窗口按 Ctrl+Q 都会让 `hotkey_dictate` 正常退出，而托盘不自动重启（菜单只变成「启动后端」）。
  4. 远端 ASR 无重试：`HttpCloudProvider.transcribe_file` 直接 `raise_for_status()`，GPUStack 实例冷启动/重启期间一次失败就丢掉整句（实测首请求 17.7s 冷启动，之后 4.2s）。
  5. 托盘无单实例保护：`recordian-launch.sh` 无锁，第二个托盘的 `BackendManager.start()` 会先 `_cleanup_orphan_recordian_recorders()` 杀掉第一个托盘的后端。
- 规则：
  - 任何 `error` / 后端退出事件必须落 journald 与 `recordian.log`（托盘统一打 `[recordian-tray]` 前缀）。禁止「只在 overlay 上显示、事后查不到」的错误。
  - 全局退出热键禁止用 `Ctrl+Q` 这类日常组合（现为 `<ctrl>+<alt>+q`）；后端非预期退出必须自动重启（退避 1/2/4/8/15s，用尽后桌面通知），主动停止/重启不得触发该逻辑。
  - 托盘必须有 flock 单实例锁（`tray.lock`）；改动「杀孤儿进程」逻辑前先确认不会误杀别的托盘实例的后端。
  - ASR 瞬时失败（断连 / 5xx / 404 no running instances）必须重试一次再报错，错误文案要是可读中文（overlay 只显示前 72 字符）。
  - 回归测试见 `tests/test_runtime_recovery.py`。

## 2026-09-13 文字精炼全线飘红（补记）
- 证据链：
  1. 采样记录（`refine-samples.jsonl`）里 provider/model 从 2026-07-16 起一直是 `cloud` + `Qwen3.6-35B-A3B-UD-Q4_K_XL`，2026-07-16～09-05 的中位延迟 0.7～1.7s（= 真的在调远端 LLM）；**2026-09-13 当天 15 次全部 ~10ms 返回**（= 请求瞬间失败）。→ 配置没变，是 111 上那个模型实例在 09-05 之后停了。
  2. 实测 `POST http://192.168.5.111/v1/chat/completions`：`Qwen3.6-35B-A3B-UD-Q4_K_XL` 返回 `404 Model not found or no running instances available`（注意与不存在模型的 `404 Model not found` 文案不同 → 模型在 GPUStack 里有注册、只是没有运行实例）；`/v1/models` 当前只有 `gpt-sovits-v2proplus / Qwen3-Embedding-4B / Qwen3-Reranker-4B / mega-asr-vllm / glm-ocr / VoxCPM2-nanovllm-experimental`。
  3. 本机存着的 4 个 `gpustack_*` key 全部没有管理权限（`/v2/models`、`/v2/model-instances` 都是 `total=0`，其中一个 401），SSH 到 111 也 `Permission denied` → **不能从 Recordian 侧拉起实例**，必须在 GPUStack 控制台（http://192.168.5.111/ ，v2.2.3）启动。
- 另一条路（本地 llama.cpp）实测结论：本机 `models/Qwen3-0.6B-GGUF/.../Qwen3-0.6B-Q4_K_M.gguf` 存在，装 `llama-cpp-python==0.3.19`（cu124）后 CPU 推理 0.2～0.5s，但 0.6B 会**把 preset 里的 few-shot 答案原样照抄**（输入「嗯这个这个那个…」输出「先别动，你听懂吗？」）→ 不适合做精炼主力，已按用户要求卸载运行时。
- 顺带修掉的两个真 bug（与上一条同源）：
  - `LlamaCppTextRefiner` 之前**完全不用 preset 规则**（只按关键词挑内置 few-shot），现在走 `create_chat_completion` + 原样渲染 preset + 非 thinking 模式追加 `/no_think`；`chat_format` 改为 `None`（用 GGUF 自带模板），`n_ctx` 2048→3072（intent preset 约 1.4k token，2048 会把正文截断）。
  - 精炼请求的 `stop` 里不能放 `"\n\n"`：Qwen3 会先吐 ` thinking\n\n response`（非 thinking 模式下是空块），按空行截断会让精炼输出整个变空。
- 规则：精炼失败必须报「哪个模型没运行实例 + 去哪儿启动」，不能只抛 `API 调用失败: 404 {...}`；本地精炼只在 preset 真正喂进 prompt 且验证过输出正确时才可当主力。
- **结论/现状（13:37 验证通过）**：在 GPUStack 控制台启动 `Qwen3.6-35B-A3B-UD-Q4_K_XL` 后，`/v1/models` 里出现该模型，`POST /v1/chat/completions` 200/0.15s；`enable_text_refine` 已置 true。实测：
  - preset 自验收样例全过：`我跟你说这个事情吧，就是先别动，你听懂吗？`→`先别动，你听懂吗？`；`你在不在？`→ 原样保留（问句保护生效）。
  - 真实听写样本 0.9～1.8s 输出正常（删口水/去重复、句意不变）。
  - 整链路（用主人声纹样本 wav 走 `run_postprocess_pipeline`，不粘贴窗口）：ASR 4124ms + 精炼 1911ms，热词 `Recording→Recordian` 被纠正且精炼后保留。
  - 后端用「SIGTERM → 托盘自动重启」换配置：日志 `backend_exited code=-15 intentional=False` → `1s 后自动重启（第 1 次）` → `使用预设: intent` + `使用云端 LLM: Qwen3.6-35B-A3B-UD-Q4_K_XL`，全程在 journald 可见。
- 附带修复：`.venv/bin/` 里 55 个脚本的 shebang 还指向已不存在的移动硬盘路径（`.venv/bin/pip` 直接 `exec: ... not found`），已批量改为当前仓库路径，`pip/pytest/mypy/coverage` 均可用了。

## 2026-09-05 流式上屏未生效，还把松键粘贴搞坏
- 根因：Mega-ASR 的 `/api/start|chunk|finish` 是 growing-WAV 重推理，不是真流式；`/api/finish` 500 后连 GPUStack `/v1/audio/transcriptions` 也 500。clipboard/Electron 路径会跳过 live type，松键再走 oneshot。两个 `hotkey_dictate` 同时监听同一热键会粘贴两遍。
- 规则：未验证「松键后只粘贴一次且日志 `asr_path=oneshot`」之前，禁止打开 `enable_streaming_commit` 或填写 `asr_realtime_endpoint`。流式开发必须独立开关且默认关闭；上线前必须 flock 单实例，且不得用 growing-WAV 重推理冒充流式。

## 2026-09-24 Alt 连续听写被 25 秒保护终止
- 根因：为避免 Confucius 单次 30 秒音频预算溢出，客户端提前停止了整个录音；这只解决服务限制，没有实现用户需要的持续采集和内部识别分段。
- 规则：声称 Alt 支持持续听写前，必须用至少 90 秒实时音频验证同一次采集跨多次内部识别分段、持续上屏、末尾仅提交一次；单段预算不得直接终止正常的 Alt 连续录音。

## 2026-09-25 数字规则截断完整数词、指代“一”被当数量、raw 尾段丢 marker 上下文
- 症状：
  - `format_spoken_text('端口一百零二')` = `端口1百零二`，`'编号一百二十三'` = `编号1百二十三`，`'数字三百二十'` = `数字3百二十`；`'一百一服务器'` = `一百1服务器`（后缀只从长数词尾部截了“一”）。
  - 真实 PTT→Fcitx→GTK 把“下一个”上屏成“下1个”（`reports/correction-output-e2e.json` 的 `discovered.ordinal_yi`，`exit_code: 1`）；另一个、上一个、每一个、这一个等同类定位/指代词同样中招。
  - raw seam 丢上下文：`_split_held_tail('比例百分之3')` = `('比例百分之','3')`、`('号码是三')` = `('号码是','三')`、`('号码 三')` = `('号码 ','三')`，跨段后百分比/编号无法与后一段数字合并。
  - `'下午三点十五分'` 全汉字不转；`'九月二十四号'` 只转成 `九月24号`。
- 根因：
  - `_SEQ_RE`/`_SUFFIX_SEQ_RE` 只用 `_POS_DIGIT` 吃数位，放开 1 位后抢先 claim 完整 cardinal 的首字（“一/三”），把后面的 十百千万亿 留给其它 pass；后缀规则的左边界允许从长数词中间起匹配。
  - `_MEASURE_RE` 只看“一+量词”，不看左侧的定位/指代前缀。
  - realtime 的 `_CN_NUMBER_MARKER` 是独立短表（漏 手机号/电话/数字/服务器 等），且不认 ASCII 数字、是/为 连接词和必要空格；`百分之` 分支只认汉字数字。
  - 钟点规则只有 点半/点钟；日期规则只有带年份的 `YYYY年M月D日`。
- 规则（下次直接按此执行）：
  1. marker 前/后的数词必须整段吃满 `_NUMERAL_CHARS`（含 十百千万亿幺），用 `_marker_number_text` 渲染：纯数位串按位转并保留前导零（编号零零一二→0012），否则必须过严格 cardinal（端口一百零二→102）；两侧边界都不许从长数词中间起匹配。
  2. “这/那/哪/每/另/上/下/前/后/头/某/本/此/该/唯/同/么” + 一 + 量词 保持中文；“我有一个文件”“多一个文件”照常转 1 个。
  3. `_split_held_tail` 只 hold 末尾数字 span + 必要 marker（marker 后的 是/为/空格、ASCII 数字也算同一 span），marker 词表必须取自 `spoken_formatting.NUMBER_MARKERS` 单一来源；上限和普通英文单词行为不变。
  4. `HH点MM分` 与无年份 `MM月DD日/号` 结构要前置于 cardinal；一位分钟只有带 上午/下午 等 daypart 时按钟点（“三点五分”仍是小数 3.5），完整年份日期与 leading-zero 编号既有规则不动。
- 复现门槛：先写红灯（`PYTHONPATH=src python -m pytest tests/test_spoken_formatting.py tests/test_continuous_dictation.py -q`），再最小修复，最后跑 `PYTHONPATH=src python -m pytest tests/test_spoken_formatting.py tests/test_text_cleanup.py tests/test_continuous_dictation.py -q` 与 `uvx ruff check src tests`。必须包含“端口一百零二/一百一服务器/下一个/我有一个文件/下午三点十五分/九月二十四号”以及“比例百分之3|5→35%”“号码是三|4→号码是34”的跨段真实 worker 用例；纯函数正例或模块返回值不算验证。
- 附带教训：`test_number_path_is_independent_of_correction_provider` 那类“切换环境变量 + 断言源码不含 provider 字样”的测试没有行为价值，已替换为上述行为用例；不要再用源码字符串断言冒充真实 provider 路径验证。

## 2026-09-25 接口可用和单句纠词通过，不能代表长录音纠词有效
- 实证：公开合成语音走本机 Confucius ASR、生产 worker、111 SemIf 和隔离的 Fcitx/GTK。10.416 秒单次语音能把 jeff 改成 jev；31.248 秒连续语音的第一个提交段含两次 jeff，两处都保持原词，最后一段单次 jeff 才改正。数字、百分数已在同一真实链路转换。
- 直接根因：`_alias_spans` 对同一快照同一个 heard token 使用全局 `len(found) == 1` 过滤。重复软件句两次或三次，新的 corrector 的 `finish` 在 0 ms 返回原文、没有 HTTP；不能只凭 HTTP 200 或猜测等待超时定位。
- 修复边界：按明确分句隔离软件语境；同一处理任务中完全相同的分句可复用判定。混合人名、同分句重复、模型不确定、取消或过期的结果必须保留保护，不能直接解除全局重复限制后无条件替换。
- 独立反例：模型会把“Jeff帮我调试脚本”判成软件；假 Session 固定返回 person 的测试不能证明真实服务保留人名。必须把这个反例和“服务器上的Jeff模型”一起验证，明确软件对象与歧义主语分别处理。
- 验收规则：逐层记录公开音频、ASR 原文、worker 结果和输入框实际内容。长录音样本须足以跨越至少两次内部提交；对每次重复的 jev、端口、逐位数字和百分比计数，不仅比较 worker 与输入框相同。异常第一次的输出和报告保留，不改预期掩盖失败。
