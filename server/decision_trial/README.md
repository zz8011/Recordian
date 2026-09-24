# decision_trial — 通用 SystemOne 模型对照基准

本目录是 Recordian 纠词试点的**通用 SystemOne `/v1/systemone` choice 模型对照**工具，
从 `server/clm_trial/` 迁移而来（旧目录已移除）。它不绑定任何单一服务：同一语料可以对
任何接受产品请求形状的模型运行，用于速度 / 准确平衡选型时的**同请求对照**。

## 文件

| 文件 | 作用 |
|---|---|
| `benchmark.py` | 串行发 role choice 请求、记录完整 payload 与判定重放、输出 JSON/Markdown 报告 |
| `cases.json` | 99 条手写合成中文语料（54 tool / 33 person / 12 unclear；99 条 alt 改写句；schema_version 2） |
| `tests/test_benchmark.py` | 22 条本地检查（不发网络请求），覆盖 payload 唯一性、预算口径、分桶不变量、温度口径、弱拒绝与无效响应的区分 |
| `recompute_report.py` | 从既有报告的 records **离线**重算指标（不发请求），带 source/tool sha256 与 `http_rerun=false` 溯源 |
| `README.md` | 本文件 |

## 语料（`cases.json`）

- 别名设定：常用词 `jev`（软件工具），ASR 常误写成 `jeff`；`alias.heard=jeff`。
- 每条 = 一个句子 + `target_span`（被标记的那一次出现）+ `focus`（产品会发问的小句）+
  `expected`（tool / person / unclear）+ `guard_expected`（结构保护预判）+ `alt`（warm 新句子）。
- 标签只针对 marker 标出的那一次出现；否定 / 元话语 / 引用仍按“所指对象”标注，
  产品是否发问由结构保护单独决定。
- `guard_expected` 是人工预判，运行时会用真实产品函数（`_alias_spans` 等）复核，
  不一致会写进报告的 `guard_check`。
- 全部为合成文本，不含录音、私密文本、真实人名或账号信息。
- **原始准确率排除**：`mixed-05` / `mixed-06` / `guard-repeat-01` 是同一小句内同一别名
  出现多次、无法明确指向哪一次的结构跳过样例，标了 `exclude_from_raw_accuracy=true`，
  不计入原始 role 准确率，但仍保留结构保护重放测试。校验会强制这条规则。
- F3 修正：`multi-03` 加前导词、`mixed-08` 的 alt 改写，保证主轮 99 条完整 payload 唯一，
  且 alt payload 不与任一主轮 payload 相同（不只看 text）。

## 复现命令

```bash
cd candidate

# 1) 只校验语料 + 产品桥接（不发请求）
python3 server/decision_trial/benchmark.py \
    --cases server/decision_trial/cases.json --validate-only

# 2) 看完整请求载荷（不发请求）
python3 server/decision_trial/benchmark.py --cases server/decision_trial/cases.json \
    --endpoint http://HOST:PORT/v1/systemone --dry-run --dry-run-count 3

# 3) 基线：串行、固定顺序、2 轮 + warm 新句子、诊断捕获 3s、不重试
python3 server/decision_trial/benchmark.py --cases server/decision_trial/cases.json \
    --endpoint http://HOST:PORT/v1/systemone --timeout 3.0 --retries 0 --rounds 2 --warm-new-round \
    --budget-ms 350 --label <label> \
    --model-label <model-id> [--model-revision <rev>] [--quantization <quant>] \
    --effective-temperature <value|unknown> [--temperature-source '<source>'] \
    [--runtime-manifest <path>] \
    --output <report>.json --summary-md <report>.md

# 4) 本地检查
python3 -m pytest server/decision_trial/tests/test_benchmark.py -q
uvx ruff check server/decision_trial/

# 5) 口径修正后离线重算既有报告（不发请求，保留原始 payload 与 HTTP 时间）
python3 server/decision_trial/recompute_report.py \
    --source ../reports/<old>.json --output ../reports/<new>.json --summary-md ../reports/<new>.md
```

## 请求与运行元数据

发出的 HTTP payload **只有** `{"state", "questions"}`，与产品
`semif_judge.request_choices` 一致；**不发送**顶层 `temperature` / `model`
（SemIf、Winnow、Decider 对这两个字段的含义各不相同）。

