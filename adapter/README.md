# Confucius → OpenAI-compatible 转写适配器

让 Orca 桌面听写（OpenAI-compatible provider）直接用本机 Confucius 流式 ASR（`ws://127.0.0.1:8321`）。

`confucius_openai_adapter.py` 单文件，只依赖标准库 + `websockets>=11` + 系统 `ffmpeg`：

- `POST /v1/audio/transcriptions`：multipart `file`（wav/webm/ogg/mp3），可选 `language`（`zh`/`en`/`Chinese`…，空或 `auto` 默认 `Chinese`）；`model` 等其他字段忽略。返回 `{"text": "..."}`。
- `GET /v1/models`：返回 `confucius4-r2t2`。
- ffmpeg 归一为 16k mono s16le，按 160 ms 帧推给 Confucius；>28 s 的音频自动分段（服务单会话预算 30 s），最长 10 分钟。
- Confucius 单并发：适配器内串行，上游忙（CLOSE 4429）时重试 5 次，仍忙返回 503。
- 只绑 `127.0.0.1`，拒绝非回环 `Host`/`Origin`；token 读自 `~/.config/recordian/confucius_server_token.txt`（可用 `CONFUCIUS_TOKEN_FILE` 覆盖），每次请求现读，不落日志。
- 不校验调用方的 API key（仅回环、单用户）。

## 安装（systemd user）

```bash
cd ~/Projects/recordian            # 本仓库检出位置（需已含 adapter/）
mkdir -p ~/.config/systemd/user
cp adapter/recordian-confucius-adapter.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now recordian-confucius-adapter.service
curl -s http://127.0.0.1:8391/v1/models
```

默认端口 8391；冲突时在 unit 里加 `Environment=CONFUCIUS_ADAPTER_PORT=<新端口>`，或手动 `python adapter/confucius_openai_adapter.py --port <新端口>`。

## Orca 设置

Settings → Voice dictation → 选 OpenAI-compatible：

| 字段 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:8391/v1`（Orca 自动补 `/audio/transcriptions`） |
| Model | `confucius4-r2t2`（任意非空，适配器忽略） |
| API Key | 留空（或随便填 `local`，不校验） |

## 回退

```bash
systemctl --user disable --now recordian-confucius-adapter.service
```

停用后在 Orca 里把听写 provider 切回原生模型（zipformer）即可；适配器不改 Confucius 服务与 Recordian 任何文件。

## 已知限制

- 固定 28 s 硬切分段，可能切在词中间（短听写不受影响）。
- 实测服务端 `auto` 语言对有损音频（ogg）会丢尾字，所以默认强制 `Chinese`；中英混说如需 auto 请显式传 `language=zhen`。
- 仓库另有 `server/confucius_openai_bridge.py`（Flask，8322，给 Hermes 用），本适配器与它互不依赖。

## 测试

`pytest tests/test_confucius_openai_adapter.py`（假 Confucius WS 服务，需 ffmpeg）。
