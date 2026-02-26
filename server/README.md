# Recordian 服务器部署指南

在局域网服务器（192.168.5.225）上部署 ASR + LLM 服务，供其他电脑使用。

## 📋 服务器配置

**服务器 IP**: 192.168.5.225

**提供的服务**:
- ASR 服务（端口 8000）：Qwen3-ASR-1.7B 语音识别
- LLM 服务（端口 11434）：Qwen2.5:7b 文本精炼

## 🚀 快速部署

### 方式 1：一键部署（推荐）

```bash
# 在服务器上执行
cd /path/to/Recordian
sudo ./server/setup_server.sh
```

脚本会自动完成：
1. 安装 Ollama
2. 配置 Ollama 监听所有网络接口
3. 下载 Qwen2.5:7b 模型
4. 安装 Python 依赖
5. 下载 Qwen3-ASR-1.7B 模型（可选）
6. 创建并启动 systemd 服务

### 方式 2：手动部署

#### 步骤 1：安装 Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

#### 步骤 2：配置 Ollama

编辑 `/etc/systemd/system/ollama.service`，在 `[Service]` 下添加：

```ini
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

重启服务：

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
sudo systemctl enable ollama
```

#### 步骤 3：下载 Ollama 模型

```bash
ollama pull qwen2.5:7b
```

#### 步骤 4：安装 Python 依赖

```bash
cd /path/to/Recordian
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[qwen-asr] flask
```

#### 步骤 5：下载 ASR 模型

```bash
pip install modelscope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir ./models/Qwen3-ASR-1.7B
```

#### 步骤 6：启动 ASR 服务

```bash
# 手动启动（测试用）
python server/asr_server.py --host 0.0.0.0 --port 8000 --model ./models/Qwen3-ASR-1.7B

# 或创建 systemd 服务（生产环境）
sudo cp server/recordian-asr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable recordian-asr
sudo systemctl start recordian-asr
```

## 🔧 服务管理

### 查看服务状态

```bash
# ASR 服务
sudo systemctl status recordian-asr

# Ollama 服务
sudo systemctl status ollama
```

### 查看日志

```bash
# ASR 服务日志
sudo journalctl -u recordian-asr -f

# Ollama 日志
sudo journalctl -u ollama -f
```

### 重启服务

```bash
# 重启 ASR
sudo systemctl restart recordian-asr

# 重启 Ollama
sudo systemctl restart ollama
```

### 停止服务

```bash
# 停止 ASR
sudo systemctl stop recordian-asr

# 停止 Ollama
sudo systemctl stop ollama
```

## 🖥️ 客户端配置

在局域网内其他电脑上配置 Recordian 使用服务器：

### 配置文件位置

`~/.config/recordian/hotkey.json`

### 配置示例

```json
{
  "asr_provider": "http-cloud",
  "asr_endpoint": "http://192.168.5.225:8000/transcribe",
  "asr_timeout_s": 30,

  "enable_text_refine": true,
  "refine_provider": "cloud",
  "refine_api_base": "http://192.168.5.225:11434",
  "refine_api_key": "dummy",
  "refine_api_model": "qwen2.5:7b",

  "hotkey": "<ctrl_r>",
  "trigger_mode": "ptt"
}
```

说明：
- `refine_api_base` 连接 Ollama 时请使用 `http://主机:11434`，不要加 `/v1`。
- `refine_api_key` 对 Ollama 无实际校验，保留占位字符串即可。

### 测试连接

```bash
# 测试 ASR 服务
curl http://192.168.5.225:8000/health

# 测试 Ollama 服务
curl http://192.168.5.225:11434/api/tags
```

## 📊 性能说明

### 显存占用

- Qwen3-ASR-1.7B: ~4GB
- Qwen2.5:7b: ~8GB
- **总计**: ~12GB 显存

### 延迟

- 局域网延迟: <10ms
- ASR 识别: ~1-3 秒（取决于音频长度）
- 文本精炼: ~0.5-2 秒（取决于文本长度）

### 并发

- ASR 服务: 单线程处理（Flask threaded=True）
- Ollama: 支持并发请求

## 🔒 安全建议

1. **防火墙配置**：只允许局域网访问

```bash
# 允许局域网访问
sudo ufw allow from 192.168.5.0/24 to any port 8000
sudo ufw allow from 192.168.5.0/24 to any port 11434
```

2. **不要暴露到公网**：这些服务没有认证机制

3. **定期更新**：保持模型和依赖最新

## 🐛 故障排查

### ASR 服务无法启动

```bash
# 查看详细日志
sudo journalctl -u recordian-asr -n 50

# 检查模型路径
ls -la ./models/Qwen3-ASR-1.7B

# 检查 GPU
nvidia-smi
```

### Ollama 无法访问

```bash
# 检查服务状态
sudo systemctl status ollama

# 检查监听端口
sudo netstat -tlnp | grep 11434

# 测试本地访问
curl http://localhost:11434/api/tags
```

### 客户端连接失败

```bash
# 检查网络连通性
ping 192.168.5.225

# 检查端口
telnet 192.168.5.225 8000
telnet 192.168.5.225 11434

# 检查防火墙
sudo ufw status
```

## 📝 API 文档

### ASR 服务 API

#### POST /transcribe

**请求**:
```json
{
  "audio_base64": "base64 编码的 WAV 音频",
  "hotwords": ["可选的热词列表"]
}
```

**响应**:
```json
{
  "text": "识别结果",
  "confidence": 0.95,
  "model": "Qwen/Qwen3-ASR-1.7B"
}
```

#### GET /health

**响应**:
```json
{
  "status": "ok",
  "model": "Qwen/Qwen3-ASR-1.7B",
  "device": "cuda:0"
}
```

### Ollama API

参考 [Ollama API 文档](https://github.com/ollama/ollama/blob/main/docs/api.md)

## 🎯 优势

1. **集中管理**：模型只需在服务器上维护
2. **资源共享**：多台电脑共享 GPU 资源
3. **降低门槛**：客户端无需 GPU
4. **统一更新**：模型更新只需在服务器操作

## 📞 支持

如有问题，请查看：
- [Recordian 主文档](../README.md)
- [配置指南](../docs/)
- [GitHub Issues](https://github.com/zz8011/Recordian/issues)
