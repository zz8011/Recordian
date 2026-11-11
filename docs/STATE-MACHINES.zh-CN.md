# 隐式状态机清单

> 2026-11-12 审计。目的：让开发者和 agent 改动前知道「哪里是状态机、哪里已被测试钉死、哪里别碰」，不必重新扫一遍代码。

**隐式状态机**：行为取决于多个布尔、Optional、字符串、dict key、`threading.Event` 或 Timer 的组合，转换散落在不同方法、回调、线程里，没有单一 Enum 加转换表；或同一套状态词汇以字符串字面量复制在多处。

**置信度标注**：`已修` 表示本轮改了代码并有回归测试；`确认` 表示读代码确认过矛盾但本轮不改；`推断` 表示审计时读代码推出的，没有复现。行号会漂移，以函数名为准。

## 改动前先看这三条

1. **字符串值就是协议**。`idle/recording/processing/transcribing`、`CommitResult.outcome`、任务状态等值会进入 socket、JSON、日志和测试断言。集中成常量可以，改值不行。
2. **别删「只写不读」的标志**。`worker.session_stale`、`worker.composition_active`、`_RealtimeCommitAccumulator` 在生产代码里没人读，但 `test_continuous_dictation.py`、`test_streaming_preedit.py`、`test_hotkey_dictate.py` 直接断言或构造它们。删之前先改测试并说明原因。
3. **C++ 与 Rust 端只记录**。改 `fcitx/recordian-commit` 要编译并部署插件，Wayland 回显启发式只有真机能验证；改 `native/recordian-core` 要重建 `.so` 和 ABI/SHA 元数据。

## 一、本轮已修的 3 处

| 状态机 | 问题 | 修法 | 回归测试 |
| --- | --- | --- | --- |
| `TrayApp._handle_event` 的 `busy` 分支（`tray_app.py`） | 录音期间 controller 的 `lock` 也被持有，再按热键发 `busy`，托盘状态被降级成 `busy`、overlay 被改成 processing 后隐藏，`stream_partial` 不再刷新 | 状态为 `recording` 时只记日志，不动 status 和 overlay | `test_runtime_recovery.py::test_busy_event_*` |
| 唤醒会话监控的静音阈值（`wake_session_monitor.py` 的 `_maybe_schedule_auto_stop`） | 读 `args.wake_owner_verify`（配置意图），而声纹档案缺失或版本不符时 monitor 已把 `owner_filter_enabled` 置 False；过滤没生效，静音阈值仍被加上 `wake_owner_silence_extend_s` | 改读运行时的 `owner_filter_enabled` | `test_wake_session_monitor.py::test_disabled_owner_filter_does_not_extend_silence_threshold` |
| `_QwenServiceRealtimeSession.finish()`（`providers/qwen_service_asr.py`） | 分块解码循环不检查取消，取消后仍占着单槽服务解码剩余分块 | 每块前检查 `_cancelled` / `_cancel_event`，与入口检查一致，抛 `RuntimeError("...cancelled")` | `test_qwen_service_asr.py::test_session_cancel_during_finish_stops_remaining_chunks` |

## 二、状态词汇：三套同名异义

| 词汇 | 成员 | 主要位置 |
| --- | --- | --- |
| 托盘 `UiState.status` | idle / starting / warming / recording / processing / busy / error / stopped | `tray_app.py`、`tray_menu.py`、`recommended_profile.py`、`native_settings.py`、`tray_settings.py`、`tray_speaker_wizard.py` |
| overlay | idle / recording / processing / error | `waveform_renderer.py`、`orb_shader.py`、`tray_utils.py` |
| daemon / agent phase | idle / recording / transcribing（加 stopped） | `hotkey_dictate.py`、`desktop_control.py`、`agent_entry.py`、`agent_panel.html` |

- daemon 侧的 `transcribing` 对应托盘的 `processing`，映射只在 `hotkey_dictate.py` 的 `desktop_status` 推导处。`test_desktop_control.py` 断言了 `transcribing`，两套词汇不能合并。
- 未知状态的默认行为不一致：`waveform_renderer` 落到 else（等同 idle），`orb_shader` 用 `.get(state, 0.95)`，`tray_menu` 用 `.get(status, "idle")`，`desktop_control` 的 `watch-status` 遇到白名单外的值会抛错并被吞成 `stopped`。新增状态时这几处要一起改。
- `error` 含义不同：daemon 把 `error` 映射成 `idle`，托盘保持 `error`。
- 推荐的集中化方式（未做）：托盘 8 态抽成 `class UiStatus(str, Enum)`，成员值与现有字符串完全一致；overlay 4 态用子集常量。