```json
{
  "state": "上一段：<bounded context>\n<focus>\n只判断这个跨度：「<surface>」",
  "questions": {
    "role": {
      "type": "choice",
      "instructions": "判断句子中标记的「{surface}」…语音识别经常把它误写成 {heard}。",
      "criteria": {"tool": "…", "person": "…", "unclear": "…"}
    }
  }
}
```

F1：`heard` 取**匹配到的 surface**（与 `streaming_correction._judge` 的
`heard=surface` 一致），不是词表里的 `jeff`；99 条里有 35 条 surface≠`jeff`。
`--compat-lexicon-heard` 只用于复现修正前的旧证据，报告会标记
`payload_shape=legacy_lexicon_heard` 且参数核对必然出现 mismatch。

每条记录保存完整 `request_payload`（合成、无隐私）与**规范 sha256**
（键排序、无空白、保留非 ASCII 原文），并用 10 项检查逐项对照产品模板参数。

运行元数据由 CLI 记录，全部**不进请求体**：

| 参数 | 含义 |
|---|---|
| `--model-label` / `--model-revision` / `--quantization` | 本次运行声明的模型身份 |
| `--effective-temperature` + `--temperature-source` | 只有**同时**给出数值与来源才算已核实；否则写 `unknown` |
| `--runtime-manifest` | 只记录运行时清单路径与 sha256（例：Winnow 官方校验清单 `release-manifest.json`，manifest 1.0） |
| `--budget-ms` | ask 子集延迟预算，默认 350ms，只统计不注入 deadline |

`temperature_sent` 恒为 `null`。每个模型按**各自发布配置（含各自发布温度）**
对照实际产品行为，不强制统一温度；这不是校准精度比较（`is_calibration_accuracy_comparison=false`）。
已知的发布配置举例：

| 模型 | 发布温度 | 来源 |
|---|---|---|
| SemIf（本机 42032） | 1.0（`softmax(raw logits)`，无温度缩放） | 只读核查 `serve.py` sha256 `0e80ff0b…` + `semif-logits.cpp` sha256 `0c31f418…` |
| Winnow-12B | 1.0（`winnow.temperature`，不是顶层字段） | 官方校验清单 `release-manifest.json`（manifest 1.0，`fitted_posthoc_map: false`） |
| Decider-4b v2 | 1.935（`decider_config.json`，`DECIDER_TEMPERATURE` 可覆盖） | 官方配置 + 模型卡 |

对照重放行为的做法：逐 run 核实该模型的部署配置与发布温度（未核实就写 `unknown`，
报告显著标注），用**同一 `cases` sha256 / 同一 payload 形状 / 同一门槛**重放，再对照各模型在
**各自发布温度**下的实际接受 / 误改 / 延迟。温度不同不是禁止比较的理由（argmax 排序本就不随
温度变化），但对照时必须写明温度；概率值不能跨模型当作相同真实置信度，**相同温度也不意味着
不同模型的概率已校准**。

`comparability.cross_model_gate_comparable` 恒为 `false`：**单个 run 不能判定两个 run 已经可比**，
它只能声明本 run 一侧 `this_run_comparison_preconditions_met`（部署配置与发布温度已核实）。
其他 run 必须各自核实，`reason`/`requirements` 会写明这一点。

## 指标口径

- **原始准确率**：只统计分布合法且有 `choice` 的回答；`probabilities` 缺失/非法时
  `answer_choice=null`（不计正确），只留 `debug_choice` 供诊断。坏分布、错误、
  超时单独计数。头条数字用“可用子集”（排除同小句多目标结构跳过样例），
  同时给出“全部回答”口径。
- **结构保护重放**（`structural_guard_replay`）：用产品函数重放“会不会发请求”。
  产品模块不可用时 `available=false`、每条 `guard_replay=unavailable`、
  `correctness=null`，**不得据此宣称零误改**。
- **概率门槛重放**（`gate_replay_0_70_0_20`）：产品 `interpret_choice` 的 0.70/0.20
  拒绝门槛，是保守拒绝规则，不是校准正确率。顶层 `scope=all_requests_counterfactual`：
  `accept_*` / `reject_weak` / `unusable_bad_distribution` / `no_answer_error` 统计**全部**
  HTTP 记录，**包含结构保护本会跳过的反事实请求**（因此不等于产品路径）；产品路径用并列的
  `ask_subset_*`（只取 `guard_replay=="ask"`）与 `rewrite_replay`，两套口径**不可相加**。
