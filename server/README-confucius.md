# Confucius4-R2T2 本机流式 ASR 服务（单用户 loopback）

`server/confucius_streaming_server.py` 把 Confucius4-R2T2 的官方流式推理
（`r2t2.R2T2ASRModel`，vLLM 后端）包装成 Recordian `confucius-asr` provider
使用的 v1 WebSocket 协议服务。面向**单用户本机**场景：默认只绑
127.0.0.1、单并发、带本地 token 认证、音频不落盘。

上游固定版本：代码 `Confucius4-R2T2 @ c4611929bc3592b38dab34e96a8c9940d6da3755`
（Apache-2.0）；模型权重另受 NetEase Model License 约束（见上游仓库
`MODEL_LICENSE`），两者许可证不同，分发时须分别标明。

## 与上游官方 ws_server.py 的差异（明确声明，不冒充）

- **无 FireRedVAD**（官方 `if True or use_vad` 强制开启）。句段结束由客户端
  EOS（`YOUDAO_ONETIME_ASR_STREAM_EOS`，即松开录音键）驱动，不发 VAD reset。
- 单并发：服务忙时第二个连接得到 CLOSE 4429。
- 本地随机 token 认证（0600 文件），不使用上游仓库的公开 debug 密钥；
  错误密钥 → CLOSE 4401。
- 有界队列（默认 32 帧）+ 单会话音频预算（默认 30 秒，显式拒绝而非 silently
  截断）。注意：provider 客户端的 6 帧发送队列只是客户端背压上限，
  **不等于**网络+服务的总延迟上限。
- 每 chunk 推送的是**回滚后的 fixed 增量**（与官方一致）；末尾标点可能在
  finish 时才提交（auto 语言路径可能不提交末尾标点，上游行为一致）。

## 依赖安装（2026-09-24 实测来源）

模型栈较重，**不要**装进 Recordian 的基础环境；两种方式任选：

1. 复用已有的 Qwen3-ASR/vLLM 环境（本项目验证用的是 torch 2.9.1 + vllm
   0.14.0 + qwen-asr 0.0.6 + transformers 4.57.6 的独立 venv，只读复用）；
2. 或新建独立 venv，经清华镜像安装：
   `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "qwen-asr[vllm]==0.0.6" librosa soundfile "websockets>=13.0"`
   （torch 按目标 CUDA 版本单独装官方 wheel；`websockets.asyncio` 从 13.0 才存在，
   12.x 不可用，见 https://websockets.readthedocs.io/en/13.0.1/ ）。

`r2t2` 包来自上游固定 commit 的源码 checkout，用 `--r2t2-source` 指给服务
即可（或在上游仓库里 `pip install .`）。模型权重（约 4GB
`model.safetensors`）与 Stream-VAD 均可从 ModelScope 获取；本服务不需要 VAD
权重。轻量依赖见 `requirements-confucius.txt`。

## 启动 / 停止 / 连接

```bash
# 启动（token 默认写到 per-user 配置目录：
# $XDG_CONFIG_HOME/recordian/confucius_server_token.txt 或
# ~/.config/recordian/confucius_server_token.txt，0600 且不覆盖已有文件；
# 也可用 --token-file 显式指定到仓库之外的位置。READY 前会做双语言 warmup）
python server/confucius_streaming_server.py \
  --model-dir /path/to/models/Confucius4-R2T2 \
  --r2t2-source /path/to/Confucius4-R2T2 \
  --host 127.0.0.1 --port 8321 \
  --gpu-memory-utilization 0.60 --max-model-len 4096
# 日志出现 [confucius-server ...] READY 127.0.0.1:8321 即就绪
# （--port 0 时 READY 与 ready 文件写的是实际绑定的端口）

# 停止
kill <pid>          # 仅限上面手动启动的进程；前台进程直接 Ctrl-C 亦可
```

Recordian 侧配置（热键守护进程 `recordian-hotkey-dictate --config-path <file>`
加载的 JSON，参考 `examples/hotkey.confucius-asr.local.json`；注意
`recordian.cli` 是 `--wav` 单次识别入口，没有 `--config-path`）：

```json
{
  "asr_provider": "confucius-asr",
  "asr_realtime_endpoint": "ws://127.0.0.1:8321/asr_stream_api_v1",
  "asr_api_key": "<粘贴 ~/.config/recordian/confucius_server_token.txt 的内容>",
  "enable_streaming_commit": true
}
```

token 是本地私有值，不要提交进 git（仓库 `.gitignore` 已对历史默认文件名
`confucius_server_token.txt` / `recordian-confucius.local.json` 做防御）。

单会话音频预算默认 30 秒（`--max-session-seconds`）：流式每 chunk 重喂累计
音频，成本随时长增长，超限服务端显式报错并以非 1000 关闭，**不是**无限长流；
Recordian 的 Confucius 听写客户端会在约 25 秒通过正常停止/EOS 路径收尾，
并通知用户重新按键开始下一段。该限制覆盖按住说话、单击录音和超长固定时长
录音；尚不支持无缝续录。预算按音频采样数计算，与模型推理耗时不同。

## 8GB GPU 实测参数依据（RTX 4070 Laptop, 2026-09-24）

本机桌面启动配置为 `gpu_memory_utilization=0.60` + `max_model_len=4096` +
`max_num_seqs=1` + `limit_mm_per_prompt={"audio":1}` + eager。上游默认
`max_seq_len=65536` 需要 7.0 GiB KV cache，8GB 卡无法启动；默认多模态
profiling 按 21 条音频探测会再吃掉 ~3.3 GiB。

前一轮公开样本在 0.80 档测得峰值 6836 MiB、热态单 chunk 32–137 ms；
随后真实多次听写时，桌面总显存占用达到 7716 MiB，驱动报告仅剩 92 MiB。
初次降到 0.70 后，短公开样本检查测得占用 6002 MiB、可用 1806 MiB；
随后实际连续使用时，占用继续增至 7184 MiB、可用只剩 624 MiB。就绪时或
短样本后的读数不能代替持续使用后的显存检查。

最终本机启动脚本采用 0.60。约 7 秒公开样本检查后，又按真实节奏输入
同一公开音频重复三次形成的 20.82 秒样本：正常完成，输出包含三次原句
（去标点比较一致），完成耗时约 20.88 秒，EOS 后收尾约 83 ms。结束及静置后
显存占用 6082 MiB、驱动可用 1726 MiB。两档读数来自不同负载场景，不能
当作严格性能对照，也不是所有录音的峰值或中英混合准确率保证。

服务程序的内置显存比例默认值仍为 0.80；上述启动命令显式指定 0.60。
本机桌面入口和 `recordian-confucius-asr.service` 的使用方法见
[日常使用说明](../docs/DESKTOP-QUICKSTART.zh-CN.md)。

## 测试

```bash
# 无需 GPU：fake model + 真实 loopback（websockets 缺失时自动跳过 loopback 层）
pytest tests/test_confucius_server.py -v
```
