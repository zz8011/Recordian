# Recordian 跨电脑语音粘贴 Agent 需求文档

## 1. 项目背景

Recordian 是一个语音识别输入工具，支持语音识别后自动将文字粘贴到当前焦点窗口。

**现有问题**：用户使用 Deskflow 在多台 Linux 电脑之间共享鼠标/键盘/剪贴板。当鼠标从一台电脑移动到另一台电脑时，在 A 电脑语音识别产生的文字无法自动粘贴到 B 电脑（鼠标所在位置）。

**目标**：在每台被控电脑上运行一个 Agent，接收主电脑的粘贴命令并自动执行。

## 2. 系统架构

### 2.1 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│  电脑A (主控)                      电脑B (被控)                  │
│  ┌───────────────────┐          ┌───────────────────┐         │
│  │ Recordian         │  TCP     │ recordian-agent   │         │
│  │ - 语音识别        │─────────▶│ - 监听24872端口   │         │
│  │ - 发送粘贴命令    │  局域网  │ - 执行xdotool粘贴 │         │
│  └───────────────────┘          └───────────────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 通信方式

- **协议**：TCP
- **端口**：24872
- **消息格式**：JSON over TCP

## 3. 功能需求

### 3.1 Agent 端 (recordian-agent)

运行在被控电脑上，负责接收命令并执行粘贴。

#### 3.1.1 启动参数

```bash
python -m recordian.remote_paste.agent [OPTIONS]

可选参数：
  --port PORT         监听端口，默认 24872
  --hostname HOSTNAME 计算机名称，用于日志/通知显示，默认从系统获取
  --config PATH       配置文件路径
  --enable-notify     启用桌面通知（默认开启）
  --no-notify        禁用桌面通知
  --log-level LEVEL  日志级别：DEBUG, INFO, WARNING, ERROR
```

#### 3.1.2 配置文件 (agent_config.yaml)

```yaml
port: 24872                    # 监听端口
hostname: "电脑B名称"           # 计算机名称
enable_notify: true           # 是否发送桌面通知
notify_title: "Recordian"     # 通知标题前缀
paste_delay_ms: 100           # 收到命令后延迟粘贴的毫秒数
log_level: "INFO"             # 日志级别
log_file: ""                  # 日志文件路径，空表示输出到stdout
```

#### 3.1.3 接收的命令

| 命令 | 说明 | 请求格式 | 响应格式 |
|------|------|----------|----------|
| paste | 粘贴文本到当前焦点窗口 | `{"action": "paste", "text": "文字内容", "timestamp": 1234567890}` | `{"status": "ok", "hostname": "电脑B"}` |
| ping | 心跳检测 | `{"action": "ping"}` | `{"status": "pong", "hostname": "电脑B"}` |
| status | 查询状态 | `{"action": "status"}` | `{"status": "ok", "hostname": "电脑B", "uptime": 123}` |

#### 3.1.4 执行逻辑 (paste 命令)

1. 接收 JSON 消息
2. 解析 `action` 字段
3. 如果是 `paste`：
   - 将文本写入剪贴板（使用 xclip 或 xsel）
   - 等待 `paste_delay_ms` 毫秒（默认100ms）
   - 执行 `xdotool key ctrl+v` 模拟粘贴
   - 如果 `enable_notify: true`，发送桌面通知显示粘贴成功
4. 返回执行结果

#### 3.1.5 桌面通知内容

- 标题：`Recordian 跨电脑粘贴`
- 内容：`已在 {hostname} 粘贴: {text_preview}`
- 其中 `text_preview` 为文本前20个字符，超出显示 `...`

### 3.2 Recordian 客户端集成

修改 `linux_dictate.py`，添加远程粘贴功能。

#### 3.2.1 新增命令行参数

```bash
--enable-remote-paste      启用跨电脑粘贴功能
--remote-port PORT         Agent端口，默认 24872
--remote-hosts HOSTS       目标电脑IP，多个用逗号分隔，如 "192.168.1.100,192.168.1.101"
--remote-enable-notify     远程粘贴成功后在主电脑也显示通知（默认开启）
--remote-no-notify         禁用远程通知
```

#### 3.2.2 工作流程

在 `transcribe_and_commit` 函数中，commit 完成后：

```
如果 enable_remote_paste 为真：
    遍历 remote_hosts 列表
    对每个IP建立TCP连接
    发送 paste 命令
    等待响应
    记录日志
```

#### 3.2.3 连接策略

- 超时时间：3秒
- 重试次数：0（一次失败则跳过）
- 错误处理：记录日志但不影响主流程（本地粘贴仍成功）

#### 3.2.4 日志输出

```
[RemotePaste] 向 192.168.1.100:24872 发送粘贴命令成功
[RemotePaste] 向 192.168.1.101:24872 发送粘贴命令失败: Connection refused
```

## 4. 技术实现要求

### 4.1 Agent 实现

- 使用 Python 标准库 `socket` 实现 TCP 服务器
- 使用 `threading` 处理多客户端连接
- 使用 `subprocess` 调用 xdotool/xclip 执行粘贴
- 使用 `dbus` 或调用 `notify-send` 发送桌面通知（参考现有 `linux_notify.py`）

### 4.2 客户端实现

- 在 `linux_dictate.py` 中添加 `--enable-remote-paste` 等参数
- 解析 `remote_hosts` 字符串为IP列表
- 使用 `socket` 发送 JSON 命令
- 错误处理：try-except 包装，不影响主流程

### 4.3 依赖

**必须**：
- Python 3.10+
- xdotool（已存在于 Recordian 依赖）
- xclip 或 xsel（已存在于 Recordian 依赖）

**可选**：
- dbus（用于桌面通知，使用现有 linux_notify.py 的实现）

## 5. 文件结构

```
src/recordian/
├── remote_paste/
│   ├── __init__.py           # 模块入口
│   ├── agent.py              # Agent 主程序
│   ├── client.py             # TCP 客户端
│   ├── config.py             # 配置模型
│   └── protocol.py           # 协议定义
├── linux_dictate.py          # 添加 --enable-remote-paste 参数
└── cli.py                    # 添加远程粘贴相关参数

bin/
└── recordian-agent           # Agent 启动脚本（可选）

agent_config.example.yaml     # 配置文件示例
```

## 6. 验收标准

### 6.1 Agent 功能

- [ ] 启动后监听指定端口
- [ ] 收到 paste 命令后正确执行粘贴
- [ ] 收到 ping 命令后返回 pong
- [ ] 收到 status 命令后返回状态信息
- [ ] 配置文件能正确加载
- [ ] 桌面通知能正确显示（启用时）
- [ ] 日志输出正常

### 6.2 Recordian 集成

- [ ] 新增命令行参数能正确解析
- [ ] 语音识别完成后向远程主机发送命令
- [ ] 连接失败时不阻塞主流程
- [ ] 日志正确输出

### 6.3 端到端测试

- [ ] 在A电脑语音识别
- [ ] B电脑自动收到粘贴（无需手动按Ctrl+V）
- [ ] 文字内容正确

## 7. 已知限制

1. **仅支持 Linux**：使用 xdotool/xclip，仅限 Linux
2. **仅支持 TCP**：不考虑 UDP
3. **无重连机制**：客户端一次连接失败则跳过
4. **无加密**：局域网内明文传输

## 8. 未来扩展（可选）

- 支持 UDP 广播发现 Agent
- 支持 TLS 加密
- 支持 WebSocket
- 支持配置文件热重载
- 支持多个 paste 命令队列