- **重放改写**（`rewrite_replay`）：结构保护 + 概率门槛重放的合成结论
  （`would_replace`），**不是线上真实改写**，没有运行 IME。
- **需要 tool**（`expected_tool`）分成：总 tool 数、`guard=ask` 子集、该子集内四分：
  被门槛接受、`ask_eligible_weak_rejected_n`（分布合法的弱拒绝，产品的合法保守结果）、
  `ask_eligible_rejected_non_tool_n`（person/unclear 合法判定）、
  `ask_eligible_no_valid_verdict_n`（**只计 response 错误 / 坏分布**，不得把弱拒绝算进去）；
  `guard_skipped` 明确标注为“结构保护跳过，不算未修正错误”。
- **person/unclear 负例**（`negatives_person_unclear`）：`ask_eligible_n` 直接按负例记录
  `guard_replay=="ask"` 计数（= 有效判断 + 无有效判定），`n = ask + guard 跳过 + 不可用`；
  合法的弱分布拒绝（`reject_weak`）是“有效判断（分布合法）”的子集，**不得**再加进 ask 分母。
  `effective_wrong_replay_edit_n` 只统计分布合法且门槛接受 tool 的负例；
  `safe_keep_n`（= `kept_correct_n`）是**安全保留**（结构保护跳过 + 合法判定拒绝，含弱分布拒绝），
  它是产品保守门槛兜底，**不算模型识别正确**；`model_correct_abstain_n` 才只数分布合法且门槛给出
  person/unclear 的负例。错误 / 坏分布 `correctness=null`，既不算模型正确弃权，也不算安全保留。
  每轮 `summarize_round` 末尾用 `assert_round_bucket_invariants` 校验上述分桶互斥且可加。
- **预算**（`budget`）：ask 子集里“合法”（ok + 分布合法 + payload 参数核对通过）
  且总延迟 ≤ `--budget-ms` 的条数与比例，及预算内正确热词数、预算内重放误改数、
  超预算数、错误数。延迟同时给成功 / 全部 / 失败三套（含超时真实耗时），
  失败不会被剔除。

## 边界（必须随报告一起引用）

1. 本工具**没有**运行完整流式 correction、**没有** IME、**没有**产品异步 deadline；
   3s timeout 是诊断捕获。所有改写结论都是结构保护与概率门槛重放。
2. 0.70/0.20 是产品拒绝门槛，不是校准正确率；概率不是真实置信度。
3. `effective_temperature=unknown` 时该 run 不满足比较前置条件，报告显著标注 unknown；
   单个 run 不宣布两个 run 可比，只声明本 run 的部署配置 / 发布温度是否已核实。
4. HTTP 错误、超时、坏分布不算正确弃权；`debug_choice` 不计原始准确率。
5. `repeat_identical` 轮延迟含缓存/前缀复用效应，不能当作新句子速度；
   只有 `warm_new` 轮的延迟接近新句子参考。
6. 语料是合成文本；数字/端口/网址由本机规则处理，不在模型评估范围。

## 流式集成探针（`integration_probe.py`）

只调用产品工厂 `corrector_from_args`（本树没有 `create_streaming_corrector`）和真实 `request_choices`。默认源码根是本文件上两级目录的 `src/`（`<project>/server/decision_trial/` → `<project>/src`）。内存里覆盖端点、超时和别名，不读不写用户配置，不打开麦克风或输入法。预热报告未显示 ready 时不发请求。五条用例一起计数，其中带软件上下文的人名句失败也算失败。

```bash
python3 server/decision_trial/integration_probe.py \
  --source-root src \
  --endpoint http://192.168.5.111:42071/v1/systemone \
  --warmup-report reports/decider-warmup-final.json \
  --report-json reports/streaming-decider-probe.json \
  --report-md reports/streaming-decider-probe.md \
  --wait-ready-s 30
```

省略路径时，源码根仍是脚本旁的 `<project>/src`，预热报告和两份输出默认写到当前目录的 `reports/`。`--wait-ready-s` 是等 ready 文件的上限，不是产品纠词超时。