## 三、录音核心

| 状态机 | 状态编码 | 已知问题 | 测试 | 决定 |
| --- | --- | --- | --- | --- |
| 录音控制器 `voice_*` 标志簇（`recording_controller.py`） | `RecordingState` 枚举外加约 14 个 `voice_*` 键和 `record_source`，在 `state` dict 里 | `确认`：同一个重置块复制 5 份且不一致，stop 路径不重置 `record_source`；`voice_session_active` 与 `record_source=="voice_wake"` 等价；`voice_semantic_*` 除一处读取外无写入逻辑 | `test_duration_guard.py` 较好；无测试断言 `RecordingState` | 记录。合并重置块前先固定「stop 是否重置 `record_source`」的语义 |
| `hotkey_dictate` 的 `desktop_status` 与布尔 dict | 字符串加约 9 个本地布尔 dict，多线程改，无锁 | `推断`：`error` 一律映射 idle，agent 按键报错时普通录音可能仍在进行；`result` 先于 `lock.release` 发出，窗口内 wake 可能得到 busy 并烧掉 cooldown | `test_hotkey_toggle_controls.py`、`test_hotkey_dictate.py` | 不动。不要合并那 9 个布尔 dict，风险高 |
| realtime worker 终态（`realtime_asr.py`、`continuous_dictation.py`） | `outcome` 字符串加 `composition_*`、`session_stale`、`cancel_event`，无锁，靠只 False→True 的约定 | `确认`：注释列 6 个 outcome，实际还会写 `suppressed`；composition 被拒后再 cancel，`outcome` 被覆盖成 `cancelled` 而 `commit_info["outcome"]` 仍是 `suppressed` | `test_continuous_dictation.py`、`test_recording_audit.py`、`test_streaming_preedit.py` | 记录。注意第二节第 2 条 |
| `VoiceWakeService._run`、`_MonitorFanout`、`continuous_dictation` 的 nonlocal 单向状态 | 局部变量或有序优先级 | 无 | 有 | 不动，已够清楚 |

`models.SessionState` 不是状态机：`engine.py` 里的 `state` 是局部标签，没人读，最终必为 `COMMIT`，`IDLE` 和 `STREAMING` 从未使用。不要给它加转换表。

## 四、UI 与 Agent

| 状态机 | 已知问题 | 决定 |
| --- | --- | --- |
| `BackendManager` 重启事件（`backend_manager.py`、`tray_app.py` 的 stopped / backend_exited 分支） | `推断`：`restart()` 排队的旧 `stopped` / `backend_exited` 在新后端预热期间才被处理，托盘短暂写成 `idle/Ready`。修法是事件带 `generation`，但要改事件字典，测试和线上协议都依赖它 | 记录，不动 |
| `AgentHub` 的 `phase` / `capture`（`agent_entry.py`） | `确认`：`begin_capture` 不设 phase，只影响显示。任务状态词汇 `running/completed/failed/cancelled/interrupted` 在 `agent_entry`、`agent_response_overlay`、`tray_app`、`agent_feedback`、`agent_panel.html` 重复；`feedback_message` 把未知状态当「已停止」，`card_content` 把未知状态当「正在回复」 | 记录。可抽 `TaskStatus` 常量 |
| `AgentResponseOverlay`（`dismissed_id`） | `推断`：两个 agent 并发时，已关闭的卡片会在另一任务事件到达后复现并跳回默认位置。修法是 `dismissed_ids: set`，且仅新 id 首次出现时 `_place()` | 记录，不动 |
| `WaveformRenderer`（`state` / `hide_deadline` / `window.visible`） | `推断`：`error` 状态下 overlay 因 `hide_deadline` 到期隐藏后，tick 仍按活跃帧率空转；窗口隐藏时收到 `processing` 不可见 | 记录，不动 |
| 声纹向导 `wizard_state`（`tray_speaker_wizard.py`） | `确认`：`wizard_state["error"]` 从未被读取，录音线程失败只提示「未采集到音频」；断开旧 handler 的 5 行重复 6 次；无测试 | 记录 |
| commit `outcome` 词汇 | `tray_app` 只认 `{uncertain, stale, suppressed}`，`agent_entry` 还多认 `cancelled`。可能是有意的，改之前先确认意图 | 记录 |

不动：`output_mute`、`http_service`、`remote_paste`、`auto_lexicon._closed`、`error_tracker._initialized`、`claude_code_adapter`、`WaylandBufferedSession.active`、`settings_draft` / `setting_effects`。它们已是显式布尔加锁、Enum，或短生命周期对象。

## 五、输入法提交与流式链路

| 状态机 | 已知问题 | 决定 |
| --- | --- | --- |
| `FcitxStreamingSession` 与 `CommitResult.outcome`（`linux_commit.py`） | `确认`：`cancel()` 在已关闭时返回 `committed=False`（约 462），成功时返回 `committed=True`（约 478），都带 `outcome="cancelled"`；调用方现在都丢弃返回值，所以没出事。降级到 buffered 的判断在 3 处重复。outcome 子集在 5 处各抄一份且不一致（`postprocess_pipeline`、`agent_entry`、`tray_app`、`continuous_dictation`） | 记录。集中常量用普通字符串加 `frozenset`，不要用 `str, Enum`：这些值会被拼进 f-string 和日志 |
| C++ `StreamingSession` / `SegmentAck` / 基线三态（`recordian-commit.cpp`） | `确认`：`ack.poisoned` 是死标志（置位后被紧接着的 `clearPendingAck` 清零，读取点都不可达）；`afterKnown` 等价于 `after.valid`；`"wayland_v2"` 字面量出现 6 次 | **不动** |
| Rust `SessionBus` / `Binding`（`dbus.rs`、`native_bus.py`） | `推断`：无 TTL 回收，调用方遗弃 session 会占满 256 上限后报 `SessionBusy`；`NativeBus._connect_error` 粘性，首次瞬时失败后需重启。Rust、Python、C++ 三层靠错误名同步 | **不动** |
| `StreamingHotwordCorrector` / `ParticleCorrector` | 4 套失效令牌（`dropped` / `_generation` / `_epoch` / `_accepted_id`）全在 `_cv` 下；`_stopped` 恒等于 `_closed`。`jev_judge` 与 `semif_judge` 的请求函数已捕获 OSError、Timeout、JSON 解码错误，所以 `_loop` 里的 `_judge` 不需要额外 try/except | 不动，令牌合并已被大量测试钉死 |
| `ConfuciusRealtimeSession`（`providers/confucius_asr.py`） | `推断`：`_finished` 既表示已请求 finish 又表示已完成；服务端中途 CLOSE 不 `_set_error`，要等下一帧发送失败才报错 | 不动，协议语义被 `test_confucius_asr.py` 钉死 |
| `_QwenServiceRealtimeSession`（`providers/qwen_service_asr.py`） | `_finished` 与 `_finishing` 总是同时置位，冗余；`finish()` 不等在途 preview POST，最长占槽 20 秒 | 取消检查已修；冗余标志与占槽不动 |
| `server/confucius_streaming_server.py` | `terminal["kind"]` 的 `"overflow"` 从未被赋值；`total_new_asr_tokens` 从未 append，`_adapt` 里的 `last_is_chinese` 恒为 False，疑似移植遗漏（`推断`） | 不动，改它会改变解码预算，需真机回归 |

## 六、候选后续（未做，按价值/风险比排序）

1. outcome 与任务状态常量化（普通字符串加 `frozenset`，替换 5 处集合字面量，保持各处语义）。
2. 托盘 `UiStatus`（`str, Enum`，值不变）。
3. 录音控制器 5 处 voice 重置块抽成一个常量 dict（先固定 `record_source` 语义）。
4. `AgentResponseOverlay.dismissed_ids`、`WaveformRenderer` tick 条件、`BackendManager` 事件 `generation`。

## 七、测试怎么跑

在 git worktree 里跑测试要用主仓库的 `.venv`，并设 `PYTHONPATH=src` 才会加载当前 worktree 的源码：

```bash
PYTHONPATH=src <主仓库>/.venv/bin/python -m pytest tests -q -p no:cacheprovider
```

需要真机或额外环境的测试：`RECORDIAN_NATIVE_GUI=1`（`test_fcitx_native`）、`RECORDIAN_RUST_GUI=1`（`test_rust_fcitx_runtime`）。
